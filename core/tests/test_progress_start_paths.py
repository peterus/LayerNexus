"""Regression: every print start path gets live progress and completion.

Each path ends in ``mark_queue_entry_started``. After starting an entry the
tests feed Moonraker notifications through the worker's event handler (the
printer backend is mocked, no real Moonraker) and assert that progress is
written and that a finished history event sends the entry to review.
"""

from datetime import timedelta
from typing import Any
from unittest.mock import MagicMock, patch

from asgiref.sync import async_to_sync
from django.contrib.auth.models import Group, User
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from core.management.commands.moonraker_worker import _handle_event_for_printer
from core.models import PrinterProfile, PrintJob, PrintJobPlate, PrintQueue, ScheduledQueueStart
from core.services.moonraker_ws import snapshot_event
from core.services.operator_dialog import StartNext, handle_action
from core.services.scheduled_start import run_due_scheduled_starts
from core.tests.mixins import complete_plate, queue_state_fields


@patch("core.services.queue.get_printer_backend")
class ProgressStartPathTests(TestCase):
    """Start a queue entry via each path, then check progress and completion."""

    def setUp(self) -> None:
        self.user = User.objects.create_user(username="operator", password="testpass123")
        self.user.groups.add(Group.objects.get(name="Operator"))
        self.client.login(username="operator", password="testpass123")
        self.printer = PrinterProfile.objects.create(name="P1", moonraker_url="http://p1:7125", created_by=self.user)
        self.job = PrintJob.objects.create(name="Bracket", status=PrintJob.STATUS_SLICED, created_by=self.user)
        self.plate1 = PrintJobPlate.objects.create(print_job=self.job, plate_number=1, gcode_file="gcode_jobs/p1.gcode")
        self.plate2 = PrintJobPlate.objects.create(print_job=self.job, plate_number=2, gcode_file="gcode_jobs/p2.gcode")

    def _entry(self, plate: PrintJobPlate, **kwargs: Any) -> PrintQueue:
        return PrintQueue.objects.create(plate=plate, printer=self.printer, **queue_state_fields("waiting"), **kwargs)

    def _feed(self, event: dict[str, Any]) -> None:
        async_to_sync(_handle_event_for_printer)(self.printer, event)

    def _assert_started(self, entry: PrintQueue, backend: MagicMock) -> None:
        backend.return_value.start_print.assert_called_once()
        entry.refresh_from_db()
        self.assertEqual(entry.state, PrintQueue.STATE_PRINTING)

    def _assert_progress_and_completion(self, entry: PrintQueue) -> None:
        self._feed({"method": "notify_status_update", "params": [{"virtual_sdcard": {"progress": 0.5}}, 1.0]})
        entry.refresh_from_db()
        self.assertEqual(entry.progress, 0.5)
        self.assertIsNotNone(entry.status_updated_at)

        self._feed(
            {"method": "notify_history_changed", "params": [{"action": "finished", "job": {"status": "completed"}}]}
        )
        entry.refresh_from_db()
        self.assertEqual(entry.state, PrintQueue.STATE_AWAITING_REVIEW)

    def test_web_run_next(self, mock_backend: MagicMock) -> None:
        entry = self._entry(self.plate1)

        response = self.client.post(reverse("core:run_queue", args=[self.printer.pk]))

        self.assertEqual(response.status_code, 302)
        self._assert_started(entry, mock_backend)
        self._assert_progress_and_completion(entry)

    def test_web_run_all_queues(self, mock_backend: MagicMock) -> None:
        entry = self._entry(self.plate1)

        response = self.client.post(reverse("core:run_all_queues"))

        self.assertEqual(response.status_code, 302)
        self._assert_started(entry, mock_backend)
        self._assert_progress_and_completion(entry)

    @override_settings(SCHEDULED_START_MAX_LATENESS_MINUTES=30)
    def test_scheduled_start(self, mock_backend: MagicMock) -> None:
        entry = self._entry(self.plate1)
        ScheduledQueueStart.objects.create(printer=self.printer, start_at=timezone.now() - timedelta(minutes=1))

        run_due_scheduled_starts()

        self.assertEqual(ScheduledQueueStart.objects.get().status, ScheduledQueueStart.STATUS_STARTED)
        self._assert_started(entry, mock_backend)
        self._assert_progress_and_completion(entry)

    def test_klipperscreen_start_next(self, mock_backend: MagicMock) -> None:
        entry = self._entry(self.plate1)

        handle_action(self.printer, StartNext(entry.pk))

        self._assert_started(entry, mock_backend)
        self._assert_progress_and_completion(entry)

    def test_remaining_plate_of_printing_job(self, mock_backend: MagicMock) -> None:
        # Plate 1 was printed (and passed) earlier on the same printer.
        complete_plate(self.plate1, self.printer, when=timezone.now() - timedelta(hours=3))
        entry = self._entry(self.plate2)

        self.client.post(reverse("core:run_queue", args=[self.printer.pk]))

        self._assert_started(entry, mock_backend)
        self._assert_progress_and_completion(entry)

    def test_requeued_entry_with_stale_progress(self, mock_backend: MagicMock) -> None:
        entry = self._entry(self.plate1, progress=0.18, status_updated_at=timezone.now() - timedelta(days=1))

        self.client.post(reverse("core:run_queue", args=[self.printer.pk]))

        self._assert_started(entry, mock_backend)
        self.assertIsNone(entry.progress)
        self.assertIsNone(entry.status_updated_at)
        self._assert_progress_and_completion(entry)

    def test_resubscribe_snapshot_does_not_end_print(self, mock_backend: MagicMock) -> None:
        entry = self._entry(self.plate1)
        self.client.post(reverse("core:run_queue", args=[self.printer.pk]))
        self._assert_started(entry, mock_backend)

        snapshot = snapshot_event(
            {"eventtime": 2.0, "status": {"print_stats": {"state": "standby"}, "virtual_sdcard": {"progress": 0.7}}}
        )
        self._feed(snapshot)

        entry.refresh_from_db()
        self.assertEqual(entry.progress, 0.7)
        self.assertEqual(entry.state, PrintQueue.STATE_PRINTING)
