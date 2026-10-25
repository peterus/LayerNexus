"""Print queue service functions.

Provides reusable logic for starting prints via the printer backend
abstraction, extracted from the queue views so it can be called from
both ``RunNextQueueView`` and ``RunAllQueuesView`` without duplication.
"""

import logging
import re

from django.db import transaction
from django.db.models import QuerySet
from django.utils import timezone

from core.models import PrinterProfile, PrintJob, PrintJobPlate, PrintQueue
from core.services.printer_backend import get_printer_backend

logger = logging.getLogger(__name__)

__all__ = [
    "QUEUE_EXECUTION_ORDER",
    "move_queue_entry",
    "next_waiting_entry",
    "ordered_waiting_entries",
    "start_print_for_queue_entry",
    "PrintStartError",
    "QueueReorderError",
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


def move_queue_entry(
    entry_pk: int,
    *,
    direction: str | None = None,
    target_index: int | None = None,
) -> PrintQueue:
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
        The moved (refreshed) ``PrintQueue`` entry.

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
    return moved


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
