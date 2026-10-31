"""Tests for the derived plate state (``PrintJobPlate.state``) and its display (Topic 31)."""

from datetime import timedelta

from django.db.models import Prefetch
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from core.models import PrinterProfile, PrintJobPlate, PrintQueue
from core.tests.mixins import TestDataMixin, add_attempt, complete_plate, fail_plate, make_sliced_job


class _PlateStateBase(TestDataMixin, TestCase):
    """A sliced two-plate job with G-code."""

    def setUp(self):
        super().setUp()
        self.job = make_sliced_job("State Job", plates=2, created_by=self.user)
        self.plate, self.plate2 = list(self.job.plates.order_by("plate_number"))
        PrintJobPlate.objects.filter(print_job=self.job).update(gcode_file="gcode_jobs/x.gcode")
        self.plate.refresh_from_db()
        self.plate2.refresh_from_db()

    def _state(self, plate: PrintJobPlate) -> str:
        """Return the plate state via a fresh instance (no cached attempts)."""
        return PrintJobPlate.objects.get(pk=plate.pk).state

    def _annotated(self, plate: PrintJobPlate) -> str:
        """Return the plate state via the SQL annotation."""
        return PrintJobPlate.objects.with_state().get(pk=plate.pk).annotated_state


class PlateStateTests(_PlateStateBase):
    """One test per state, each checked via the property and the annotation."""

    def _assert_state(self, expected: str) -> None:
        self.assertEqual(self._state(self.plate), expected)
        self.assertEqual(self._annotated(self.plate), expected)

    def test_plate_state_not_queued_without_attempts(self):
        self._assert_state(PrintJobPlate.STATE_NOT_QUEUED)

    def test_plate_state_waiting(self):
        add_attempt(self.plate, self.printer, "waiting")
        self._assert_state(PrintJobPlate.STATE_WAITING)

    def test_plate_state_starting_with_active_claim(self):
        add_attempt(self.plate, self.printer, "waiting", start_claimed_at=timezone.now())
        self._assert_state(PrintJobPlate.STATE_STARTING)

    def test_plate_state_waiting_with_expired_claim(self):
        add_attempt(self.plate, self.printer, "waiting", start_claimed_at=timezone.now() - timedelta(hours=1))
        self._assert_state(PrintJobPlate.STATE_WAITING)

    def test_plate_state_printing(self):
        add_attempt(self.plate, self.printer, "printing")
        self._assert_state(PrintJobPlate.STATE_PRINTING)

    def test_plate_state_awaiting_review(self):
        add_attempt(self.plate, self.printer, "awaiting_review")
        self._assert_state(PrintJobPlate.STATE_AWAITING_REVIEW)

    def test_plate_state_completed(self):
        attempt = complete_plate(self.plate, self.printer)
        self._assert_state(PrintJobPlate.STATE_COMPLETED)
        self.assertEqual(PrintJobPlate.objects.get(pk=self.plate.pk).completed_at, attempt.resolved_at)

    def test_completed_wins_over_open_attempt(self):
        complete_plate(self.plate, self.printer)
        add_attempt(self.plate, self.printer, "waiting")
        self._assert_state(PrintJobPlate.STATE_COMPLETED)

    def test_plate_state_failed_after_max_failures(self):
        fail_plate(self.plate, self.printer, max_retries=3)
        self._assert_state(PrintJobPlate.STATE_FAILED)

    def test_plate_not_failed_below_max_failures(self):
        add_attempt(self.plate, self.printer, PrintQueue.RESULT_FAILED, max_retries=3)
        self._assert_state(PrintJobPlate.STATE_NOT_QUEUED)

    def test_manual_requeue_resets_failure_counter(self):
        fail_plate(self.plate, self.printer, max_retries=3)
        add_attempt(self.plate, self.printer, PrintQueue.RESULT_FAILED, max_retries=3)  # manual, 1 failure
        self._assert_state(PrintJobPlate.STATE_NOT_QUEUED)

    def test_cancelled_attempt_never_counts_as_failure(self):
        for _ in range(3):
            add_attempt(self.plate, self.printer, PrintQueue.RESULT_CANCELLED, max_retries=1)
        self._assert_state(PrintJobPlate.STATE_NOT_QUEUED)

    def test_failed_plate_with_new_open_attempt_is_queued(self):
        fail_plate(self.plate, self.printer)
        add_attempt(self.plate, self.printer, "waiting")
        self._assert_state(PrintJobPlate.STATE_WAITING)

    def test_with_state_annotation_matches_property(self):
        """Many plates in every state: the SQL annotation equals the Python derivation."""
        now = timezone.now()
        setups = [
            lambda p: None,
            lambda p: add_attempt(p, self.printer, "waiting"),
            lambda p: add_attempt(p, self.printer, "waiting", start_claimed_at=now),
            lambda p: add_attempt(p, self.printer, "printing"),
            lambda p: add_attempt(p, self.printer, "awaiting_review"),
            lambda p: complete_plate(p, self.printer),
            lambda p: fail_plate(p, self.printer),
            lambda p: fail_plate(p, self.printer, max_retries=1),
            lambda p: add_attempt(p, self.printer, PrintQueue.RESULT_FAILED),
            lambda p: add_attempt(p, None, PrintQueue.RESULT_CANCELLED),
            lambda p: (fail_plate(p, self.printer), add_attempt(p, self.printer, PrintQueue.RESULT_FAILED)),
            lambda p: (add_attempt(p, self.printer, PrintQueue.RESULT_FAILED), complete_plate(p, self.printer)),
            # Retry-only history (no manual attempt): falls back to the last attempt's max_retries.
            lambda p: [add_attempt(p, self.printer, PrintQueue.RESULT_FAILED, is_retry=True) for _ in range(3)],
            lambda p: add_attempt(p, self.printer, PrintQueue.RESULT_FAILED, is_retry=True, max_retries=2),
            # max_retries=0: never "failed" without a failure, failed after one.
            lambda p: add_attempt(p, self.printer, PrintQueue.RESULT_CANCELLED, max_retries=0),
            lambda p: add_attempt(p, self.printer, PrintQueue.RESULT_FAILED, max_retries=0),
            # Mixed max_retries along the chain: the last manual attempt's value counts.
            lambda p: (
                add_attempt(p, self.printer, PrintQueue.RESULT_FAILED, max_retries=1),
                add_attempt(p, self.printer, PrintQueue.RESULT_FAILED, max_retries=5),
                add_attempt(p, self.printer, PrintQueue.RESULT_FAILED, is_retry=True, max_retries=1),
            ),
        ]
        job = make_sliced_job("Many", plates=len(setups))
        plates = list(job.plates.order_by("plate_number"))
        for plate, setup in zip(plates, setups, strict=True):
            setup(plate)
        annotated = {plate.pk: plate.annotated_state for plate in PrintJobPlate.objects.with_state()}
        for plate in plates:
            with self.subTest(plate=plate.plate_number):
                self.assertEqual(annotated[plate.pk], PrintJobPlate.objects.get(pk=plate.pk).state)

    def test_prefetched_state_needs_no_query(self):
        add_attempt(self.plate, self.printer, "printing")
        complete_plate(self.plate2, self.printer)
        plates = list(PrintJobPlate.objects.filter(print_job=self.job).prefetch_related(Prefetch("queue_entries")))
        with self.assertNumQueries(0):
            states = [plate.state for plate in plates]
        self.assertEqual(states, [PrintJobPlate.STATE_PRINTING, PrintJobPlate.STATE_COMPLETED])

    def test_queueable_plates(self):
        add_attempt(self.plate, self.printer, "waiting")
        self.assertEqual(list(PrintJobPlate.objects.queueable()), [self.plate2])
        PrintQueue.objects.filter(plate=self.plate).delete()
        fail_plate(self.plate, self.printer)
        complete_plate(self.plate2, self.printer)
        self.assertEqual(list(PrintJobPlate.objects.queueable()), [self.plate])

    def test_queueable_requires_sliced_job_and_gcode(self):
        PrintJobPlate.objects.filter(pk=self.plate.pk).update(gcode_file="")
        self.assertEqual(list(PrintJobPlate.objects.queueable()), [self.plate2])
        self.job.status = self.job.STATUS_FAILED
        self.job.save(update_fields=["status"])
        self.assertEqual(list(PrintJobPlate.objects.queueable()), [])


class PlateStateTransitionTests(_PlateStateBase):
    """The derived state follows the attempt transitions (enqueue … pass / fail / remove)."""

    def test_state_follows_attempt_lifecycle(self):
        attempt = PrintQueue.enqueue(plate=self.plate, printer=self.printer)
        self.assertEqual(self._state(self.plate), PrintJobPlate.STATE_WAITING)
        attempt.mark_started(remote_filename="LN_1.gcode")
        self.assertEqual(self._state(self.plate), PrintJobPlate.STATE_PRINTING)
        attempt.mark_finished()
        self.assertEqual(self._state(self.plate), PrintJobPlate.STATE_AWAITING_REVIEW)
        attempt.refresh_from_db()
        attempt.resolve(passed=True)
        self.assertEqual(self._state(self.plate), PrintJobPlate.STATE_COMPLETED)

    def test_remove_waiting_attempt_returns_to_not_queued(self):
        PrintQueue.enqueue(plate=self.plate, printer=self.printer).remove()
        self.assertEqual(self._state(self.plate), PrintJobPlate.STATE_NOT_QUEUED)

    def test_failed_review_requeues_until_max(self):
        attempt = PrintQueue.enqueue(plate=self.plate, printer=self.printer)
        for expected in ("requeued", "requeued", "discarded"):
            PrintQueue.objects.filter(pk=attempt.pk).update(started_at=timezone.now(), completed_at=timezone.now())
            attempt.refresh_from_db()
            self.assertEqual(attempt.resolve(passed=False), expected)
            attempt = PrintQueue.objects.open().filter(plate=self.plate).first() or attempt
        self.assertEqual(self._state(self.plate), PrintJobPlate.STATE_FAILED)


class PlateStateDisplayTests(_PlateStateBase):
    """Job detail shows the derived plate state, queue place and attempt history (Topic 31)."""

    def setUp(self):
        super().setUp()
        self.client.login(username="testuser", password="testpass123")
        self.url = reverse("core:printjob_detail", args=[self.job.pk])

    def _plate(self, response, plate: PrintJobPlate) -> PrintJobPlate:
        return next(p for p in response.context["plates"] if p.pk == plate.pk)

    def test_plate_state_queued_shows_printer_and_position(self):
        other_job = make_sliced_job("Other", plates=1)
        PrintQueue.enqueue(plate=other_job.plates.get(), printer=self.printer)
        PrintQueue.enqueue(plate=self.plate, printer=self.printer)
        response = self.client.get(self.url)
        self.assertEqual(self._plate(response, self.plate).queue_label, "Queued · Test Printer, position 2")
        self.assertContains(response, "Queued · Test Printer, position 2")

    def test_queue_position_follows_priority(self):
        other_job = make_sliced_job("Other", plates=1)
        PrintQueue.enqueue(plate=other_job.plates.get(), printer=self.printer)
        PrintQueue.enqueue(plate=self.plate, printer=self.printer, priority=4)
        response = self.client.get(self.url)
        self.assertEqual(self._plate(response, self.plate).queue_label, "Queued · Test Printer, position 1")

    def test_printing_plate_shows_printer(self):
        add_attempt(self.plate, self.printer, "printing")
        add_attempt(self.plate2, self.other_printer, "awaiting_review")
        response = self.client.get(self.url)
        self.assertEqual(self._plate(response, self.plate).queue_label, "Printing · Test Printer")
        self.assertEqual(self._plate(response, self.plate2).queue_label, "Awaiting review · Other Printer")

    def test_unqueued_plate_has_no_queue_label(self):
        response = self.client.get(self.url)
        self.assertEqual(self._plate(response, self.plate).queue_label, "")
        self.assertNotContains(response, "plate-queue-label")

    def test_enqueue_button_hidden_for_queued_or_completed_plate(self):
        queue_url = f"{reverse('core:printqueue_create')}?plate="
        response = self.client.get(self.url)
        self.assertContains(response, f"{queue_url}{self.plate.pk}")
        self.assertContains(response, f"{queue_url}{self.plate2.pk}")
        add_attempt(self.plate, self.printer, "waiting")
        complete_plate(self.plate2, self.printer)
        response = self.client.get(self.url)
        self.assertNotContains(response, "plate-queue-button")
        self.assertIsNone(response.context["first_queueable_plate"])

    def test_enqueue_button_shown_for_failed_plate(self):
        fail_plate(self.plate, self.printer)
        complete_plate(self.plate2, self.printer)
        response = self.client.get(self.url)
        self.assertContains(response, "plate-queue-button", count=1)
        self.assertEqual(response.context["first_queueable_plate"], self.plate)

    def test_job_detail_shows_attempt_history_per_plate(self):
        start = timezone.now() - timedelta(hours=2)
        add_attempt(self.plate, self.printer, PrintQueue.RESULT_FAILED, when=start)
        add_attempt(self.plate, self.other_printer, PrintQueue.RESULT_PASSED)
        add_attempt(self.plate2, self.printer, "printing")
        response = self.client.get(self.url)
        history = self._plate(response, self.plate).resolved_attempts
        self.assertEqual([a.result for a in history], [PrintQueue.RESULT_FAILED, PrintQueue.RESULT_PASSED])
        self.assertEqual(self._plate(response, self.plate2).resolved_attempts, [])
        self.assertContains(response, "plate-attempt-history", count=1)
        self.assertContains(response, start.strftime("%Y-%m-%d"))
        self.assertContains(response, "Other Printer")
        self.assertContains(response, "Passed")
        self.assertContains(response, "Failed")

    def test_detail_query_count_independent_of_attempts(self):
        add_attempt(self.plate, self.printer, "printing")
        add_attempt(self.plate2, self.printer, "waiting")
        response_small = self._count_queries()
        PrintQueue.objects.filter(plate=self.plate2).delete()
        printer3 = PrinterProfile.objects.create(name="P3")
        for _ in range(5):
            add_attempt(self.plate2, printer3, PrintQueue.RESULT_FAILED)
        add_attempt(self.plate2, printer3, "waiting")
        self.assertEqual(self._count_queries(), response_small)

    def _count_queries(self) -> int:
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        with CaptureQueriesContext(connection) as ctx:
            self.client.get(self.url)
        return len(ctx.captured_queries)
