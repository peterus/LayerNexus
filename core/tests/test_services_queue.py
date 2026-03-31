"""Comprehensive tests for the print queue service functions."""

from unittest.mock import MagicMock, patch

from django.test import TestCase

from core.models import PrintJob, PrintJobPlate, PrintQueue, PrinterProfile, Project
from core.services.moonraker import MoonrakerError
from core.services.queue import PrintStartError, start_print_for_queue_entry
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

    @patch("core.services.queue.MoonrakerClient")
    def test_success(self, MockClientCls):
        """Successful print start updates all statuses."""
        # Set up a mock gcode file
        mock_gcode = MagicMock()
        mock_gcode.path = "/tmp/test.gcode"
        mock_gcode.__bool__ = MagicMock(return_value=True)
        self.plate.gcode_file = mock_gcode
        self.plate.save = MagicMock()  # Prevent actual save
        # We need to reload the entry to get the plate with the mock
        # Instead, directly set on the entry's plate
        self.entry.plate = self.plate

        mock_client = MagicMock()
        MockClientCls.return_value = mock_client

        # Mock save methods to avoid DB issues with the mock file
        with patch.object(PrintQueue, "save"):
            with patch.object(PrintJobPlate, "save"):
                with patch.object(PrintJob, "save"):
                    remote_filename = start_print_for_queue_entry(self.entry)

        # Verify MoonrakerClient was constructed with correct args
        MockClientCls.assert_called_once_with("http://printer:7125", "test-key")

        # Verify upload and start were called
        mock_client.upload_gcode.assert_called_once()
        mock_client.start_print.assert_called_once_with(remote_filename)

        # Verify remote filename format
        self.assertTrue(remote_filename.startswith("LN_"))
        self.assertTrue(remote_filename.endswith(".gcode"))
        self.assertIn("_p1", remote_filename)

    @patch("core.services.queue.MoonrakerClient")
    def test_moonraker_upload_error(self, MockClientCls):
        """MoonrakerError during upload should propagate."""
        mock_gcode = MagicMock()
        mock_gcode.path = "/tmp/test.gcode"
        mock_gcode.__bool__ = MagicMock(return_value=True)
        self.plate.gcode_file = mock_gcode
        self.entry.plate = self.plate

        mock_client = MagicMock()
        mock_client.upload_gcode.side_effect = MoonrakerError("Connection refused")
        MockClientCls.return_value = mock_client

        with self.assertRaises(MoonrakerError):
            start_print_for_queue_entry(self.entry)

    @patch("core.services.queue.MoonrakerClient")
    def test_moonraker_start_error(self, MockClientCls):
        """MoonrakerError during start_print should propagate."""
        mock_gcode = MagicMock()
        mock_gcode.path = "/tmp/test.gcode"
        mock_gcode.__bool__ = MagicMock(return_value=True)
        self.plate.gcode_file = mock_gcode
        self.entry.plate = self.plate

        mock_client = MagicMock()
        mock_client.upload_gcode.return_value = {"item": {"path": "test.gcode"}}
        mock_client.start_print.side_effect = MoonrakerError("Printer busy")
        MockClientCls.return_value = mock_client

        with self.assertRaises(MoonrakerError):
            start_print_for_queue_entry(self.entry)

    @patch("core.services.queue.MoonrakerClient")
    def test_status_updates_on_success(self, MockClientCls):
        """Verify that queue entry, plate, and job statuses are updated."""
        mock_gcode = MagicMock()
        mock_gcode.path = "/tmp/test.gcode"
        mock_gcode.__bool__ = MagicMock(return_value=True)
        self.plate.gcode_file = mock_gcode
        self.entry.plate = self.plate

        mock_client = MagicMock()
        MockClientCls.return_value = mock_client

        # Use a mock for save to track calls but also check field assignments
        entry_save = MagicMock()
        plate_save = MagicMock()
        job_save = MagicMock()

        with patch.object(PrintQueue, "save", entry_save):
            with patch.object(PrintJobPlate, "save", plate_save):
                with patch.object(PrintJob, "save", job_save):
                    start_print_for_queue_entry(self.entry)

        # Check status was set to printing
        self.assertEqual(self.entry.status, PrintQueue.STATUS_PRINTING)
        self.assertIsNotNone(self.entry.started_at)

        self.assertEqual(self.plate.status, PrintJobPlate.STATUS_PRINTING)
        self.assertIsNotNone(self.plate.started_at)
        self.assertTrue(self.plate.klipper_job_id.startswith("LN_"))

        self.assertEqual(self.job.status, PrintJob.STATUS_PRINTING)
        self.assertIsNotNone(self.job.started_at)

        # Verify save was called on each
        entry_save.assert_called_once()
        plate_save.assert_called_once()
        job_save.assert_called_once()
