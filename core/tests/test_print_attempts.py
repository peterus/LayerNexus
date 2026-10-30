"""Tests for print attempts: ``PrintQueue`` rows kept as history with a derived state."""

from datetime import timedelta

from django.db import IntegrityError, transaction
from django.test import TestCase
from django.utils import timezone

from core.models import PrintJob, PrintJobPlate, PrintQueue
from core.tests.mixins import TestDataMixin, queue_state_fields


class _AttemptTestBase(TestDataMixin, TestCase):
    """Create a sliced two-plate job for attempt tests."""

    def setUp(self):
        super().setUp()
        self.job = PrintJob.objects.create(name="Attempt Job", status=PrintJob.STATUS_SLICED, created_by=self.user)
        self.plate = PrintJobPlate.objects.create(print_job=self.job, plate_number=1)
        self.plate2 = PrintJobPlate.objects.create(print_job=self.job, plate_number=2)

    def _attempt(self, plate: PrintJobPlate | None = None, state: str = "waiting", **kwargs) -> PrintQueue:
        fields = {**queue_state_fields(state), **kwargs}
        return PrintQueue.objects.create(plate=plate or self.plate, printer=self.printer, **fields)


class AttemptStateTests(_AttemptTestBase):
    """The attempt state is derived from timestamps and the result."""

    def test_state_waiting_when_not_started(self):
        self.assertEqual(self._attempt().state, PrintQueue.STATE_WAITING)

    def test_state_starting_with_active_claim(self):
        attempt = self._attempt(start_claimed_at=timezone.now())
        self.assertEqual(attempt.state, PrintQueue.STATE_STARTING)

    def test_state_waiting_with_expired_claim(self):
        attempt = self._attempt(start_claimed_at=timezone.now() - timedelta(hours=1))
        self.assertEqual(attempt.state, PrintQueue.STATE_WAITING)

    def test_state_printing_when_started_not_completed(self):
        self.assertEqual(self._attempt(state="printing").state, PrintQueue.STATE_PRINTING)

    def test_state_awaiting_review_when_completed_no_result(self):
        self.assertEqual(self._attempt(state="awaiting_review").state, PrintQueue.STATE_AWAITING_REVIEW)

    def test_state_resolved_values(self):
        for result in (PrintQueue.RESULT_PASSED, PrintQueue.RESULT_FAILED, PrintQueue.RESULT_CANCELLED):
            with self.subTest(result=result):
                attempt = self._attempt(state="awaiting_review", result=result, resolved_at=timezone.now())
                self.assertEqual(attempt.state, result)
                self.assertTrue(attempt.is_resolved)
                PrintQueue.objects.filter(pk=attempt.pk).delete()

    def test_state_display_label(self):
        self.assertEqual(self._attempt(state="awaiting_review").get_state_display(), "Awaiting Review")

    def test_open_queryset_excludes_resolved(self):
        open_attempt = self._attempt(self.plate2)
        self._attempt(result=PrintQueue.RESULT_PASSED, resolved_at=timezone.now())
        self.assertEqual(list(PrintQueue.objects.open()), [open_attempt])
        self.assertEqual(PrintQueue.objects.resolved().count(), 1)

    def test_state_querysets_partition_open_attempts(self):
        waiting = self._attempt()
        printing = self._attempt(self.plate2, state="printing")
        plate3 = PrintJobPlate.objects.create(print_job=self.job, plate_number=3)
        review = self._attempt(plate3, state="awaiting_review")
        self.assertEqual(list(PrintQueue.objects.waiting()), [waiting])
        self.assertEqual(list(PrintQueue.objects.printing()), [printing])
        self.assertEqual(list(PrintQueue.objects.awaiting_review()), [review])
        self.assertCountEqual(PrintQueue.objects.active(), [printing, review])

    def test_second_open_attempt_for_plate_violates_constraint(self):
        self._attempt(state="printing")
        with self.assertRaises(IntegrityError), transaction.atomic():
            self._attempt()

    def test_resolved_attempt_does_not_block_new_open_attempt(self):
        self._attempt(result=PrintQueue.RESULT_FAILED, resolved_at=timezone.now())
        self._attempt()
        self.assertEqual(self.plate.queue_entries.count(), 2)


class AttemptTransitionTests(_AttemptTestBase):
    """Transitions are model methods and keep the history."""

    def test_mark_started_sets_started_at_and_plate_printing(self):
        attempt = self._attempt(start_claimed_at=timezone.now(), progress=0.7)
        attempt.mark_started(remote_filename="LN_x.gcode")
        attempt.refresh_from_db()
        self.plate.refresh_from_db()
        self.job.refresh_from_db()
        self.assertEqual(attempt.state, PrintQueue.STATE_PRINTING)
        self.assertIsNone(attempt.start_claimed_at)
        self.assertIsNone(attempt.progress)
        self.assertEqual(self.plate.status, PrintJobPlate.STATUS_PRINTING)
        self.assertEqual(self.plate.remote_job_id, "LN_x.gcode")
        self.assertEqual(self.job.status, PrintJob.STATUS_PRINTING)
        self.assertIsNotNone(self.job.started_at)

    def test_mark_finished_moves_to_review(self):
        attempt = self._attempt(state="printing")
        self.assertTrue(attempt.mark_finished(error="Printer reported: error"))
        attempt.refresh_from_db()
        self.assertEqual(attempt.state, PrintQueue.STATE_AWAITING_REVIEW)
        self.assertEqual(attempt.last_error, "Printer reported: error")

    def test_mark_finished_noop_for_resolved_attempt(self):
        attempt = self._attempt(state="printing")
        stale = PrintQueue.objects.get(pk=attempt.pk)
        attempt.remove()
        self.assertFalse(stale.mark_finished())
        stale.refresh_from_db()
        self.assertEqual(stale.state, PrintQueue.RESULT_CANCELLED)
        self.assertIsNone(stale.completed_at)

    def test_pass_keeps_attempt_and_completes_plate(self):
        attempt = self._attempt(state="awaiting_review")
        self.assertEqual(attempt.resolve(passed=True), "passed")
        attempt.refresh_from_db()
        self.plate.refresh_from_db()
        self.assertEqual(attempt.result, PrintQueue.RESULT_PASSED)
        self.assertIsNotNone(attempt.resolved_at)
        self.assertEqual(self.plate.status, PrintJobPlate.STATUS_COMPLETED)
        self.assertEqual(self.plate.completed_at, attempt.resolved_at)
        self.assertFalse(PrintQueue.objects.open().exists())

    def test_pass_last_plate_completes_job(self):
        self._attempt(self.plate2, state="awaiting_review").resolve(passed=True)
        self.job.refresh_from_db()
        self.assertNotEqual(self.job.status, PrintJob.STATUS_COMPLETED)
        self._attempt(state="awaiting_review").resolve(passed=True)
        self.job.refresh_from_db()
        self.assertEqual(self.job.status, PrintJob.STATUS_COMPLETED)
        self.assertIsNotNone(self.job.completed_at)

    def test_fail_creates_retry_attempt_first_in_priority_group(self):
        attempt = self._attempt(state="awaiting_review", priority=3, position=5)
        self.assertEqual(attempt.resolve(passed=False), "requeued")
        attempt.refresh_from_db()
        self.assertEqual(attempt.result, PrintQueue.RESULT_FAILED)
        retry = PrintQueue.objects.open().get(plate=self.plate)
        self.assertTrue(retry.is_retry)
        self.assertEqual(retry.position, 0)
        self.assertEqual(retry.priority, 3)
        self.assertEqual(retry.printer, self.printer)
        self.assertEqual(retry.max_retries, attempt.max_retries)
        self.assertEqual(retry.state, PrintQueue.STATE_WAITING)
        self.plate.refresh_from_db()
        self.assertEqual(self.plate.status, PrintJobPlate.STATUS_WAITING)
        self.assertEqual(self.plate.failed_attempts_since_manual_enqueue(), 1)

    def test_fail_after_max_failures_marks_plate_failed_and_creates_no_retry(self):
        attempt = self._attempt(state="awaiting_review", max_retries=3)
        outcomes = []
        for _ in range(3):
            outcomes.append(attempt.resolve(passed=False))
            attempt = PrintQueue.objects.open().filter(plate=self.plate).first()
            if attempt is not None:
                PrintQueue.objects.filter(pk=attempt.pk).update(**queue_state_fields("awaiting_review"))
                attempt.refresh_from_db()
        self.assertEqual(outcomes, ["requeued", "requeued", "discarded"])
        self.assertFalse(PrintQueue.objects.open().filter(plate=self.plate).exists())
        self.assertEqual(self.plate.queue_entries.filter(result=PrintQueue.RESULT_FAILED).count(), 3)
        self.plate.refresh_from_db()
        self.assertEqual(self.plate.status, PrintJobPlate.STATUS_FAILED)

    def test_manual_requeue_resets_failure_counter(self):
        now = timezone.now()
        for index in range(3):
            self._attempt(result=PrintQueue.RESULT_FAILED, resolved_at=now, state="awaiting_review", is_retry=index > 0)
        self.assertEqual(self.plate.failed_attempts_since_manual_enqueue(), 3)
        manual = self._attempt(state="awaiting_review")
        self.assertEqual(self.plate.failed_attempts_since_manual_enqueue(), 0)
        self.assertEqual(manual.resolve(passed=False), "requeued")
        self.assertEqual(self.plate.failed_attempts_since_manual_enqueue(), 1)

    def test_remove_unstarted_deletes(self):
        attempt = self._attempt()
        attempt.remove()
        self.assertFalse(PrintQueue.objects.filter(pk=attempt.pk).exists())

    def test_remove_started_marks_cancelled_and_frees_plate(self):
        attempt = self._attempt(state="printing")
        self.plate.refresh_stored_status()
        self.plate.refresh_from_db()
        self.assertEqual(self.plate.status, PrintJobPlate.STATUS_PRINTING)
        attempt.remove()
        attempt.refresh_from_db()
        self.plate.refresh_from_db()
        self.assertEqual(attempt.result, PrintQueue.RESULT_CANCELLED)
        self.assertIsNotNone(attempt.resolved_at)
        self.assertEqual(self.plate.status, PrintJobPlate.STATUS_WAITING)
        # The plate is free again: a new open attempt is allowed.
        self._attempt()

    def test_cancelled_never_counts_as_failure(self):
        for _ in range(3):
            self._attempt(state="printing", max_retries=1).remove()
        self.assertEqual(self.plate.failed_attempts_since_manual_enqueue(), 0)
        self.plate.refresh_from_db()
        self.assertEqual(self.plate.status, PrintJobPlate.STATUS_WAITING)

    def test_failed_retry_counts_from_last_manual_attempt_only(self):
        self._attempt(result=PrintQueue.RESULT_FAILED, resolved_at=timezone.now(), state="awaiting_review")
        self._attempt(
            result=PrintQueue.RESULT_FAILED, resolved_at=timezone.now(), state="awaiting_review", is_retry=True
        )
        self.assertEqual(self.plate.failed_attempts_since_manual_enqueue(), 2)

    def test_resolve_open_but_not_finished_raises(self):
        attempt = self._attempt(state="printing")
        with self.assertRaises(ValueError):
            attempt.resolve(passed=True)
