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
from datetime import timedelta
from typing import Any

from django.utils import timezone

from core.models import PrintQueue
from core.services.moonraker import map_klipper_state
from core.services.printer_backend import NormalizedJobStatus

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
    "server_exit": NormalizedJobStatus.STATE_ERROR,
}


def apply_status_event(entry: PrintQueue, event: dict[str, Any]) -> bool:
    """Apply a Moonraker JSON-RPC notification to a ``PrintQueue`` entry.

    Handles two Moonraker notifications:

    * ``notify_history_changed`` with ``action="finished"``: transitions
      the entry to ``awaiting_review`` and sets ``completed_at``.
    * ``notify_status_update``: reads ``print_stats.state`` and
      ``virtual_sdcard.progress`` and either updates the progress
      (throttled) or, if the state is terminal, triggers the same
      transition as above.

    Args:
        entry: The PrintQueue entry currently associated with the
            printer that produced the event.  Must have ``status ==
            STATUS_PRINTING`` for any transition to happen.
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
        return _handle_status_update(entry, params)

    logger.debug("Ignoring Moonraker event: %s", method)
    return False


def _handle_history_changed(entry: PrintQueue, params: list[Any]) -> bool:
    if entry.status != PrintQueue.STATUS_PRINTING:
        return False
    if not params:
        return False
    payload = params[0] if isinstance(params[0], dict) else {}
    if payload.get("action") != "finished":
        return False

    job = payload.get("job") or {}
    raw_status = job.get("status", "completed")
    normalized = _HISTORY_STATUS_MAP.get(raw_status, NormalizedJobStatus.STATE_COMPLETE)

    return _transition_to_review(entry, normalized, raw_status)


def _handle_status_update(entry: PrintQueue, params: list[Any]) -> bool:
    if entry.status != PrintQueue.STATUS_PRINTING:
        return False
    if not params:
        return False
    status_block = params[0] if isinstance(params[0], dict) else {}

    # Terminal state via print_stats -> transition immediately.
    print_stats = status_block.get("print_stats") or {}
    raw_state = print_stats.get("state")
    if raw_state:
        normalized = map_klipper_state(raw_state)
        if normalized in NormalizedJobStatus.TERMINAL_STATES:
            return _transition_to_review(entry, normalized, raw_state)

    # Otherwise update progress (throttled).
    virtual_sd = status_block.get("virtual_sdcard") or {}
    new_progress = virtual_sd.get("progress")
    if new_progress is None:
        return False

    now = timezone.now()
    if entry.status_updated_at and (now - entry.status_updated_at) < PROGRESS_WRITE_INTERVAL:
        return False

    entry.progress = float(new_progress)
    entry.status_updated_at = now
    entry.save(update_fields=["progress", "status_updated_at"])
    return True


def _transition_to_review(entry: PrintQueue, normalized_state: str, raw_state: str) -> bool:
    """Mark the entry as awaiting review with the given terminal state."""
    now = timezone.now()
    entry.status = PrintQueue.STATUS_AWAITING_REVIEW
    entry.completed_at = now
    entry.status_updated_at = now
    if normalized_state in (
        NormalizedJobStatus.STATE_ERROR,
        NormalizedJobStatus.STATE_CANCELLED,
    ):
        entry.last_error = f"Printer reported: {raw_state}"
    entry.save(
        update_fields=[
            "status",
            "completed_at",
            "status_updated_at",
            "last_error",
        ]
    )
    logger.info(
        "Queue entry %s transitioned to awaiting_review (state=%s)",
        entry.pk,
        raw_state,
    )
    return True
