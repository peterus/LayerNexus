"""Print queue service functions.

Provides reusable logic for starting prints via the printer backend
abstraction, extracted from the queue views so it can be called from
both ``RunNextQueueView`` and ``RunAllQueuesView`` without duplication.
"""

import logging
import re
from dataclasses import dataclass

from django.db import transaction
from django.db.models import QuerySet
from django.utils import timezone

from core.models import PrinterProfile, PrintJob, PrintJobPlate, PrintQueue
from core.services.printer_backend import PrinterError, get_printer_backend

logger = logging.getLogger(__name__)

__all__ = [
    "MOVE_DIRECTIONS",
    "QUEUE_EXECUTION_ORDER",
    "PRINTER_ERROR_MESSAGE",
    "QueueMoveResult",
    "ReviewResult",
    "StartResult",
    "busy_entry_for_printer",
    "move_queue_entry",
    "next_waiting_entry",
    "ordered_waiting_entries",
    "review_queue_entry",
    "start_next_for_printer",
    "start_print_for_queue_entry",
    "PrintStartError",
    "QueueReorderError",
    "ReviewStateError",
]

#: The order in which waiting entries of one printer are executed.  The queue
#: list, ``RunNextQueueView``/``RunAllQueuesView`` and ``move_queue_entry`` all
#: use this so the displayed order always equals the processing order.
QUEUE_EXECUTION_ORDER = ("-priority", "position", "added_at", "pk")

#: Accepted ``direction`` values for :func:`move_queue_entry`.
MOVE_DIRECTIONS = ("up", "down", "top", "bottom")


class PrintStartError(Exception):
    """Raised when a print could not be started."""


class QueueReorderError(Exception):
    """Raised when a queue entry cannot be moved (not waiting / bad input)."""


class ReviewStateError(Exception):
    """Raised when a queue entry cannot be reviewed (gone / not awaiting review / other printer)."""


#: User-facing text when the printer backend fails while starting a print.
PRINTER_ERROR_MESSAGE = "Failed to start print. Please check the printer connection and try again."


@dataclass(frozen=True)
class ReviewResult:
    """Outcome of :func:`review_queue_entry`.

    Attributes:
        outcome: ``"passed"``, ``"requeued"`` or ``"discarded"``.
        plate_label: ``"Plate <n> of '<job>'"`` as shown in the web messages.
        plate_number: The reviewed plate's number.
        plates_completed: Completed plates of the job after the review.
        plate_count: Total plates of the job.
        retry_count: The entry's retry count after the review.
        max_retries: The entry's maximum retries.
    """

    outcome: str
    plate_label: str
    plate_number: int
    plates_completed: int
    plate_count: int
    retry_count: int
    max_retries: int


@dataclass(frozen=True)
class StartResult:
    """Outcome of :func:`start_next_for_printer`.

    Attributes:
        outcome: ``"started"``, ``"busy"``, ``"empty"``, ``"not_next"``,
            ``"not_configured"`` or ``"error"``.
        entry: The started entry (``"started"``), or the entry that would have
            been started (``"not_next"``, ``"error"``).
        busy_entry: The printing / awaiting-review entry for ``"busy"``.
        error: User-facing error text for ``"error"``.
    """

    outcome: str
    entry: PrintQueue | None = None
    busy_entry: PrintQueue | None = None
    error: str = ""


@dataclass(frozen=True)
class QueueMoveResult:
    """Outcome of :func:`move_queue_entry`.

    Attributes:
        entry: The moved (refreshed) queue entry.
        previous_priority: The entry's priority before the move.
    """

    entry: PrintQueue
    previous_priority: int

    @property
    def priority_changed(self) -> bool:
        """Return True if the move adopted a neighbour's priority."""
        return self.entry.priority != self.previous_priority


def ordered_waiting_entries(printer: PrinterProfile | int) -> QuerySet[PrintQueue]:
    """Return the waiting entries of a printer in execution order.

    Args:
        printer: The printer (or its primary key) whose queue to return.

    Returns:
        A queryset of waiting ``PrintQueue`` entries, next-to-print first.
    """
    return PrintQueue.objects.filter(printer=printer, status=PrintQueue.STATUS_WAITING).order_by(*QUEUE_EXECUTION_ORDER)


def next_waiting_entry(printer: PrinterProfile) -> PrintQueue | None:
    """Return the waiting entry that should be printed next on ``printer``.

    Args:
        printer: The printer to pick the next entry for.

    Returns:
        The first waiting entry in execution order, or ``None`` if the queue
        has no waiting entries.
    """
    return ordered_waiting_entries(printer).select_related("plate__print_job").first()


def busy_entry_for_printer(printer: PrinterProfile | int) -> PrintQueue | None:
    """Return the entry that keeps ``printer`` busy (printing or awaiting review).

    Args:
        printer: The printer (or its primary key) to check.

    Returns:
        The printing / awaiting-review entry, or ``None`` if the printer is free.
    """
    return (
        PrintQueue.objects.filter(
            printer=printer,
            status__in=[PrintQueue.STATUS_PRINTING, PrintQueue.STATUS_AWAITING_REVIEW],
        )
        .select_related("plate__print_job")
        .first()
    )


def review_queue_entry(entry_pk: int, passed: bool, *, printer_pk: int | None = None) -> ReviewResult:
    """Confirm a finished print as Pass or Fail.

    **Pass:** marks the plate completed and removes the entry; if all plates
    of the job are completed the job is completed as well.

    **Fail:** increments ``retry_count`` and requeues the entry at
    ``position=0`` (first within its priority group).  If ``retry_count``
    reaches ``max_retries`` the plate is marked failed and the entry removed.

    The entry is re-fetched and its state checked inside one transaction, so
    concurrent reviews (web + printer) cannot both apply.

    Args:
        entry_pk: Primary key of the ``PrintQueue`` entry.
        passed: ``True`` for Pass, ``False`` for Fail.
        printer_pk: If given, the entry must belong to this printer.

    Returns:
        A :class:`ReviewResult` describing what happened.

    Raises:
        ReviewStateError: If the entry does not exist, is not awaiting review,
            or belongs to another printer than ``printer_pk``.
    """
    with transaction.atomic():
        entry = (
            PrintQueue.objects.select_for_update()
            .select_related("plate__print_job")
            .filter(pk=entry_pk, status=PrintQueue.STATUS_AWAITING_REVIEW)
            .first()
        )
        if entry is None or entry.plate is None:
            raise ReviewStateError("Queue entry is not awaiting review.")
        if printer_pk is not None and entry.printer_id != printer_pk:
            raise ReviewStateError("Queue entry belongs to another printer.")

        plate = entry.plate
        job = plate.print_job
        plate_label = f"Plate {plate.plate_number} of '{job}'"

        if passed:
            plate.status = PrintJobPlate.STATUS_COMPLETED
            plate.completed_at = timezone.now()
            plate.save(update_fields=["status", "completed_at"])
            entry.delete()
            if job.all_plates_completed:
                job.status = PrintJob.STATUS_COMPLETED
                job.completed_at = timezone.now()
                job.save(update_fields=["status", "completed_at"])
            outcome = "passed"
        else:
            entry.retry_count += 1
            if entry.retry_count >= entry.max_retries:
                plate.status = PrintJobPlate.STATUS_FAILED
                plate.save(update_fields=["status"])
                entry.delete()
                outcome = "discarded"
            else:
                entry.status = PrintQueue.STATUS_WAITING
                entry.started_at = None
                entry.completed_at = None
                # Waiting positions are renumbered 1..n by manual reordering, so
                # a stale position would land the retry at an arbitrary rank.
                # Position 0 puts it first within its priority group (it was
                # the entry being printed, i.e. it was next in line).
                entry.position = 0
                entry.save(update_fields=["status", "retry_count", "started_at", "completed_at", "position"])
                plate.status = PrintJobPlate.STATUS_WAITING
                plate.save(update_fields=["status"])
                outcome = "requeued"

        result = ReviewResult(
            outcome=outcome,
            plate_label=plate_label,
            plate_number=plate.plate_number,
            plates_completed=job.plates.filter(status=PrintJobPlate.STATUS_COMPLETED).count(),
            plate_count=job.plate_count,
            retry_count=entry.retry_count,
            max_retries=entry.max_retries,
        )

    logger.info("Reviewed queue entry %s: %s", entry_pk, outcome)
    return result


def start_next_for_printer(printer: PrinterProfile, *, expected_entry_pk: int | None = None) -> StartResult:
    """Start the next waiting entry on ``printer`` (same rules as "Run Next").

    The printer must be free (no printing / awaiting-review entry).  The
    busy check and next-entry lookup run in a short transaction; the G-code
    upload and start happen **after** it so the SQLite write lock is not
    held during the (possibly slow) upload.

    Args:
        printer: The printer to start the next print on.
        expected_entry_pk: If given, only start if this entry is still the
            next waiting one (guards stale "Start" buttons).

    Returns:
        A :class:`StartResult`; never raises for printer/start errors.
    """
    if not printer.moonraker_url:
        return StartResult(outcome="not_configured")

    with transaction.atomic():
        busy_entry = busy_entry_for_printer(printer)
        if busy_entry is not None:
            return StartResult(outcome="busy", busy_entry=busy_entry)
        entry = next_waiting_entry(printer)
        if entry is None:
            return StartResult(outcome="empty")
        if expected_entry_pk is not None and entry.pk != expected_entry_pk:
            return StartResult(outcome="not_next", entry=entry)

    try:
        start_print_for_queue_entry(entry)
    except PrintStartError as exc:
        return StartResult(outcome="error", entry=entry, error=str(exc))
    except PrinterError as exc:
        logger.exception("Failed to run queue entry %s: %s", entry.pk, exc)
        return StartResult(outcome="error", entry=entry, error=PRINTER_ERROR_MESSAGE)
    return StartResult(outcome="started", entry=entry)


def move_queue_entry(
    entry_pk: int,
    *,
    direction: str | None = None,
    target_index: int | None = None,
) -> QueueMoveResult:
    """Move a waiting queue entry within its printer's queue.

    Only ``waiting`` entries can be moved; ``printing`` and ``awaiting_review``
    entries are fixed.  The move happens inside one transaction with the
    printer's waiting entries locked, and all waiting positions of that
    printer are renumbered gaplessly (``1..n``) in the new order.

    Priority interaction: execution order is ``-priority`` first, so an entry
    moved past a neighbour of a different priority adopts that priority
    (moved above a *High* entry -> becomes *High*; moved below a *Low* entry
    -> becomes *Low*).  This keeps the manual order and the execution order
    identical without a separate "manual" sort key.

    Args:
        entry_pk: Primary key of the ``PrintQueue`` entry to move.
        direction: One of ``"up"``, ``"down"``, ``"top"``, ``"bottom"``.
        target_index: Zero-based target index among the printer's waiting
            entries (clamped to the valid range).  Used if ``direction`` is
            not given.

    Returns:
        A :class:`QueueMoveResult` with the moved (refreshed) entry and its
        priority before the move.

    Raises:
        PrintQueue.DoesNotExist: If no entry with ``entry_pk`` exists.
        QueueReorderError: If the entry is not waiting, or neither a valid
            ``direction`` nor a ``target_index`` was given.
    """
    if direction is None and target_index is None:
        raise QueueReorderError("Either a direction or a target position is required.")
    if direction is not None and direction not in MOVE_DIRECTIONS:
        raise QueueReorderError(f"Invalid direction: {direction}.")

    with transaction.atomic():
        entry = PrintQueue.objects.select_for_update().get(pk=entry_pk)
        if entry.status != PrintQueue.STATUS_WAITING:
            raise QueueReorderError("Only waiting queue entries can be reordered.")

        waiting = list(ordered_waiting_entries(entry.printer_id).select_for_update())
        current = next(i for i, e in enumerate(waiting) if e.pk == entry.pk)
        last = len(waiting) - 1

        if direction == "up":
            target = current - 1
        elif direction == "down":
            target = current + 1
        elif direction == "top":
            target = 0
        elif direction == "bottom":
            target = last
        else:
            target = target_index
        target = max(0, min(target, last))

        moved = waiting.pop(current)
        previous_priority = moved.priority
        waiting.insert(target, moved)

        # Adopt the neighbours' priority boundary so ``-priority`` ordering
        # cannot contradict the manual position.
        if target > 0 and moved.priority > waiting[target - 1].priority:
            moved.priority = waiting[target - 1].priority
        if target < last and moved.priority < waiting[target + 1].priority:
            moved.priority = waiting[target + 1].priority

        for index, item in enumerate(waiting, start=1):
            item.position = index
        PrintQueue.objects.bulk_update(waiting, ["position", "priority"])

    logger.info("Moved queue entry %s to index %s on printer %s", entry.pk, target, entry.printer_id)
    moved.refresh_from_db()
    return QueueMoveResult(entry=moved, previous_priority=previous_priority)


def start_print_for_queue_entry(entry: PrintQueue) -> str:
    """Upload G-code and start a print for the given queue entry.

    The entry must already be validated (waiting status, printer free,
    G-code file present).  This function handles:

    1. Building a filesystem-safe remote filename.
    2. Uploading the G-code via the printer backend.
    3. Starting the print via the printer backend.
    4. Updating the queue entry, plate, and job status/timestamps.

    Args:
        entry: A ``PrintQueue`` instance with ``plate__print_job`` and
            ``printer`` already selected/loaded.

    Returns:
        The remote filename used for the print.

    Raises:
        PrintStartError: If the entry has no G-code file.
        PrinterError: If the printer backend call fails.
    """
    plate = entry.plate
    job = plate.print_job
    printer = entry.printer

    gcode_file = plate.gcode_file
    if not gcode_file:
        raise PrintStartError(f"No G-code file for plate {plate.plate_number}. Slice the job first.")

    # Build a descriptive, filesystem-safe remote filename
    safe_name = re.sub(r"[^\w\-]", "_", str(job))[:50]
    remote_filename = f"LN_{safe_name}_p{plate.plate_number}.gcode"

    backend = get_printer_backend(printer)

    # Upload gcode and start the print
    backend.upload_gcode(gcode_file.path, filename=remote_filename)
    backend.start_print(remote_filename)

    # Update queue entry
    entry.status = PrintQueue.STATUS_PRINTING
    entry.started_at = timezone.now()
    entry.save(update_fields=["status", "started_at"])

    # Update plate status
    plate.status = PrintJobPlate.STATUS_PRINTING
    plate.remote_job_id = remote_filename
    plate.started_at = timezone.now()
    plate.save(update_fields=["status", "remote_job_id", "started_at"])

    # Update job status
    job.status = PrintJob.STATUS_PRINTING
    if not job.started_at:
        job.started_at = timezone.now()
    job.save(update_fields=["status", "started_at"])

    return remote_filename
