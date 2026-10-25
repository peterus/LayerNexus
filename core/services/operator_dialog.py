"""Printer-agnostic operator dialogs (review a finished plate, start the next job).

This module describes *what* to ask the operator at the printer — a title,
some text lines and buttons that carry an abstract action — and applies those
actions to the queue.  It knows nothing about how a dialog is rendered or how
a button press reaches LayerNexus; that is the job of a printer-specific
adapter that only the printer worker imports.

All functions are synchronous and use the ORM; async callers must wrap them
with ``asgiref.sync.sync_to_async``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from core.models import PrinterProfile, PrintJob, PrintQueue
from core.services.queue import (
    ReviewStateError,
    busy_entry_for_printer,
    next_waiting_entry,
    review_queue_entry,
    start_next_for_printer,
)
from core.templatetags.core_tags import duration_format

logger = logging.getLogger(__name__)

__all__ = [
    "DIALOG_TITLE",
    "DialogButton",
    "Dismiss",
    "OperatorAction",
    "OperatorDialog",
    "Review",
    "ShowMenu",
    "StartNext",
    "filament_summary",
    "handle_action",
    "menu_dialog",
    "next_job_dialog",
    "review_dialog",
]

#: Title of every operator dialog.
DIALOG_TITLE = "LayerNexus"

ALREADY_HANDLED = "Already handled"


@dataclass(frozen=True)
class Review:
    """Mark the plate of a queue entry as passed (Good) or failed (Bad)."""

    entry_pk: int
    passed: bool


@dataclass(frozen=True)
class StartNext:
    """Start the given queue entry, if it is still the next waiting one."""

    entry_pk: int


@dataclass(frozen=True)
class Dismiss:
    """Close the dialog without doing anything."""


@dataclass(frozen=True)
class ShowMenu:
    """Show the dialog that fits the printer's current state."""


OperatorAction = Review | StartNext | Dismiss | ShowMenu


@dataclass(frozen=True)
class DialogButton:
    """A dialog button.

    Attributes:
        label: Button text.
        action: The action triggered by pressing the button.
        style: ``"primary"``, ``"secondary"``, ``"warning"`` or ``"error"``.
    """

    label: str
    action: OperatorAction
    style: str = "primary"


@dataclass(frozen=True)
class OperatorDialog:
    """A dialog to show on the printer.

    Attributes:
        title: Dialog title.
        lines: Text lines, top to bottom.
        buttons: Buttons, left to right.
    """

    title: str
    lines: tuple[str, ...]
    buttons: tuple[DialogButton, ...]


CLOSE_BUTTON = DialogButton("Close", Dismiss(), "secondary")


def _job_label(job: PrintJob) -> str:
    """Return the job's name (without the status suffix of ``str(job)``)."""
    return job.name or f"Job #{job.pk}"


def _info_dialog(*lines: str, buttons: tuple[DialogButton, ...] = (CLOSE_BUTTON,)) -> OperatorDialog:
    return OperatorDialog(title=DIALOG_TITLE, lines=tuple(lines), buttons=buttons)


def filament_summary(job: PrintJob) -> str:
    """Describe the filament(s) a job expects, e.g. ``"Filament: PLA Black"``.

    Uses the distinct non-empty material/colour combinations of the job's
    parts, sorted and joined with ``", "``.

    Args:
        job: The print job.

    Returns:
        The summary line, or ``"Filament: not specified"`` if no part has a
        material or colour set.
    """
    combos = {
        " ".join(value for value in (material.strip(), color.strip()) if value)
        for material, color in job.job_parts.values_list("part__material", "part__color")
    }
    combos.discard("")
    if not combos:
        return "Filament: not specified"
    return f"Filament: {', '.join(sorted(combos))}"


def review_dialog(entry: PrintQueue) -> OperatorDialog:
    """Build the Good/Bad dialog for a finished (awaiting review) entry.

    Args:
        entry: The queue entry awaiting review.

    Returns:
        The review dialog.
    """
    plate = entry.plate
    job = plate.print_job
    lines = [_job_label(job), f"Plate {plate.plate_number} of {job.plate_count}"]
    if entry.last_error:
        lines.append(f"Ended: {entry.last_error}")
    return _info_dialog(
        *lines,
        buttons=(
            DialogButton("Good", Review(entry.pk, True), "primary"),
            DialogButton("Bad", Review(entry.pk, False), "error"),
        ),
    )


def _busy_line(entry: PrintQueue) -> str:
    return f"{entry.get_status_display()}: {_job_label(entry.plate.print_job)}, plate {entry.plate.plate_number}"


def next_job_dialog(printer: PrinterProfile, header_lines: tuple[str, ...] = ()) -> OperatorDialog:
    """Build the "start next job?" dialog for a printer.

    Args:
        printer: The printer.
        header_lines: Lines shown first (e.g. the result of a review).

    Returns:
        A dialog offering Start/Later for the next waiting entry, or a
        "Printer busy" / "Queue empty" dialog with only Close.
    """
    busy = busy_entry_for_printer(printer)
    if busy is not None:
        return _info_dialog(*header_lines, "Printer busy", _busy_line(busy))

    entry = next_waiting_entry(printer)
    if entry is None:
        return _info_dialog(*header_lines, "Queue empty")

    plate = entry.plate
    job = plate.print_job
    lines = [*header_lines, f"Next: {_job_label(job)}, plate {plate.plate_number}", filament_summary(job)]
    if plate.print_time_estimate:
        lines.append(f"Est. time: {duration_format(plate.print_time_estimate)}")
    return _info_dialog(
        *lines,
        buttons=(
            DialogButton("Start", StartNext(entry.pk), "primary"),
            DialogButton("Later", Dismiss(), "secondary"),
        ),
    )


def pending_review_entry(printer: PrinterProfile) -> PrintQueue | None:
    """Return the printer's entry awaiting review, if any."""
    return (
        PrintQueue.objects.filter(printer=printer, status=PrintQueue.STATUS_AWAITING_REVIEW)
        .select_related("plate__print_job")
        .order_by("completed_at", "pk")
        .first()
    )


def menu_dialog(printer: PrinterProfile) -> OperatorDialog:
    """Build the dialog that fits the printer's state (pending review first).

    Args:
        printer: The printer.

    Returns:
        The review dialog if an entry awaits review, else the next-job dialog.
    """
    entry = pending_review_entry(printer)
    if entry is not None:
        return review_dialog(entry)
    return next_job_dialog(printer)


def _review_result_line(result_outcome: str, plates_completed: int, plate_count: int, retry: str) -> str:
    if result_outcome == "passed":
        return f"Plate passed ({plates_completed}/{plate_count} plates done)"
    if result_outcome == "requeued":
        return f"Failed - requeued (retry {retry})"
    return "Failed - max retries reached, removed from queue"


def _handle_review(printer: PrinterProfile, action: Review) -> OperatorDialog:
    try:
        result = review_queue_entry(action.entry_pk, action.passed, printer_pk=printer.pk)
    except ReviewStateError as exc:
        logger.info("Review of queue entry %s from printer %s ignored: %s", action.entry_pk, printer.name, exc)
        return _info_dialog(ALREADY_HANDLED)
    line = _review_result_line(
        result.outcome,
        result.plates_completed,
        result.plate_count,
        f"{result.retry_count}/{result.max_retries}",
    )
    return next_job_dialog(printer, (line,))


def _handle_start(printer: PrinterProfile, action: StartNext) -> OperatorDialog:
    result = start_next_for_printer(printer, expected_entry_pk=action.entry_pk)
    if result.outcome == "started":
        plate = result.entry.plate
        return _info_dialog(f"Started: {_job_label(plate.print_job)}, plate {plate.plate_number}")
    if result.outcome == "busy":
        return _info_dialog(ALREADY_HANDLED, "Printer busy", _busy_line(result.busy_entry))
    if result.outcome == "empty":
        return _info_dialog(ALREADY_HANDLED, "Queue empty")
    if result.outcome == "not_next":
        return _info_dialog(
            ALREADY_HANDLED,
            "The queue has changed.",
            buttons=(DialogButton("Show next", ShowMenu(), "primary"), CLOSE_BUTTON),
        )
    if result.outcome == "not_configured":
        return _info_dialog("Start failed", "No printer connection configured.")
    return _info_dialog("Start failed", result.error)


def handle_action(printer: PrinterProfile, action: OperatorAction) -> OperatorDialog | None:
    """Apply an operator action triggered on ``printer`` and return the follow-up dialog.

    Every action is re-validated against the database: a review only applies
    to an entry of this printer that still awaits review, a start only to the
    entry that is still next on a free printer.  Stale or double presses
    change nothing and yield an "Already handled" dialog.  A print is only
    ever started by an explicit :class:`StartNext` action.

    Args:
        printer: The printer the action came from.
        action: The abstract action.

    Returns:
        The dialog to show next, or ``None`` to close the dialog.
    """
    logger.info("Operator action %r from printer %s", action, printer.name)
    if isinstance(action, Review):
        return _handle_review(printer, action)
    if isinstance(action, StartNext):
        return _handle_start(printer, action)
    if isinstance(action, ShowMenu):
        return menu_dialog(printer)
    return None
