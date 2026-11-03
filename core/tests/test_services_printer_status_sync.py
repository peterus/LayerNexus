"""Tests for the printer status event -> PrintQueue sync logic."""

from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from core.models import PrintJob, PrintJobPlate, PrintQueue
from core.services.moonraker_ws import snapshot_event
from core.services.printer_status_sync import (
    PROGRESS_WRITE_INTERVAL,
    apply_status_event,
)
from core.tests.mixins import TestDataMixin, queue_state_fields


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
        )
        self.entry = PrintQueue.objects.create(
            plate=self.plate,
            printer=self.printer,
            **queue_state_fields("printing"),
        )

    # ----- notify_history_changed ------------------------------------------

    def test_finish_event_for_cancelled_attempt_is_ignored(self):
        """A finish event for an attempt removed (cancelled) meanwhile must not re-open it."""
        stale = PrintQueue.objects.get(pk=self.entry.pk)  # the worker's copy, still "printing"
        self.entry.remove()
        for event in (
            {"method": "notify_history_changed", "params": [{"action": "finished", "job": {"status": "completed"}}]},
            {"method": "notify_status_update", "params": [{"print_stats": {"state": "complete"}}]},
            {"method": "notify_status_update", "params": [{"virtual_sdcard": {"progress": 0.5}}]},
        ):
            with self.subTest(method=event["method"]):
                self.assertFalse(apply_status_event(stale, event))
        self.entry.refresh_from_db()
        self.assertEqual(self.entry.state, PrintQueue.RESULT_CANCELLED)
        self.assertIsNone(self.entry.completed_at)
        self.assertIsNone(self.entry.progress)

    def test_history_finished_completed_transitions_to_review(self):
        event = {
            "method": "notify_history_changed",
            "params": [{"action": "finished", "job": {"status": "completed"}}],
        }
        changed = apply_status_event(self.entry, event)
        self.entry.refresh_from_db()
        self.assertTrue(changed)
        self.assertEqual(self.entry.state, PrintQueue.STATE_AWAITING_REVIEW)
        self.assertIsNotNone(self.entry.completed_at)
        self.assertEqual(self.entry.last_error, "")

    def test_history_finished_cancelled_sets_last_error(self):
        event = {
            "method": "notify_history_changed",
            "params": [{"action": "finished", "job": {"status": "cancelled"}}],
        }
        apply_status_event(self.entry, event)
        self.entry.refresh_from_db()
        self.assertEqual(self.entry.state, PrintQueue.STATE_AWAITING_REVIEW)
        self.assertIn("cancelled", self.entry.last_error)

    def test_history_finished_completed_clears_previous_last_error(self):
        self.entry.last_error = "old failure from previous retry"
        self.entry.save(update_fields=["last_error"])
        event = {
            "method": "notify_history_changed",
            "params": [{"action": "finished", "job": {"status": "completed"}}],
        }
        apply_status_event(self.entry, event)
        self.entry.refresh_from_db()
        self.assertEqual(self.entry.state, PrintQueue.STATE_AWAITING_REVIEW)
        self.assertEqual(self.entry.last_error, "")

    def test_history_finished_error_sets_last_error(self):
        event = {
            "method": "notify_history_changed",
            "params": [{"action": "finished", "job": {"status": "error"}}],
        }
        apply_status_event(self.entry, event)
        self.entry.refresh_from_db()
        self.assertEqual(self.entry.state, PrintQueue.STATE_AWAITING_REVIEW)
        self.assertIn("error", self.entry.last_error)

    def test_history_action_added_is_ignored(self):
        event = {
            "method": "notify_history_changed",
            "params": [{"action": "added", "job": {"status": "in_progress"}}],
        }
        changed = apply_status_event(self.entry, event)
        self.entry.refresh_from_db()
        self.assertFalse(changed)
        self.assertEqual(self.entry.state, PrintQueue.STATE_PRINTING)

    def test_history_on_non_printing_entry_is_ignored(self):
        self.entry.started_at = None
        self.entry.save(update_fields=["started_at"])
        event = {
            "method": "notify_history_changed",
            "params": [{"action": "finished", "job": {"status": "completed"}}],
        }
        changed = apply_status_event(self.entry, event)
        self.assertFalse(changed)

    def test_history_finished_for_other_file_is_ignored(self):
        """A finish event for a different G-code file than this attempt's must not end it."""
        self.plate.remote_job_id = "LN_Test_Job_p1.gcode"
        self.plate.save(update_fields=["remote_job_id"])
        event = {
            "method": "notify_history_changed",
            "params": [{"action": "finished", "job": {"status": "completed", "filename": "LN_Other_p2.gcode"}}],
        }
        self.assertFalse(apply_status_event(self.entry, event))
        self.entry.refresh_from_db()
        self.assertEqual(self.entry.state, PrintQueue.STATE_PRINTING)

    def test_history_finished_for_matching_file_transitions(self):
        self.plate.remote_job_id = "LN_Test_Job_p1.gcode"
        self.plate.save(update_fields=["remote_job_id"])
        for filename in ("LN_Test_Job_p1.gcode", "sub/LN_Test_Job_p1.gcode"):
            with self.subTest(filename=filename):
                entry = PrintQueue.objects.get(pk=self.entry.pk)
                PrintQueue.objects.filter(pk=entry.pk).update(completed_at=None)
                entry.refresh_from_db()
                event = {
                    "method": "notify_history_changed",
                    "params": [{"action": "finished", "job": {"status": "completed", "filename": filename}}],
                }
                self.assertTrue(apply_status_event(entry, event))
                entry.refresh_from_db()
                self.assertEqual(entry.state, PrintQueue.STATE_AWAITING_REVIEW)

    def test_history_finished_without_known_filename_transitions(self):
        """Without a stored remote file name (or job file name) the finish event still applies."""
        event = {
            "method": "notify_history_changed",
            "params": [{"action": "finished", "job": {"status": "completed", "filename": "anything.gcode"}}],
        }
        self.assertTrue(apply_status_event(self.entry, event))
        self.entry.refresh_from_db()
        self.assertEqual(self.entry.state, PrintQueue.STATE_AWAITING_REVIEW)

    def test_history_finished_klippy_shutdown_sets_last_error(self):
        event = {
            "method": "notify_history_changed",
            "params": [{"action": "finished", "job": {"status": "klippy_shutdown"}}],
        }
        self.assertTrue(apply_status_event(self.entry, event))
        self.entry.refresh_from_db()
        self.assertEqual(self.entry.state, PrintQueue.STATE_AWAITING_REVIEW)
        self.assertIn("klippy_shutdown", self.entry.last_error)

    def test_history_finished_klippy_disconnect_sets_last_error(self):
        event = {
            "method": "notify_history_changed",
            "params": [{"action": "finished", "job": {"status": "klippy_disconnect"}}],
        }
        self.assertTrue(apply_status_event(self.entry, event))
        self.entry.refresh_from_db()
        self.assertIn("klippy_disconnect", self.entry.last_error)

    # ----- notify_status_update: terminal state ----------------------------

    def test_status_update_terminal_state_triggers_transition(self):
        event = {
            "method": "notify_status_update",
            "params": [{"print_stats": {"state": "complete"}}],
        }
        changed = apply_status_event(self.entry, event)
        self.entry.refresh_from_db()
        self.assertTrue(changed)
        self.assertEqual(self.entry.state, PrintQueue.STATE_AWAITING_REVIEW)

    def test_status_update_error_state_sets_last_error(self):
        event = {
            "method": "notify_status_update",
            "params": [{"print_stats": {"state": "error"}}],
        }
        apply_status_event(self.entry, event)
        self.entry.refresh_from_db()
        self.assertEqual(self.entry.state, PrintQueue.STATE_AWAITING_REVIEW)
        self.assertIn("error", self.entry.last_error)

    def test_status_update_cancelled_state_triggers_transition(self):
        event = {"method": "notify_status_update", "params": [{"print_stats": {"state": "cancelled"}}]}
        self.assertTrue(apply_status_event(self.entry, event))
        self.entry.refresh_from_db()
        self.assertEqual(self.entry.state, PrintQueue.STATE_AWAITING_REVIEW)
        self.assertIn("cancelled", self.entry.last_error)

    def test_standby_right_after_start_does_not_end_attempt(self):
        """Klipper briefly reports ``standby`` when a print starts; that must not end the attempt.

        Regression: SDCARD_PRINT_FILE resets print_stats (-> ``standby``)
        before the print actually begins, so the start of every print
        broadcasts ``standby``. Processed after ``mark_started`` it used to
        mark the fresh attempt as finished (review dialog while printing).
        """
        PrintQueue.objects.filter(pk=self.entry.pk).delete()
        entry = PrintQueue.objects.create(plate=self.plate, printer=self.printer, **queue_state_fields("waiting"))
        entry.mark_started(remote_filename="LN_Test_Job_p1.gcode")

        standby = {"method": "notify_status_update", "params": [{"print_stats": {"state": "standby"}}]}
        self.assertFalse(apply_status_event(entry, standby))
        entry.refresh_from_db()
        self.assertEqual(entry.state, PrintQueue.STATE_PRINTING)
        self.assertIsNone(entry.completed_at)

        printing = {
            "method": "notify_status_update",
            "params": [{"print_stats": {"state": "printing"}, "virtual_sdcard": {"progress": 0.01}}],
        }
        self.assertTrue(apply_status_event(entry, printing))
        entry.refresh_from_db()
        self.assertEqual(entry.state, PrintQueue.STATE_PRINTING)

        complete = {"method": "notify_status_update", "params": [{"print_stats": {"state": "complete"}}]}
        self.assertTrue(apply_status_event(entry, complete))
        entry.refresh_from_db()
        self.assertEqual(entry.state, PrintQueue.STATE_AWAITING_REVIEW)
        self.assertEqual(entry.last_error, "")

    def test_standby_after_real_progress_ends_attempt_with_error(self):
        """``standby`` after the print made progress (e.g. FIRMWARE_RESTART, SDCARD_RESET_FILE) ends it."""
        PrintQueue.objects.filter(pk=self.entry.pk).update(progress=0.42)
        self.entry.refresh_from_db()
        event = {"method": "notify_status_update", "params": [{"print_stats": {"state": "standby"}}]}
        self.assertTrue(apply_status_event(self.entry, event))
        self.entry.refresh_from_db()
        self.assertEqual(self.entry.state, PrintQueue.STATE_AWAITING_REVIEW)
        self.assertIn("standby", self.entry.last_error)

    def test_firmware_restart_snapshot_ends_attempt_with_progress(self):
        """After FIRMWARE_RESTART mid-print, the resubscribe snapshot (standby, progress 0) ends the attempt."""
        PrintQueue.objects.filter(pk=self.entry.pk).update(progress=0.42)
        self.entry.refresh_from_db()
        snapshot = snapshot_event(
            {"eventtime": 9.0, "status": {"print_stats": {"state": "standby"}, "virtual_sdcard": {"progress": 0.0}}}
        )
        self.assertTrue(apply_status_event(self.entry, snapshot))
        self.entry.refresh_from_db()
        self.assertEqual(self.entry.state, PrintQueue.STATE_AWAITING_REVIEW)
        self.assertIn("standby", self.entry.last_error)

    def test_standby_with_zero_progress_does_not_end_attempt(self):
        PrintQueue.objects.filter(pk=self.entry.pk).update(progress=0.0)
        self.entry.refresh_from_db()
        event = {"method": "notify_status_update", "params": [{"print_stats": {"state": "standby"}}]}
        self.assertFalse(apply_status_event(self.entry, event))
        self.entry.refresh_from_db()
        self.assertEqual(self.entry.state, PrintQueue.STATE_PRINTING)

    def test_status_update_standby_with_progress_still_writes_progress(self):
        event = {
            "method": "notify_status_update",
            "params": [{"print_stats": {"state": "standby"}, "virtual_sdcard": {"progress": 0.0}}],
        }
        self.assertTrue(apply_status_event(self.entry, event))
        self.entry.refresh_from_db()
        self.assertEqual(self.entry.state, PrintQueue.STATE_PRINTING)
        self.assertEqual(self.entry.progress, 0.0)

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

    def test_status_update_non_numeric_progress_ignored(self):
        """A non-numeric ``virtual_sdcard.progress`` must not raise; it is
        skipped so the flawed value doesn't stall progress via the broad
        except in the WS layer."""
        self.entry.progress = 0.10
        self.entry.status_updated_at = timezone.now() - PROGRESS_WRITE_INTERVAL - timedelta(seconds=1)
        self.entry.save(update_fields=["progress", "status_updated_at"])

        event = {
            "method": "notify_status_update",
            "params": [{"virtual_sdcard": {"progress": "not-a-number"}}],
        }
        changed = apply_status_event(self.entry, event)
        self.entry.refresh_from_db()
        self.assertFalse(changed)
        self.assertAlmostEqual(self.entry.progress, 0.10)

    def test_status_update_none_progress_string_ignored(self):
        """A ``None``-ish garbage value (e.g. list) is skipped gracefully."""
        self.entry.progress = 0.20
        self.entry.status_updated_at = timezone.now() - PROGRESS_WRITE_INTERVAL - timedelta(seconds=1)
        self.entry.save(update_fields=["progress", "status_updated_at"])

        event = {
            "method": "notify_status_update",
            "params": [{"virtual_sdcard": {"progress": [1, 2, 3]}}],
        }
        changed = apply_status_event(self.entry, event)
        self.entry.refresh_from_db()
        self.assertFalse(changed)
        self.assertAlmostEqual(self.entry.progress, 0.20)

    def test_status_update_overflow_progress_ignored(self):
        """A JSON integer too large for float() (raising OverflowError) must
        be skipped, not escape and get swallowed by the WS layer."""
        self.entry.progress = 0.30
        self.entry.status_updated_at = timezone.now() - PROGRESS_WRITE_INTERVAL - timedelta(seconds=1)
        self.entry.save(update_fields=["progress", "status_updated_at"])

        event = {
            "method": "notify_status_update",
            "params": [{"virtual_sdcard": {"progress": 10**400}}],
        }
        changed = apply_status_event(self.entry, event)
        self.entry.refresh_from_db()
        self.assertFalse(changed)
        self.assertAlmostEqual(self.entry.progress, 0.30)

    def test_status_update_non_finite_progress_ignored(self):
        """NaN / infinity convert via float() but violate the 0.0-1.0
        contract; they must be skipped, not persisted (a written NaN would
        also throttle later valid updates via status_updated_at)."""
        for bad in (float("nan"), float("inf"), float("-inf")):
            self.entry.progress = 0.40
            self.entry.status_updated_at = timezone.now() - PROGRESS_WRITE_INTERVAL - timedelta(seconds=1)
            self.entry.save(update_fields=["progress", "status_updated_at"])

            event = {
                "method": "notify_status_update",
                "params": [{"virtual_sdcard": {"progress": bad}}],
            }
            changed = apply_status_event(self.entry, event)
            self.entry.refresh_from_db()
            self.assertFalse(changed, f"{bad!r} should be ignored")
            self.assertAlmostEqual(self.entry.progress, 0.40)

    def test_status_update_out_of_range_progress_ignored(self):
        """Progress outside [0.0, 1.0] is malformed and skipped."""
        for bad in (-0.1, 1.5, 42.0):
            self.entry.progress = 0.40
            self.entry.status_updated_at = timezone.now() - PROGRESS_WRITE_INTERVAL - timedelta(seconds=1)
            self.entry.save(update_fields=["progress", "status_updated_at"])

            event = {
                "method": "notify_status_update",
                "params": [{"virtual_sdcard": {"progress": bad}}],
            }
            changed = apply_status_event(self.entry, event)
            self.entry.refresh_from_db()
            self.assertFalse(changed, f"{bad!r} should be ignored")
            self.assertAlmostEqual(self.entry.progress, 0.40)

    def test_status_update_boolean_progress_ignored(self):
        """A JSON boolean converts via float() (True->1.0, False->0.0) and
        would sneak past the range guard (True == 1). Reject it as
        malformed instead of persisting a bogus progress."""
        for bad in (True, False):
            self.entry.progress = 0.40
            self.entry.status_updated_at = timezone.now() - PROGRESS_WRITE_INTERVAL - timedelta(seconds=1)
            self.entry.save(update_fields=["progress", "status_updated_at"])

            event = {
                "method": "notify_status_update",
                "params": [{"virtual_sdcard": {"progress": bad}}],
            }
            changed = apply_status_event(self.entry, event)
            self.entry.refresh_from_db()
            self.assertFalse(changed, f"{bad!r} should be ignored")
            self.assertAlmostEqual(self.entry.progress, 0.40)

    # ----- unknown events ---------------------------------------------------

    def test_unknown_method_ignored(self):
        event = {"method": "notify_whatever", "params": []}
        self.assertFalse(apply_status_event(self.entry, event))

    def test_missing_params_ignored(self):
        event = {"method": "notify_status_update"}
        self.assertFalse(apply_status_event(self.entry, event))
