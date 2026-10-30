"""Tests for the printer-agnostic operator dialog model."""

import inspect
from datetime import timedelta
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from core.models import Part, PrintJob, PrintJobPart, PrintJobPlate, PrintQueue
from core.services import operator_dialog
from core.services.moonraker import MoonrakerError
from core.services.operator_dialog import (
    DIALOG_TITLE,
    Dismiss,
    OperatorDialog,
    Review,
    ShowMenu,
    StartNext,
    filament_summary,
    handle_action,
    menu_dialog,
    next_job_dialog,
    review_dialog,
)
from core.tests.mixins import TestDataMixin, queue_state_fields


def _actions(dialog: OperatorDialog) -> list:
    return [button.action for button in dialog.buttons]


class _DialogTestBase(TestDataMixin, TestCase):
    """One printing job with two plates on a Moonraker printer."""

    def setUp(self):
        super().setUp()
        self.printer.moonraker_url = "http://printer:7125"
        self.printer.operator_prompts_enabled = True
        self.printer.save()
        self.job = PrintJob.objects.create(name="Bracket", status=PrintJob.STATUS_PRINTING, created_by=self.user)
        PrintJobPart.objects.create(print_job=self.job, part=self.part, quantity=2)
        self.plate1 = PrintJobPlate.objects.create(
            print_job=self.job,
            plate_number=1,
            status=PrintJobPlate.STATUS_PRINTING,
            gcode_file="gcode_jobs/p1.gcode",
        )
        self.plate2 = PrintJobPlate.objects.create(
            print_job=self.job,
            plate_number=2,
            status=PrintJobPlate.STATUS_WAITING,
            gcode_file="gcode_jobs/p2.gcode",
            print_time_estimate=timedelta(hours=1, minutes=5),
        )

    def _entry(self, plate: PrintJobPlate, status: str, **kwargs) -> PrintQueue:
        return PrintQueue.objects.create(plate=plate, printer=self.printer, **queue_state_fields(status), **kwargs)


class FilamentSummaryTests(_DialogTestBase):
    def test_filament_summary_single(self):
        self.assertEqual(filament_summary(self.job), "Filament: PLA red")

    def test_filament_summary_multiple_distinct(self):
        same = Part.objects.create(project=self.project, name="Same", material="PLA", color="red")
        other = Part.objects.create(project=self.project, name="Other", material="PETG", color="Black")
        PrintJobPart.objects.create(print_job=self.job, part=same)
        PrintJobPart.objects.create(print_job=self.job, part=other)
        self.assertEqual(filament_summary(self.job), "Filament: PETG Black, PLA red")

    def test_filament_summary_none(self):
        job = PrintJob.objects.create(name="Empty", created_by=self.user)
        bare = Part.objects.create(project=self.project, name="Bare")
        PrintJobPart.objects.create(print_job=job, part=bare)
        self.assertEqual(filament_summary(job), "Filament: not specified")


class DialogBuilderTests(_DialogTestBase):
    def test_review_dialog_buttons(self):
        entry = self._entry(self.plate1, PrintQueue.STATE_AWAITING_REVIEW)
        dialog = review_dialog(entry)
        self.assertEqual(dialog.title, DIALOG_TITLE)
        self.assertEqual(dialog.lines, ("Bracket", "Plate 1 of 2"))
        self.assertEqual(_actions(dialog), [Review(entry.pk, True), Review(entry.pk, False)])
        self.assertEqual([b.label for b in dialog.buttons], ["Good", "Bad"])
        self.assertEqual(dialog.buttons[1].style, "error")

    def test_review_dialog_shows_error_state(self):
        entry = self._entry(self.plate1, PrintQueue.STATE_AWAITING_REVIEW, last_error="Printer reported: cancelled")
        dialog = review_dialog(entry)
        self.assertIn("Ended: Printer reported: cancelled", dialog.lines)

    def test_next_job_dialog_shows_filament_and_start(self):
        entry = self._entry(self.plate2, PrintQueue.STATE_WAITING)
        dialog = next_job_dialog(self.printer, ("Header",))
        self.assertEqual(
            dialog.lines,
            ("Header", "Next: Bracket, plate 2", "Filament: PLA red", "Est. time: 1h 5m"),
        )
        self.assertEqual(_actions(dialog), [StartNext(entry.pk), Dismiss()])
        self.assertEqual([b.label for b in dialog.buttons], ["Start", "Later"])

    def test_next_job_dialog_queue_empty(self):
        dialog = next_job_dialog(self.printer)
        self.assertEqual(dialog.lines, ("Queue empty",))
        self.assertEqual(_actions(dialog), [Dismiss()])

    def test_next_job_dialog_busy(self):
        self._entry(self.plate1, PrintQueue.STATE_PRINTING)
        self._entry(self.plate2, PrintQueue.STATE_WAITING)
        dialog = next_job_dialog(self.printer)
        self.assertEqual(dialog.lines[0], "Printer busy")
        self.assertEqual(_actions(dialog), [Dismiss()])

    def test_menu_dialog_prefers_pending_review(self):
        entry = self._entry(self.plate1, PrintQueue.STATE_AWAITING_REVIEW)
        self._entry(self.plate2, PrintQueue.STATE_WAITING)
        dialog = menu_dialog(self.printer)
        self.assertEqual(_actions(dialog), [Review(entry.pk, True), Review(entry.pk, False)])

    def test_menu_dialog_without_review_shows_next_job(self):
        entry = self._entry(self.plate2, PrintQueue.STATE_WAITING)
        self.assertIn(StartNext(entry.pk), _actions(menu_dialog(self.printer)))

    def test_module_has_no_moonraker_imports(self):
        source = inspect.getsource(operator_dialog).lower()
        self.assertNotIn("moonraker", source)
        self.assertNotIn("klipper", source)
        self.assertNotIn("gcode_script", source)


@patch("core.services.queue.get_printer_backend")
class HandleActionTests(_DialogTestBase):
    def test_handle_review_pass_returns_next_job_dialog(self, mock_get_backend):
        self.plate2.status = PrintJobPlate.STATUS_COMPLETED
        self.plate2.save()
        other_plate = PrintJobPlate.objects.create(
            print_job=PrintJob.objects.create(name="Clip", status=PrintJob.STATUS_SLICED, created_by=self.user),
            plate_number=1,
            gcode_file="gcode_jobs/c1.gcode",
        )
        nxt = self._entry(other_plate, PrintQueue.STATE_WAITING)
        entry = self._entry(self.plate1, PrintQueue.STATE_AWAITING_REVIEW)

        dialog = handle_action(self.printer, Review(entry.pk, True))

        self.assertEqual(dialog.lines[0], "Plate passed (2/2 plates done)")
        self.assertIn(StartNext(nxt.pk), _actions(dialog))
        mock_get_backend.assert_not_called()
        nxt.refresh_from_db()
        self.assertEqual(nxt.state, PrintQueue.STATE_WAITING)
        self.plate1.refresh_from_db()
        self.assertEqual(self.plate1.status, PrintJobPlate.STATUS_COMPLETED)

    def test_handle_review_fail_requeues(self, mock_get_backend):
        entry = self._entry(self.plate1, PrintQueue.STATE_AWAITING_REVIEW)
        dialog = handle_action(self.printer, Review(entry.pk, False))
        self.assertEqual(dialog.lines[0], "Failed - requeued (retry 1/3)")
        retry = PrintQueue.objects.open().get(plate=self.plate1)
        self.assertIn(StartNext(retry.pk), _actions(dialog))
        mock_get_backend.assert_not_called()

    def test_handle_review_fail_discards(self, mock_get_backend):
        failed = {"result": PrintQueue.RESULT_FAILED, "resolved_at": timezone.now()}
        self._entry(self.plate1, PrintQueue.STATE_AWAITING_REVIEW, **failed)
        self._entry(self.plate1, PrintQueue.STATE_AWAITING_REVIEW, is_retry=True, **failed)
        entry = self._entry(self.plate1, PrintQueue.STATE_AWAITING_REVIEW, is_retry=True)
        dialog = handle_action(self.printer, Review(entry.pk, False))
        self.assertEqual(dialog.lines[0], "Failed - max retries reached, plate failed")

    def test_handle_review_twice_already_handled(self, mock_get_backend):
        entry = self._entry(self.plate1, PrintQueue.STATE_AWAITING_REVIEW)
        handle_action(self.printer, Review(entry.pk, True))
        self.plate1.refresh_from_db()
        completed_at = self.plate1.completed_at

        dialog = handle_action(self.printer, Review(entry.pk, False))

        self.assertEqual(dialog.lines[0], "Already handled")
        self.assertEqual(_actions(dialog), [ShowMenu(), Dismiss()])
        self.plate1.refresh_from_db()
        self.assertEqual(self.plate1.status, PrintJobPlate.STATUS_COMPLETED)
        self.assertEqual(self.plate1.completed_at, completed_at)

    def test_handle_review_other_printer(self, mock_get_backend):
        entry = self._entry(self.plate1, PrintQueue.STATE_AWAITING_REVIEW)
        dialog = handle_action(self.other_printer, Review(entry.pk, True))
        self.assertEqual(dialog.lines[0], "Already handled")
        entry.refresh_from_db()
        self.assertEqual(entry.state, PrintQueue.STATE_AWAITING_REVIEW)

    def test_handle_start_next_started(self, mock_get_backend):
        entry = self._entry(self.plate2, PrintQueue.STATE_WAITING)
        dialog = handle_action(self.printer, StartNext(entry.pk))
        self.assertEqual(dialog.lines, ("Started: Bracket, plate 2",))
        self.assertEqual(_actions(dialog), [Dismiss()])
        mock_get_backend.return_value.start_print.assert_called_once()
        entry.refresh_from_db()
        self.assertEqual(entry.state, PrintQueue.STATE_PRINTING)

    def test_handle_start_next_error_shows_message(self, mock_get_backend):
        mock_get_backend.return_value.upload_gcode.side_effect = MoonrakerError("boom")
        entry = self._entry(self.plate2, PrintQueue.STATE_WAITING)
        with self.assertLogs("core.services.queue", level="ERROR"):
            dialog = handle_action(self.printer, StartNext(entry.pk))
        self.assertEqual(dialog.lines[0], "Start failed")
        self.assertIn("Failed to start print. Please check the printer connection and try again.", dialog.lines)
        entry.refresh_from_db()
        self.assertEqual(entry.state, PrintQueue.STATE_WAITING)

    def test_handle_start_next_not_next(self, mock_get_backend):
        self._entry(self.plate1, PrintQueue.STATE_WAITING, priority=4)
        stale = self._entry(self.plate2, PrintQueue.STATE_WAITING)
        dialog = handle_action(self.printer, StartNext(stale.pk))
        self.assertEqual(dialog.lines[0], "Already handled")
        self.assertIn(ShowMenu(), _actions(dialog))
        mock_get_backend.assert_not_called()

    def test_handle_start_next_busy(self, mock_get_backend):
        self._entry(self.plate1, PrintQueue.STATE_PRINTING)
        entry = self._entry(self.plate2, PrintQueue.STATE_WAITING)
        dialog = handle_action(self.printer, StartNext(entry.pk))
        self.assertEqual(dialog.lines[:2], ("Already handled", "Printer busy"))
        self.assertIn(ShowMenu(), _actions(dialog))
        mock_get_backend.assert_not_called()

    def test_handle_start_next_empty(self, mock_get_backend):
        dialog = handle_action(self.printer, StartNext(424242))
        self.assertEqual(dialog.lines, ("Already handled", "Queue empty"))

    def test_handle_dismiss_returns_none(self, mock_get_backend):
        self.assertIsNone(handle_action(self.printer, Dismiss()))

    def test_handle_show_menu(self, mock_get_backend):
        entry = self._entry(self.plate1, PrintQueue.STATE_AWAITING_REVIEW)
        dialog = handle_action(self.printer, ShowMenu())
        self.assertIn(Review(entry.pk, True), _actions(dialog))

    def test_actions_are_logged_with_printer_name(self, mock_get_backend):
        with self.assertLogs("core.services.operator_dialog", level="INFO") as logs:
            handle_action(self.printer, Dismiss())
        self.assertIn("Test Printer", logs.output[0])
