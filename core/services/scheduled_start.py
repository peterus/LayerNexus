"""One-shot scheduled queue starts.

A :class:`~core.models.ScheduledQueueStart` asks the worker to start the next
queue job of a printer once, at a given time.  It is never recurring and a
schedule that is overdue by more than ``SCHEDULED_START_MAX_LATENESS_MINUTES``
is skipped so nothing starts by surprise.
"""

import logging
from datetime import datetime, timedelta

from django.conf import settings
from django.contrib.auth.models import User
from django.db import transaction
from django.utils import timezone

from core.models import PrinterProfile, ScheduledQueueStart
from core.services.queue import start_next_for_printer

logger = logging.getLogger(__name__)

__all__ = [
    "MAX_SCHEDULE_AHEAD",
    "cancel_scheduled_start",
    "run_due_scheduled_starts",
    "schedule_start",
]

#: How far in the future a start may be scheduled.
MAX_SCHEDULE_AHEAD = timedelta(days=7)

_SKIP_OUTCOMES = {"busy", "empty", "not_next", "no_gcode"}


def schedule_start(printer: PrinterProfile, start_at: datetime, user: User | None) -> ScheduledQueueStart:
    """Create the pending schedule of ``printer`` or move the existing one.

    Args:
        printer: The printer whose next job should be started.
        start_at: Aware datetime of the start.
        user: The user scheduling it (kept on creation only).

    Returns:
        The pending ``ScheduledQueueStart``.
    """
    with transaction.atomic():
        pending = ScheduledQueueStart.objects.filter(printer=printer, status=ScheduledQueueStart.STATUS_PENDING)
        if pending.update(start_at=start_at):
            return pending.get()
        return ScheduledQueueStart.objects.create(printer=printer, start_at=start_at, created_by=user)


def cancel_scheduled_start(printer: PrinterProfile) -> bool:
    """Cancel the pending schedule of ``printer``.

    Args:
        printer: The printer to cancel the schedule for.

    Returns:
        True if a pending schedule was cancelled.
    """
    return bool(
        ScheduledQueueStart.objects.filter(printer=printer, status=ScheduledQueueStart.STATUS_PENDING).update(
            status=ScheduledQueueStart.STATUS_CANCELLED, executed_at=timezone.now()
        )
    )


def run_due_scheduled_starts(now: datetime | None = None) -> list[ScheduledQueueStart]:
    """Execute all pending schedules that are due.

    Each due row is claimed atomically (so concurrent callers run it once),
    then either skipped as missed (overdue beyond the allowed lateness) or the
    next queue job of its printer is started.  There is no retry.

    Args:
        now: Reference time (defaults to the current time).

    Returns:
        The processed rows, refreshed.
    """
    now = now or timezone.now()
    max_lateness = timedelta(minutes=settings.SCHEDULED_START_MAX_LATENESS_MINUTES)
    processed: list[ScheduledQueueStart] = []
    due = ScheduledQueueStart.objects.filter(status=ScheduledQueueStart.STATUS_PENDING, start_at__lte=now)
    for row in due.select_related("printer"):
        late = now - row.start_at > max_lateness
        # Also match ``start_at``: a reschedule between the read above and this
        # claim must not execute the row with the stale time.
        claimed = ScheduledQueueStart.objects.filter(
            pk=row.pk, status=ScheduledQueueStart.STATUS_PENDING, start_at=row.start_at
        ).update(
            status=ScheduledQueueStart.STATUS_SKIPPED if late else ScheduledQueueStart.STATUS_STARTED,
            executed_at=now,
            # Overwritten with the real outcome; stays only if the worker dies mid-start.
            result_message=(
                "Skipped: missed (overdue, e.g. worker was down)"
                if late
                else "Start attempted; outcome unknown (worker interrupted?)"
            ),
        )
        if not claimed:
            continue
        if not late:
            status, message = _start(row.printer)
            ScheduledQueueStart.objects.filter(pk=row.pk).update(status=status, result_message=message)
        row.refresh_from_db()
        logger.info("Scheduled start %s for %s: %s", row.pk, row.printer.name, row.status)
        processed.append(row)
    return processed


def _start(printer: PrinterProfile) -> tuple[str, str]:
    """Start the next job and map the outcome to a schedule status and message."""
    try:
        result = start_next_for_printer(printer)
    except Exception as exc:  # noqa: BLE001 - never lose the result row
        logger.exception("Scheduled start on %s crashed", printer.name)
        return ScheduledQueueStart.STATUS_FAILED, f"Failed: {exc}"
    detail = f" ({result.error})" if result.error else ""
    if result.outcome == "started":
        return ScheduledQueueStart.STATUS_STARTED, "Started the next queue job."
    if result.outcome in _SKIP_OUTCOMES:
        return ScheduledQueueStart.STATUS_SKIPPED, f"Skipped: {result.outcome}{detail}"
    return ScheduledQueueStart.STATUS_FAILED, f"Failed: {result.outcome}{detail}"
