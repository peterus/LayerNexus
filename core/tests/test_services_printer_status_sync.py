"""Tests for the printer status event -> PrintQueue sync logic."""

from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from core.models import PrintJob, PrintJobPlate, PrintQueue
from core.services.printer_status_sync import (
    PROGRESS_WRITE_INTERVAL,
    apply_status_event,
)
from core.tests.mixins import TestDataMixin


class ApplyStatusEventTests(TestDataMixin, TestCase):
    """Tests for ``apply_status_event``."""

    def setUp(self):
        super().setUp()
        self.printer.moonraker_url = "http://printer:7125"
        self.printer.save()

        self.job = PrintJob.objects.create(
            name="Test Job",
            status=PrintJob.STATUS_SLICED,
            created_by=self.user,
        )
        self.plate = PrintJobPlate.objects.create(
            print_job=self.job,
            plate_number=1,
            status=PrintJobPlate.STATUS_PRINTING,
        )
        self.entry = PrintQueue.objects.create(
            plate=self.plate,
            printer=self.printer,
            status=PrintQueue.STATUS_PRINTING,
            started_at=timezone.now(),
        )

    # ----- notify_history_changed ------------------------------------------

    def test_history_finished_completed_transitions_to_review(self):
        event = {
            "method": "notify_history_changed",
            "params": [{"action": "finished", "job": {"status": "completed"}}],
        }
        changed = apply_status_event(self.entry, event)
        self.entry.refresh_from_db()
        self.assertTrue(changed)
        self.assertEqual(self.entry.status, PrintQueue.STATUS_AWAITING_REVIEW)
        self.assertIsNotNone(self.entry.completed_at)
        self.assertEqual(self.entry.last_error, "")

    def test_history_finished_cancelled_sets_last_error(self):
        event = {
            "method": "notify_history_changed",
            "params": [{"action": "finished", "job": {"status": "cancelled"}}],
        }
        apply_status_event(self.entry, event)
        self.entry.refresh_from_db()
        self.assertEqual(self.entry.status, PrintQueue.STATUS_AWAITING_REVIEW)
        self.assertIn("cancelled", self.entry.last_error)

    def test_history_finished_error_sets_last_error(self):
        event = {
            "method": "notify_history_changed",
            "params": [{"action": "finished", "job": {"status": "error"}}],
        }
        apply_status_event(self.entry, event)
        self.entry.refresh_from_db()
        self.assertEqual(self.entry.status, PrintQueue.STATUS_AWAITING_REVIEW)
        self.assertIn("error", self.entry.last_error)

    def test_history_action_added_is_ignored(self):
        event = {
            "method": "notify_history_changed",
            "params": [{"action": "added", "job": {"status": "in_progress"}}],
        }
        changed = apply_status_event(self.entry, event)
        self.entry.refresh_from_db()
        self.assertFalse(changed)
        self.assertEqual(self.entry.status, PrintQueue.STATUS_PRINTING)

    def test_history_on_non_printing_entry_is_ignored(self):
        self.entry.status = PrintQueue.STATUS_WAITING
        self.entry.save(update_fields=["status"])
        event = {
            "method": "notify_history_changed",
            "params": [{"action": "finished", "job": {"status": "completed"}}],
        }
        changed = apply_status_event(self.entry, event)
        self.assertFalse(changed)

    # ----- notify_status_update: terminal state ----------------------------

    def test_status_update_terminal_state_triggers_transition(self):
        event = {
            "method": "notify_status_update",
            "params": [{"print_stats": {"state": "complete"}}],
        }
        changed = apply_status_event(self.entry, event)
        self.entry.refresh_from_db()
        self.assertTrue(changed)
        self.assertEqual(self.entry.status, PrintQueue.STATUS_AWAITING_REVIEW)

    def test_status_update_error_state_sets_last_error(self):
        event = {
            "method": "notify_status_update",
            "params": [{"print_stats": {"state": "error"}}],
        }
        apply_status_event(self.entry, event)
        self.entry.refresh_from_db()
        self.assertEqual(self.entry.status, PrintQueue.STATUS_AWAITING_REVIEW)
        self.assertIn("error", self.entry.last_error)

    # ----- notify_status_update: progress throttling -----------------------

    def test_status_update_progress_first_write(self):
        event = {
            "method": "notify_status_update",
            "params": [{"virtual_sdcard": {"progress": 0.25}}],
        }
        changed = apply_status_event(self.entry, event)
        self.entry.refresh_from_db()
        self.assertTrue(changed)
        self.assertAlmostEqual(self.entry.progress, 0.25)
        self.assertIsNotNone(self.entry.status_updated_at)

    def test_status_update_progress_within_throttle_window_ignored(self):
        self.entry.progress = 0.10
        self.entry.status_updated_at = timezone.now()
        self.entry.save(update_fields=["progress", "status_updated_at"])

        event = {
            "method": "notify_status_update",
            "params": [{"virtual_sdcard": {"progress": 0.12}}],
        }
        changed = apply_status_event(self.entry, event)
        self.entry.refresh_from_db()
        self.assertFalse(changed)
        self.assertAlmostEqual(self.entry.progress, 0.10)

    def test_status_update_progress_after_throttle_window_written(self):
        self.entry.progress = 0.10
        self.entry.status_updated_at = timezone.now() - PROGRESS_WRITE_INTERVAL - timedelta(seconds=1)
        self.entry.save(update_fields=["progress", "status_updated_at"])

        event = {
            "method": "notify_status_update",
            "params": [{"virtual_sdcard": {"progress": 0.55}}],
        }
        changed = apply_status_event(self.entry, event)
        self.entry.refresh_from_db()
        self.assertTrue(changed)
        self.assertAlmostEqual(self.entry.progress, 0.55)

    # ----- unknown events ---------------------------------------------------

    def test_unknown_method_ignored(self):
        event = {"method": "notify_whatever", "params": []}
        self.assertFalse(apply_status_event(self.entry, event))

    def test_missing_params_ignored(self):
        event = {"method": "notify_status_update"}
        self.assertFalse(apply_status_event(self.entry, event))
