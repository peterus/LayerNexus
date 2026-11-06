"""Synchronous event -> PrintQueue update logic for the Moonraker worker.

The WebSocket worker (``core.services.moonraker_ws``) receives raw
Moonraker JSON-RPC notifications and hands them to
:func:`apply_status_event` together with the matching
:class:`~core.models.PrintQueue` entry.  All business logic around
"is this a terminal state?", "should we write to the DB now?" and
"which fields to update" lives here, keeping the async I/O layer
dumb and this module fully unit-testable.

The module is intentionally sync and free of Django async helpers.
The caller (worker command) is responsible for using
``asgiref.sync.sync_to_async`` when invoking these functions from
an event loop.
"""

from __future__ import annotations

import logging
import math
from datetime import UTC, datetime, timedelta
from pathlib import PurePosixPath
from typing import Any

from django.db import transaction
from django.utils import timezone

from core.models import PrintQueue
from core.services.moonraker import map_klipper_state
from core.services.printer_backend import NormalizedJobStatus
from core.services.queue import ordered_waiting_entries

logger = logging.getLogger(__name__)

#: Minimum interval between consecutive progress writes per queue entry.
PROGRESS_WRITE_INTERVAL = timedelta(seconds=5)

#: Map Moonraker ``notify_history_changed`` job statuses -> normalized state.
_HISTORY_STATUS_MAP = {
    "completed": NormalizedJobStatus.STATE_COMPLETE,
    "cancelled": NormalizedJobStatus.STATE_CANCELLED,
    "error": NormalizedJobStatus.STATE_ERROR,
    "interrupted": NormalizedJobStatus.STATE_ERROR,
    "klippy_shutdown": NormalizedJobStatus.STATE_ERROR,
    "klippy_disconnect": NormalizedJobStatus.STATE_ERROR,
    "server_exit": NormalizedJobStatus.STATE_ERROR,
}

#: ``print_stats.state`` values that always end a running attempt.  ``standby``
#: is deliberately missing: Klipper resets ``print_stats`` (-> ``standby``) when
#: ``SDCARD_PRINT_FILE`` loads a file, *before* the print starts, so every
#: start briefly broadcasts ``standby``.  Treating it as an end marked fresh
#: attempts as finished while the printer was printing.  ``standby`` only ends
#: an attempt that already recorded progress (see :func:`_ends_attempt`), which
#: covers prints aborted by ``FIRMWARE_RESTART`` (seen in the resubscribe
#: snapshot) or ``SDCARD_RESET_FILE`` even
#: without Moonraker's ``[history]``.  Other ends arrive via
#: ``notify_history_changed`` (incl. ``klippy_shutdown``/``interrupted``).
PRINT_END_STATES = frozenset(
    {
        NormalizedJobStatus.STATE_COMPLETE,
        NormalizedJobStatus.STATE_CANCELLED,
        NormalizedJobStatus.STATE_ERROR,
    }
)

#: Raw ``print_stats.state`` values of a running print that may be adopted.
RUNNING_PRINT_STATES = frozenset({"printing", "paused"})

#: Oldest start time accepted from the printer for an adopted print; older
#: (or future) values are treated as a wrong printer clock and replaced by now.
MAX_ADOPTED_PRINT_AGE = timedelta(days=7)

#: Clock skew tolerated for a printer-reported start time in the future.
MAX_START_TIME_SKEW = timedelta(minutes=1)


def apply_status_event(entry: PrintQueue, event: dict[str, Any]) -> bool:
    """Apply a Moonraker JSON-RPC notification to a ``PrintQueue`` entry.

    Handles two Moonraker notifications:

    * ``notify_history_changed`` with ``action="finished"``: transitions
      the entry to ``awaiting_review`` and sets ``completed_at`` — unless
      the finished job's file is not the one this attempt printed (only
      checked when both file names are known).
    * ``notify_status_update``: records the objects Klipper excluded
      (``exclude_object.excluded_objects``, see
      :meth:`PrintQueue.record_excluded_objects`), then reads
      ``print_stats.state`` and ``virtual_sdcard.progress`` and either
      updates the progress (throttled) or, if the state ends the print
      (:data:`PRINT_END_STATES`; ``standby`` only after recorded progress),
      triggers the same transition as above.

    Args:
        entry: The PrintQueue entry currently associated with the
            printer that produced the event.  Must be printing (started,
            not finished, no result) for any transition to happen; the
            write is re-checked in the database, so a stale instance of an
            attempt resolved meanwhile (e.g. removed as cancelled) is ignored.
        event: The raw Moonraker notification payload (a JSON-RPC
            request dict with ``method`` and ``params``).

    Returns:
        ``True`` if the entry was modified and saved, ``False`` if the
        event was ignored (throttled progress, wrong status, unknown
        method, …).
    """
    method = event.get("method")
    params = event.get("params") or []

    if method == "notify_history_changed":
        return _handle_history_changed(entry, params)
    if method == "notify_status_update":
        # Exclusions first: the final update may carry the last exclusion together with the end state.
        try:
            excluded = _record_excluded_objects(entry, params)
        except Exception:  # noqa: BLE001 — a bad exclusion payload must not block the end of the print
            logger.exception("Queue entry %s: recording the excluded objects failed", entry.pk)
            excluded = False
        return _handle_status_update(entry, params) or excluded

    logger.debug("Ignoring Moonraker event: %s", method)
    return False


def _handle_history_changed(entry: PrintQueue, params: list[Any]) -> bool:
    if not entry.is_printing:
        return False
    if not params:
        return False
    payload = params[0] if isinstance(params[0], dict) else {}
    if payload.get("action") != "finished":
        return False

    job = payload.get("job") or {}
    if not _is_attempt_file(entry, job.get("filename")):
        logger.info(
            "Ignoring finished history job %r for queue entry %s: not its file",
            job.get("filename"),
            entry.pk,
        )
        return False
    raw_status = job.get("status", "completed")
    normalized = _HISTORY_STATUS_MAP.get(raw_status, NormalizedJobStatus.STATE_COMPLETE)

    return _transition_to_review(entry, normalized, raw_status)


def _record_excluded_objects(entry: PrintQueue, params: list[Any]) -> bool:
    """Record ``exclude_object.excluded_objects`` of a status update on the printing entry.

    Also handles the subscribe snapshot (a reconnect mid-print), which carries
    the full list.  A malformed payload is ignored.

    Returns:
        True if new excluded objects were recorded.
    """
    if not entry.is_printing or not params or not isinstance(params[0], dict):
        return False
    exclude_object = params[0].get("exclude_object")
    if not isinstance(exclude_object, dict):
        return False
    names = exclude_object.get("excluded_objects")
    if not isinstance(names, list):
        return False
    if entry.record_excluded_objects(names):
        logger.info("Queue entry %s: printer excluded objects %s", entry.pk, names)
        return True
    return False


def _handle_status_update(entry: PrintQueue, params: list[Any]) -> bool:
    if not entry.is_printing:
        return False
    if not params:
        return False
    status_block = params[0] if isinstance(params[0], dict) else {}

    # End of the print via print_stats -> transition immediately.
    print_stats = status_block.get("print_stats") or {}
    raw_state = print_stats.get("state")
    if raw_state:
        normalized = map_klipper_state(raw_state)
        if _ends_attempt(entry, normalized):
            return _transition_to_review(entry, normalized, raw_state)

    # Otherwise update progress (throttled).
    virtual_sd = status_block.get("virtual_sdcard") or {}
    new_progress = virtual_sd.get("progress")
    if new_progress is None:
        return False

    now = timezone.now()
    if entry.status_updated_at and (now - entry.status_updated_at) < PROGRESS_WRITE_INTERVAL:
        return False

    # Reject booleans explicitly: bool is an int subclass, so float(True)
    # is 1.0 and would pass the range guard below (True == 1). A JSON
    # boolean is malformed progress data, not a real fraction.
    if isinstance(new_progress, bool):
        logger.debug("Ignoring boolean virtual_sdcard.progress: %r", new_progress)
        return False

    try:
        progress_value = float(new_progress)
    except (TypeError, ValueError, OverflowError):
        # A malformed progress value from the printer must not raise —
        # the broad except in the WS layer would swallow it and silently
        # stall all further progress. This includes OverflowError, which
        # float() raises for a valid-JSON integer too large to convert
        # (e.g. 10**400). Skip this update instead.
        logger.debug("Ignoring non-numeric virtual_sdcard.progress: %r", new_progress)
        return False

    # Enforce the documented 0.0-1.0 contract. The chained comparison also
    # rejects NaN and ±inf (which float() happily produces), so a bogus
    # value can't be persisted or advance status_updated_at (which would
    # otherwise throttle later valid progress updates).
    if not (0.0 <= progress_value <= 1.0):
        logger.debug("Ignoring out-of-range virtual_sdcard.progress: %r", new_progress)
        return False

    # Conditional write: the attempt may have been finished or resolved
    # (e.g. removed) since this instance was loaded.
    if not PrintQueue.objects.printing().filter(pk=entry.pk).update(progress=progress_value, status_updated_at=now):
        return False
    entry.progress = progress_value
    entry.status_updated_at = now
    return True


def _ends_attempt(entry: PrintQueue, normalized_state: str) -> bool:
    """Return whether a ``print_stats.state`` ends the running attempt.

    ``standby`` is ambiguous: Klipper reports it right before every print
    starts, but also after a print was aborted without a ``cancelled`` /
    ``error`` state.  It only counts as an end once the attempt recorded real
    progress — ``mark_started`` clears the progress of earlier attempts, so a
    start-up ``standby`` never qualifies.

    Args:
        entry: The printing queue entry (freshly loaded by the worker).
        normalized_state: The normalized ``print_stats.state``.

    Returns:
        True if the attempt should move to "awaiting review".
    """
    if normalized_state in PRINT_END_STATES:
        return True
    return normalized_state == NormalizedJobStatus.STATE_STANDBY and bool(entry.progress)


def _is_attempt_file(entry: PrintQueue, job_filename: Any) -> bool:
    """Return whether a finished Moonraker job belongs to this attempt.

    Compares the job's file name with the remote file name stored on the
    attempt's plate (by base name — Moonraker reports paths relative to the
    ``gcodes`` root).  When either name is unknown the job is assumed to
    belong to the attempt, keeping the previous behaviour.

    Args:
        entry: The printing queue entry.
        job_filename: ``job.filename`` from ``notify_history_changed``.

    Returns:
        False only if both names are known and differ.
    """
    plate = entry.plate
    remote_filename = plate.remote_job_id if plate is not None else ""
    if not remote_filename or not isinstance(job_filename, str) or not job_filename:
        return True
    return PurePosixPath(job_filename).name == PurePosixPath(remote_filename).name


def _transition_to_review(entry: PrintQueue, normalized_state: str, raw_state: str) -> bool:
    """Mark the entry as awaiting review with the given terminal state.

    Returns:
        True if the attempt moved to "awaiting review", False if it was no
        longer printing in the database (finished or resolved meanwhile).
    """
    if normalized_state in (
        NormalizedJobStatus.STATE_ERROR,
        NormalizedJobStatus.STATE_CANCELLED,
        NormalizedJobStatus.STATE_STANDBY,
    ):
        error = f"Printer reported: {raw_state}"
    else:
        # Clear any leftover error from a previous attempt.
        error = ""
    if not entry.mark_finished(error=error):
        logger.info("Ignoring end of queue entry %s (state=%s): no longer printing", entry.pk, raw_state)
        return False
    logger.info(
        "Queue entry %s transitioned to awaiting_review (state=%s)",
        entry.pk,
        raw_state,
    )
    return True


def adopt_external_print(printer_pk: int, event: dict[str, Any], *, current_filename: str = "") -> PrintQueue | None:
    """Adopt a print started directly on the printer for a matching waiting attempt.

    A print started on the printer itself (e.g. a reprint from KlipperScreen or
    Mainsail) reuses a file LayerNexus uploaded earlier.  If the event shows
    such a start — ``notify_history_changed`` with ``action="added"`` or a
    status update with ``print_stats.state`` ``printing``/``paused`` (also the
    resubscribe snapshot, so a worker restart mid-print catches up) — the
    waiting attempt of this printer whose plate's remote file has the same base
    name is marked as started.  Nothing happens while an attempt of the printer
    is printing or a start is claimed (that print is LayerNexus' own), and no
    attempt is ever created.  An attempt awaiting review does not block: the
    printer evidently prints again, and leaving the print untracked is worse.

    Remote file names are not unique (``LN_<job name>_p<plate>.gcode``, the job
    name may repeat), so several plates can match.  The printer then holds the
    file of the plate it started last; if that plate is not among the matches
    the print is left alone rather than counted for the wrong job.

    Args:
        printer_pk: Primary key of the printer that sent the event.
        event: The raw Moonraker notification.
        current_filename: The printer's last reported ``print_stats.filename``,
            used when a status update carries the state but not the file name
            (Klipper only sends changed fields).

    Returns:
        The adopted attempt (with ``plate__print_job`` loaded), or ``None``.
    """
    started = _started_print(event, current_filename)
    if started is None:
        return None
    filename, when = started
    base_name = PurePosixPath(filename).name
    with transaction.atomic():
        attempts = PrintQueue.objects.filter(printer_id=printer_pk)
        if attempts.printing().exists() or attempts.filter(PrintQueue.active_start_claim_q()).exists():
            return None
        candidates = (
            ordered_waiting_entries(printer_pk)
            .filter(plate__isnull=False)
            .exclude(plate__remote_job_id="")
            .select_related("plate__print_job")
        )
        matches = [
            candidate for candidate in candidates if PurePosixPath(candidate.plate.remote_job_id).name == base_name
        ]
        entry = _unambiguous_match(attempts, matches, base_name)
        if entry is None:
            if len(matches) > 1:
                logger.warning(
                    "Not adopting print %r on printer %s: plates %s share the file name",
                    filename,
                    printer_pk,
                    [match.plate_id for match in matches],
                )
            return None
        entry.mark_started(remote_filename=filename, when=when)
    logger.info("Queue entry %s: adopted externally started print %r on printer %s", entry.pk, filename, printer_pk)
    return entry


def _unambiguous_match(attempts: Any, matches: list[PrintQueue], base_name: str) -> PrintQueue | None:
    """Return the waiting attempt whose plate's file is on the printer.

    Args:
        attempts: Queryset of all attempts of the printer.
        matches: Waiting attempts whose remote file name matches, in queue order.
        base_name: Base name of the started file.

    Returns:
        The only match, or — for several — the one whose plate was the last
        on this printer to start a file of that name; ``None`` if that plate is
        not waiting or no such start is known.
    """
    if len(matches) <= 1:
        return matches[0] if matches else None
    previous_starts = (
        attempts.filter(started_at__isnull=False, plate__remote_job_id__endswith=base_name)
        .order_by("-started_at", "-pk")
        .values_list("plate_id", "plate__remote_job_id")
    )
    last_plate_id = next(
        (plate_id for plate_id, remote in previous_starts.iterator() if PurePosixPath(remote).name == base_name),
        None,
    )
    return next((match for match in matches if match.plate_id == last_plate_id), None)


def _started_print(event: dict[str, Any], current_filename: str = "") -> tuple[str, datetime | None] | None:
    """Return the file name and start time of a print start shown by ``event``.

    Args:
        event: The raw Moonraker notification.
        current_filename: Fallback file name for a status update without one.

    Returns:
        ``(filename, start time or None)`` for a history ``added`` event or a
        status update of a running print with a file name, else ``None``.
    """
    params = event.get("params")
    if not isinstance(params, list) or not params or not isinstance(params[0], dict):
        return None
    method = event.get("method")
    if method == "notify_history_changed":
        job = params[0].get("job")
        if params[0].get("action") != "added" or not isinstance(job, dict):
            return None
        filename = job.get("filename")
        when = _plausible_start(_timestamp_to_datetime(job.get("start_time")))
    elif method == "notify_status_update":
        print_stats = params[0].get("print_stats")
        if not isinstance(print_stats, dict) or print_stats.get("state") not in RUNNING_PRINT_STATES:
            return None
        filename = print_stats.get("filename", current_filename)
        duration = _finite_number(print_stats.get("total_duration"))
        when = None
        if duration is not None and duration <= MAX_ADOPTED_PRINT_AGE.total_seconds():
            when = timezone.now() - timedelta(seconds=duration)
    else:
        return None
    if not isinstance(filename, str) or not filename:
        return None
    return filename, when


def _finite_number(value: Any) -> float | None:
    """Return ``value`` as a finite, non-negative float, or ``None`` if it is not one.

    Booleans are rejected (``float(True)`` is ``1.0``), as are values
    ``float()`` cannot convert or that overflow.
    """
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    try:
        number = float(value)
    except OverflowError:
        return None
    if not math.isfinite(number) or number < 0:
        return None
    return number


def _timestamp_to_datetime(value: Any) -> datetime | None:
    """Convert a Moonraker Unix timestamp to an aware datetime (``None`` if invalid)."""
    number = _finite_number(value)
    if number is None:
        return None
    try:
        return datetime.fromtimestamp(number, tz=UTC)
    except (OverflowError, OSError, ValueError):
        return None


def _plausible_start(when: datetime | None) -> datetime | None:
    """Return ``when`` if it is a plausible start time of a running print, else ``None``.

    A printer with a wrong clock must not record a start far in the past or
    in the future; the caller falls back to now.
    """
    if when is None:
        return None
    now = timezone.now()
    if when > now + MAX_START_TIME_SKEW or when < now - MAX_ADOPTED_PRINT_AGE:
        return None
    return min(when, now)


def reported_print_filename(event: dict[str, Any]) -> str | None:
    """Return ``print_stats.filename`` of a status update, ``None`` if it carries none.

    The worker remembers the last value per printer: Klipper sends only changed
    fields, so a reprint of the same file reports ``printing`` without it.

    Args:
        event: The raw Moonraker notification.

    Returns:
        The reported file name (``""`` after a reset), or ``None``.
    """
    if event.get("method") != "notify_status_update":
        return None
    params = event.get("params")
    if not isinstance(params, list) or not params or not isinstance(params[0], dict):
        return None
    print_stats = params[0].get("print_stats")
    if not isinstance(print_stats, dict):
        return None
    filename = print_stats.get("filename")
    return filename if isinstance(filename, str) else None
