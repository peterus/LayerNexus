"""Comprehensive tests for the print queue service functions."""

from unittest.mock import MagicMock, patch

from django.test import TestCase

from core.models import PrintJob, PrintJobPlate, PrintQueue
from core.services.moonraker import MoonrakerError
from core.services.queue import (
    PrintStartError,
    ReviewStateError,
    review_queue_entry,
    start_next_for_printer,
    start_print_for_queue_entry,
)
from core.tests.mixins import TestDataMixin


class StartPrintForQueueEntryTests(TestDataMixin, TestCase):
    """Tests for start_print_for_queue_entry()."""

    def setUp(self):
        super().setUp()
        self.job = PrintJob.objects.create(
            name="Test Job",
            status=PrintJob.STATUS_SLICED,
            created_by=self.user,
        )
        self.plate = PrintJobPlate.objects.create(
            print_job=self.job,
            plate_number=1,
            status=PrintJobPlate.STATUS_WAITING,
        )
        # Ensure the printer has Moonraker settings
        self.printer.moonraker_url = "http://printer:7125"
        self.printer.moonraker_api_key = "test-key"
        self.printer.save()

        self.entry = PrintQueue.objects.create(
            plate=self.plate,
            printer=self.printer,
            status=PrintQueue.STATUS_WAITING,
            priority=2,
        )

    def test_no_gcode_file_raises_print_start_error(self):
        """Entry without G-code file should raise PrintStartError."""
        # plate.gcode_file is empty by default
        with self.assertRaises(PrintStartError) as ctx:
            start_print_for_queue_entry(self.entry)

        self.assertIn("No G-code file", str(ctx.exception))

    @patch("core.services.queue.get_printer_backend")
    def test_success(self, mock_get_backend):
        """Successful print start updates all statuses."""
        # Set up a mock gcode file
        mock_gcode = MagicMock()
        mock_gcode.path = "/test.gcode"
        mock_gcode.__bool__ = MagicMock(return_value=True)
        self.plate.gcode_file = mock_gcode
        self.plate.save = MagicMock()  # Prevent actual save
        # We need to reload the entry to get the plate with the mock
        # Instead, directly set on the entry's plate
        self.entry.plate = self.plate

        mock_backend = MagicMock()
        mock_get_backend.return_value = mock_backend

        # Mock save methods to avoid DB issues with the mock file
        with patch.object(PrintQueue, "save"), patch.object(PrintJobPlate, "save"), patch.object(PrintJob, "save"):
            remote_filename = start_print_for_queue_entry(self.entry)

        # Verify factory was called with the printer
        mock_get_backend.assert_called_once_with(self.printer)

        # Verify upload and start were called
        mock_backend.upload_gcode.assert_called_once()
        mock_backend.start_print.assert_called_once_with(remote_filename)

        # Verify remote filename format
        self.assertTrue(remote_filename.startswith("LN_"))
        self.assertTrue(remote_filename.endswith(".gcode"))
        self.assertIn("_p1", remote_filename)

    @patch("core.services.queue.get_printer_backend")
    def test_backend_upload_error(self, mock_get_backend):
        """PrinterError during upload should propagate."""
        mock_gcode = MagicMock()
        mock_gcode.path = "/test.gcode"
        mock_gcode.__bool__ = MagicMock(return_value=True)
        self.plate.gcode_file = mock_gcode
        self.entry.plate = self.plate

        mock_backend = MagicMock()
        mock_backend.upload_gcode.side_effect = MoonrakerError("Connection refused")
        mock_get_backend.return_value = mock_backend

        with self.assertRaises(MoonrakerError):
            start_print_for_queue_entry(self.entry)

    @patch("core.services.queue.get_printer_backend")
    def test_backend_start_error(self, mock_get_backend):
        """PrinterError during start_print should propagate."""
        mock_gcode = MagicMock()
        mock_gcode.path = "/test.gcode"
        mock_gcode.__bool__ = MagicMock(return_value=True)
        self.plate.gcode_file = mock_gcode
        self.entry.plate = self.plate

        mock_backend = MagicMock()
        mock_backend.upload_gcode.return_value = {"item": {"path": "test.gcode"}}
        mock_backend.start_print.side_effect = MoonrakerError("Printer busy")
        mock_get_backend.return_value = mock_backend

        with self.assertRaises(MoonrakerError):
            start_print_for_queue_entry(self.entry)

    @patch("core.services.queue.get_printer_backend")
    def test_status_updates_on_success(self, mock_get_backend):
        """Verify that queue entry, plate, and job statuses are updated."""
        mock_gcode = MagicMock()
        mock_gcode.path = "/test.gcode"
        mock_gcode.__bool__ = MagicMock(return_value=True)
        self.plate.gcode_file = mock_gcode
        self.entry.plate = self.plate

        mock_backend = MagicMock()
        mock_get_backend.return_value = mock_backend

        # Use a mock for save to track calls but also check field assignments
        entry_save = MagicMock()
        plate_save = MagicMock()
        job_save = MagicMock()

        with (
            patch.object(PrintQueue, "save", entry_save),
            patch.object(PrintJobPlate, "save", plate_save),
            patch.object(PrintJob, "save", job_save),
        ):
            start_print_for_queue_entry(self.entry)

        # Check status was set to printing
        self.assertEqual(self.entry.status, PrintQueue.STATUS_PRINTING)
        self.assertIsNotNone(self.entry.started_at)

        self.assertEqual(self.plate.status, PrintJobPlate.STATUS_PRINTING)
        self.assertIsNotNone(self.plate.started_at)
        self.assertTrue(self.plate.remote_job_id.startswith("LN_"))

        self.assertEqual(self.job.status, PrintJob.STATUS_PRINTING)
        self.assertIsNotNone(self.job.started_at)

        # Verify save was called on each
        entry_save.assert_called_once()
        plate_save.assert_called_once()
        job_save.assert_called_once()


class _QueueServiceTestBase(TestDataMixin, TestCase):
    """Shared fixtures: one job with two plates queued on a Moonraker printer."""

    def setUp(self):
        super().setUp()
        self.printer.moonraker_url = "http://printer:7125"
        self.printer.save()
        self.job = PrintJob.objects.create(name="Bracket", status=PrintJob.STATUS_PRINTING, created_by=self.user)
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
        )

    def _entry(self, plate: PrintJobPlate, status: str, **kwargs) -> PrintQueue:
        return PrintQueue.objects.create(plate=plate, printer=self.printer, status=status, **kwargs)


class ReviewQueueEntryTests(_QueueServiceTestBase):
    """Tests for review_queue_entry()."""

    def test_pass_completes_plate_and_deletes_entry(self):
        entry = self._entry(self.plate1, PrintQueue.STATUS_AWAITING_REVIEW)
        result = review_queue_entry(entry.pk, True)
        self.assertEqual(result.outcome, "passed")
        self.assertEqual(result.plates_completed, 1)
        self.assertEqual(result.plate_count, 2)
        self.assertEqual(result.plate_label, "Plate 1 of 'Bracket (Printing)'")
        self.plate1.refresh_from_db()
        self.assertEqual(self.plate1.status, PrintJobPlate.STATUS_COMPLETED)
        self.assertIsNotNone(self.plate1.completed_at)
        self.assertFalse(PrintQueue.objects.filter(pk=entry.pk).exists())
        self.job.refresh_from_db()
        self.assertEqual(self.job.status, PrintJob.STATUS_PRINTING)

    def test_pass_last_plate_completes_job(self):
        self.plate2.status = PrintJobPlate.STATUS_COMPLETED
        self.plate2.save()
        entry = self._entry(self.plate1, PrintQueue.STATUS_AWAITING_REVIEW)
        result = review_queue_entry(entry.pk, True)
        self.assertEqual(result.plates_completed, 2)
        self.job.refresh_from_db()
        self.assertEqual(self.job.status, PrintJob.STATUS_COMPLETED)
        self.assertIsNotNone(self.job.completed_at)

    def test_fail_requeues_with_position_zero(self):
        entry = self._entry(self.plate1, PrintQueue.STATUS_AWAITING_REVIEW, position=5)
        result = review_queue_entry(entry.pk, False)
        self.assertEqual(result.outcome, "requeued")
        self.assertEqual((result.retry_count, result.max_retries), (1, 3))
        entry.refresh_from_db()
        self.assertEqual(entry.status, PrintQueue.STATUS_WAITING)
        self.assertEqual(entry.retry_count, 1)
        self.assertEqual(entry.position, 0)
        self.assertIsNone(entry.started_at)
        self.plate1.refresh_from_db()
        self.assertEqual(self.plate1.status, PrintJobPlate.STATUS_WAITING)

    def test_fail_at_max_retries_discards(self):
        entry = self._entry(self.plate1, PrintQueue.STATUS_AWAITING_REVIEW, retry_count=2, max_retries=3)
        result = review_queue_entry(entry.pk, False)
        self.assertEqual(result.outcome, "discarded")
        self.assertEqual(result.max_retries, 3)
        self.plate1.refresh_from_db()
        self.assertEqual(self.plate1.status, PrintJobPlate.STATUS_FAILED)
        self.assertFalse(PrintQueue.objects.filter(pk=entry.pk).exists())

    def test_not_awaiting_review_raises(self):
        entry = self._entry(self.plate1, PrintQueue.STATUS_WAITING)
        with self.assertRaises(ReviewStateError):
            review_queue_entry(entry.pk, True)
        entry.refresh_from_db()
        self.assertEqual(entry.status, PrintQueue.STATUS_WAITING)

    def test_missing_entry_raises(self):
        with self.assertRaises(ReviewStateError):
            review_queue_entry(999999, True)

    def test_other_printer_raises(self):
        entry = self._entry(self.plate1, PrintQueue.STATUS_AWAITING_REVIEW)
        with self.assertRaises(ReviewStateError):
            review_queue_entry(entry.pk, True, printer_pk=self.other_printer.pk)
        entry.refresh_from_db()
        self.assertEqual(entry.status, PrintQueue.STATUS_AWAITING_REVIEW)

    def test_matching_printer_is_accepted(self):
        entry = self._entry(self.plate1, PrintQueue.STATUS_AWAITING_REVIEW)
        result = review_queue_entry(entry.pk, True, printer_pk=self.printer.pk)
        self.assertEqual(result.outcome, "passed")


@patch("core.services.queue.get_printer_backend")
class StartNextForPrinterTests(_QueueServiceTestBase):
    """Tests for start_next_for_printer()."""

    def setUp(self):
        super().setUp()
        self.plate1.status = PrintJobPlate.STATUS_WAITING
        self.plate1.save()

    def test_start_next_starts_first_waiting(self, mock_get_backend):
        first = self._entry(self.plate1, PrintQueue.STATUS_WAITING, position=1)
        self._entry(self.plate2, PrintQueue.STATUS_WAITING, position=2)
        result = start_next_for_printer(self.printer)
        self.assertEqual(result.outcome, "started")
        self.assertEqual(result.entry.pk, first.pk)
        mock_get_backend.return_value.start_print.assert_called_once()
        first.refresh_from_db()
        self.assertEqual(first.status, PrintQueue.STATUS_PRINTING)

    def test_start_next_with_matching_expected_entry(self, mock_get_backend):
        first = self._entry(self.plate1, PrintQueue.STATUS_WAITING)
        result = start_next_for_printer(self.printer, expected_entry_pk=first.pk)
        self.assertEqual(result.outcome, "started")

    def test_start_next_busy(self, mock_get_backend):
        for busy_status in (PrintQueue.STATUS_PRINTING, PrintQueue.STATUS_AWAITING_REVIEW):
            with self.subTest(busy_status=busy_status):
                PrintQueue.objects.all().delete()
                busy = self._entry(self.plate1, busy_status)
                self._entry(self.plate2, PrintQueue.STATUS_WAITING)
                result = start_next_for_printer(self.printer)
                self.assertEqual(result.outcome, "busy")
                self.assertEqual(result.busy_entry.pk, busy.pk)
        mock_get_backend.return_value.start_print.assert_not_called()

    def test_start_next_empty(self, mock_get_backend):
        result = start_next_for_printer(self.printer)
        self.assertEqual(result.outcome, "empty")
        self.assertIsNone(result.entry)
        mock_get_backend.assert_not_called()

    def test_start_next_not_next(self, mock_get_backend):
        self._entry(self.plate1, PrintQueue.STATUS_WAITING, position=1)
        second = self._entry(self.plate2, PrintQueue.STATUS_WAITING, position=2)
        result = start_next_for_printer(self.printer, expected_entry_pk=second.pk)
        self.assertEqual(result.outcome, "not_next")
        mock_get_backend.return_value.start_print.assert_not_called()
        second.refresh_from_db()
        self.assertEqual(second.status, PrintQueue.STATUS_WAITING)

    def test_start_next_not_configured(self, mock_get_backend):
        self.printer.moonraker_url = ""
        self.printer.save()
        self._entry(self.plate1, PrintQueue.STATUS_WAITING)
        result = start_next_for_printer(self.printer)
        self.assertEqual(result.outcome, "not_configured")
        mock_get_backend.assert_not_called()

    def test_start_next_printer_error(self, mock_get_backend):
        mock_get_backend.return_value.upload_gcode.side_effect = MoonrakerError("Connection refused")
        entry = self._entry(self.plate1, PrintQueue.STATUS_WAITING)
        with self.assertLogs("core.services.queue", level="ERROR"):
            result = start_next_for_printer(self.printer)
        self.assertEqual(result.outcome, "error")
        self.assertEqual(result.error, "Failed to start print. Please check the printer connection and try again.")
        entry.refresh_from_db()
        self.assertEqual(entry.status, PrintQueue.STATUS_WAITING)

    def test_start_next_print_start_error(self, mock_get_backend):
        self.plate1.gcode_file = ""
        self.plate1.save()
        self._entry(self.plate1, PrintQueue.STATUS_WAITING)
        result = start_next_for_printer(self.printer)
        self.assertEqual(result.outcome, "no_gcode")
        self.assertEqual(result.error, "No G-code file for plate 1. Slice the job first.")
        mock_get_backend.assert_not_called()

    def test_start_next_db_error_after_start_keeps_claim(self, mock_get_backend):
        """If the printer already accepted the start, a later DB error must not re-open the entry."""
        entry = self._entry(self.plate1, PrintQueue.STATUS_WAITING)
        with (
            patch.object(PrintJobPlate, "save", side_effect=RuntimeError("database is locked")),
            self.assertRaises(RuntimeError),
        ):
            start_next_for_printer(self.printer)
        mock_get_backend.return_value.start_print.assert_called_once()
        entry.refresh_from_db()
        self.assertEqual(entry.status, PrintQueue.STATUS_PRINTING)

    def test_run_all_claims_entry_during_upload(self, mock_get_backend):
        """Run All uses the same claim: a printer Start during its upload sees the printer busy."""
        from django.contrib.auth.models import Group
        from django.urls import reverse

        entry = self._entry(self.plate1, PrintQueue.STATUS_WAITING)
        concurrent: list = []

        def upload(*args, **kwargs):
            concurrent.append(start_next_for_printer(self.printer, expected_entry_pk=entry.pk))

        mock_get_backend.return_value.upload_gcode.side_effect = upload
        self.user.groups.add(Group.objects.get(name="Admin"))
        self.client.login(username="testuser", password="testpass123")
        with self.settings(ALLOWED_HOSTS=["testserver"]):
            self.client.post(reverse("core:run_all_queues"))

        self.assertEqual(concurrent[0].outcome, "busy")
        mock_get_backend.return_value.start_print.assert_called_once()

    def test_start_next_claims_entry_during_upload(self, mock_get_backend):
        """A concurrent start while the G-code uploads sees the printer busy (no double start)."""
        entry = self._entry(self.plate1, PrintQueue.STATUS_WAITING)
        concurrent: list = []

        def upload(*args, **kwargs):
            concurrent.append(start_next_for_printer(self.printer))

        mock_get_backend.return_value.upload_gcode.side_effect = upload
        result = start_next_for_printer(self.printer, expected_entry_pk=entry.pk)

        self.assertEqual(result.outcome, "started")
        self.assertEqual(concurrent[0].outcome, "busy")
        self.assertEqual(concurrent[0].busy_entry.pk, entry.pk)
        mock_get_backend.return_value.start_print.assert_called_once()
        entry.refresh_from_db()
        self.assertEqual(entry.status, PrintQueue.STATUS_PRINTING)
        self.assertIsNone(entry.start_claimed_at)

    def test_claimed_entry_stays_waiting_during_upload(self, mock_get_backend):
        """During the upload the entry is still waiting (no early Cancel / status sync), only claimed."""
        from core.services.queue import queue_entry_status_label

        entry = self._entry(self.plate1, PrintQueue.STATUS_WAITING)
        seen: list = []

        def upload(*args, **kwargs):
            seen.append(PrintQueue.objects.get(pk=entry.pk))

        mock_get_backend.return_value.upload_gcode.side_effect = upload
        start_next_for_printer(self.printer)
        self.assertEqual(seen[0].status, PrintQueue.STATUS_WAITING)
        self.assertIsNotNone(seen[0].start_claimed_at)
        self.assertEqual(queue_entry_status_label(seen[0]), "Starting")

    def test_expired_claim_does_not_block(self, mock_get_backend):
        """A claim left behind by a crashed process expires."""
        from datetime import timedelta

        from django.utils import timezone

        entry = self._entry(
            self.plate1, PrintQueue.STATUS_WAITING, start_claimed_at=timezone.now() - timedelta(minutes=11)
        )
        result = start_next_for_printer(self.printer)
        self.assertEqual(result.outcome, "started")
        self.assertEqual(result.entry.pk, entry.pk)

    def test_start_next_failure_releases_claim(self, mock_get_backend):
        mock_get_backend.return_value.start_print.side_effect = MoonrakerError("busy")
        entry = self._entry(self.plate1, PrintQueue.STATUS_WAITING)
        with self.assertLogs("core.services.queue", level="ERROR"):
            start_next_for_printer(self.printer)
        entry.refresh_from_db()
        self.assertEqual(entry.status, PrintQueue.STATUS_WAITING)
        self.assertIsNone(entry.started_at)
        self.assertIsNone(entry.start_claimed_at)

    def test_start_next_unexpected_error_before_start_releases_claim(self, mock_get_backend):
        """A non-printer error before the printer accepted the start hands the entry back."""
        mock_get_backend.return_value.upload_gcode.side_effect = FileNotFoundError("gcode gone")
        entry = self._entry(self.plate1, PrintQueue.STATUS_WAITING)
        with self.assertRaises(FileNotFoundError):
            start_next_for_printer(self.printer)
        mock_get_backend.return_value.start_print.assert_not_called()
        entry.refresh_from_db()
        self.assertEqual(entry.status, PrintQueue.STATUS_WAITING)
        self.assertIsNone(entry.started_at)
        self.assertIsNone(entry.start_claimed_at)


class IsPrinterBusyClaimTests(_QueueServiceTestBase):
    """PrintQueue.is_printer_busy (list view) honours an unexpired start claim."""

    def test_claim_makes_printer_busy_until_expired(self):
        from datetime import timedelta

        from django.utils import timezone

        claimed = self._entry(self.plate1, PrintQueue.STATUS_WAITING, start_claimed_at=timezone.now())
        other = self._entry(self.plate2, PrintQueue.STATUS_WAITING)
        self.assertTrue(claimed.is_printer_busy)
        self.assertTrue(other.is_printer_busy)

        PrintQueue.objects.filter(pk=claimed.pk).update(start_claimed_at=timezone.now() - timedelta(minutes=11))
        self.assertFalse(other.is_printer_busy)
