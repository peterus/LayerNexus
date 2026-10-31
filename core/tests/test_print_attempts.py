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


class AttemptReaderTests(_AttemptTestBase):
    """Queue list, worker, review and farm read open attempts only."""

    def setUp(self):
        super().setUp()
        self.client.login(username="testuser", password="testpass123")

    def _resolved(self, plate: PrintJobPlate, result: str = PrintQueue.RESULT_PASSED) -> PrintQueue:
        return self._attempt(plate, state="awaiting_review", result=result, resolved_at=timezone.now())

    def test_queue_list_hides_resolved_attempts(self):
        from django.urls import reverse

        resolved = self._resolved(self.plate)
        waiting = self._attempt(self.plate2)
        resp = self.client.get(reverse("core:printqueue_list"))
        pks = [e.pk for e in resp.context["queue_entries"]]
        self.assertEqual(pks, [waiting.pk])
        self.assertNotIn(resolved.pk, pks)

    def test_queue_list_query_count_independent_of_entries(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext
        from django.urls import reverse

        def count_queries() -> int:
            with CaptureQueriesContext(connection) as ctx:
                self.client.get(reverse("core:printqueue_list"))
            return len(ctx.captured_queries)

        self._attempt(self.plate)
        self._resolved(self.plate2, PrintQueue.RESULT_FAILED)
        baseline = count_queries()
        for number in range(3, 8):
            plate = PrintJobPlate.objects.create(print_job=self.job, plate_number=number)
            self._resolved(plate, PrintQueue.RESULT_FAILED)
            self._attempt(plate, is_retry=True)
        self.assertEqual(count_queries(), baseline)

    def test_queue_list_shows_retry_count(self):
        from django.urls import reverse

        self._resolved(self.plate, PrintQueue.RESULT_FAILED)
        retry = self._attempt(is_retry=True)
        resp = self.client.get(reverse("core:printqueue_list"))
        entry = next(e for e in resp.context["queue_entries"] if e.pk == retry.pk)
        self.assertEqual(entry.retry_count, 1)
        self.assertContains(resp, "(retry 1/3)")

    def test_worker_active_entry_ignores_resolved(self):
        from asgiref.sync import async_to_sync

        from core.management.commands.moonraker_worker import _load_active_queue_entry

        self._resolved(self.plate, PrintQueue.RESULT_CANCELLED)
        self.assertIsNone(async_to_sync(_load_active_queue_entry)(self.printer.pk))
        printing = self._attempt(self.plate2, state="printing")
        self.assertEqual(async_to_sync(_load_active_queue_entry)(self.printer.pk), printing)

    def test_concurrent_review_second_call_raises(self):
        from core.services.queue import ReviewStateError, review_queue_entry

        attempt = self._attempt(state="awaiting_review")
        self.assertEqual(review_queue_entry(attempt.pk, False).outcome, "requeued")
        with self.assertRaises(ReviewStateError):
            review_queue_entry(attempt.pk, True, printer_pk=self.printer.pk)
        attempt.refresh_from_db()
        self.assertEqual(attempt.result, PrintQueue.RESULT_FAILED)
        self.assertEqual(self.plate.queue_entries.count(), 2)
        self.assertEqual(PrintQueue.objects.open().filter(plate=self.plate).count(), 1)

    def test_resolved_attempt_does_not_keep_printer_busy(self):
        from core.services.queue import busy_entry_for_printer

        self._resolved(self.plate)
        self.assertIsNone(busy_entry_for_printer(self.printer))
        review = self._attempt(self.plate2, state="awaiting_review")
        self.assertEqual(busy_entry_for_printer(self.printer), review)

    def test_farm_card_state_from_open_attempts(self):
        from django.urls import reverse

        self._resolved(self.plate)
        resp = self.client.get(reverse("core:farm_dashboard"))
        card = next(c for c in resp.context["printer_cards"] if c["printer"] == self.printer)
        self.assertEqual(card["state"], "idle")
        self.assertEqual(resp.context["completed_today"], 0)  # stored plate status untouched by fixture
        self._attempt(self.plate2, state="awaiting_review")
        resp = self.client.get(reverse("core:farm_dashboard"))
        card = next(c for c in resp.context["printer_cards"] if c["printer"] == self.printer)
        self.assertEqual(card["state"], "awaiting_review")

    def test_queueable_plates_ignore_resolved_attempts(self):
        from core.views.queue import _queueable_plates

        self.plate.gcode_file = "gcode_jobs/a.gcode"
        self.plate.save()
        self.plate2.gcode_file = "gcode_jobs/b.gcode"
        self.plate2.save()
        self._resolved(self.plate, PrintQueue.RESULT_CANCELLED)
        self._attempt(self.plate2)
        self.assertEqual(list(_queueable_plates()), [self.plate])

    def test_enqueue_failed_plate_resets_to_waiting(self):
        for index in range(3):
            self._attempt(
                state="awaiting_review",
                result=PrintQueue.RESULT_FAILED,
                resolved_at=timezone.now(),
                is_retry=index > 0,
            )
        self.plate.refresh_stored_status()
        self.plate.refresh_from_db()
        self.assertEqual(self.plate.status, PrintJobPlate.STATUS_FAILED)
        PrintQueue.enqueue(plate=self.plate, printer=self.printer)
        self.plate.refresh_from_db()
        self.assertEqual(self.plate.status, PrintJobPlate.STATUS_WAITING)
        self.assertEqual(self.plate.failed_attempts_since_manual_enqueue(), 0)


class RetryCountConsistencyTests(_AttemptTestBase):
    """The SQL retry count equals the Python count across several retry cycles."""

    def test_with_retry_count_matches_python_over_cycles(self):
        def check() -> None:
            open_attempt = PrintQueue.objects.open().with_retry_count().get(plate=self.plate)
            self.assertEqual(open_attempt.retry_count, self.plate.failed_attempts_since_manual_enqueue())

        attempt = self._attempt(state="awaiting_review")
        for passed in (False, False):
            attempt.resolve(passed=passed)
            attempt = PrintQueue.objects.open().get(plate=self.plate)
            check()
            PrintQueue.objects.filter(pk=attempt.pk).update(**queue_state_fields("awaiting_review"))
            attempt.refresh_from_db()
        # Third failure discards; a manual re-enqueue starts a new cycle at 0.
        self.assertEqual(attempt.resolve(passed=False), "discarded")
        PrintQueue.enqueue(plate=self.plate, printer=self.printer)
        check()
        self.assertEqual(PrintQueue.objects.open().with_retry_count().get(plate=self.plate).retry_count, 0)
        attempt = PrintQueue.objects.open().get(plate=self.plate)
        PrintQueue.objects.filter(pk=attempt.pk).update(**queue_state_fields("awaiting_review"))
        attempt.refresh_from_db()
        attempt.resolve(passed=False)
        check()


class PrintHistoryDeleteGuardTests(_AttemptTestBase):
    """Jobs and plates with print history are not deleted or re-sliced by any path."""

    def setUp(self):
        super().setUp()
        self.client.login(username="testuser", password="testpass123")
        self.attempt = self._attempt(state="printing")

    def test_job_with_history_not_deletable(self):
        from django.urls import reverse

        url = reverse("core:printjob_delete", args=[self.job.pk])
        self.assertContains(self.client.get(url), "Cannot delete this print job")
        resp = self.client.post(url, follow=True)
        self.assertTrue(PrintJob.objects.filter(pk=self.job.pk).exists())
        self.assertTrue(PrintQueue.objects.filter(pk=self.attempt.pk).exists())
        self.assertIn("it has print history", " ".join(str(m) for m in resp.context["messages"]))

    def test_job_with_only_waiting_attempt_is_deletable(self):
        from django.urls import reverse

        self.attempt.delete()
        self._attempt()
        resp = self.client.post(reverse("core:printjob_delete", args=[self.job.pk]))
        self.assertEqual(resp.status_code, 302)
        self.assertFalse(PrintJob.objects.filter(pk=self.job.pk).exists())

    def _make_sliceable(self) -> None:
        """Give the job everything the slice view checks before the history guard."""
        from core.models import OrcaMachineProfile, Part, PrintJobPart

        self.job.machine_profile = OrcaMachineProfile.objects.create(
            name="Guard Machine",
            orca_name="guard_machine",
            state=OrcaMachineProfile.STATE_RESOLVED,
            instantiation=True,
        )
        self.job.save(update_fields=["machine_profile"])
        Part.objects.filter(pk=self.part.pk).update(stl_file="stl_files/guard.stl")
        PrintJobPart.objects.create(print_job=self.job, part=self.part, quantity=1)

    def _post_slice(self):
        from unittest import mock

        from django.urls import reverse

        with mock.patch("core.views.print_jobs._start_orcaslicer_worker") as worker:
            resp = self.client.post(reverse("core:printjob_slice", args=[self.job.pk]), follow=True)
        return resp, worker

    def test_reslice_refused_with_history(self):
        self._make_sliceable()
        resp, worker = self._post_slice()
        self.assertIn("has print history and cannot be re-sliced", " ".join(str(m) for m in resp.context["messages"]))
        worker.assert_not_called()
        self.assertTrue(PrintJobPlate.objects.filter(pk=self.plate.pk).exists())
        self.job.refresh_from_db()
        self.assertEqual(self.job.status, PrintJob.STATUS_SLICED)

    def test_reslice_allowed_without_history(self):
        self._make_sliceable()
        self.attempt.delete()
        resp, worker = self._post_slice()
        worker.assert_called_once()
        self.assertFalse(PrintJobPlate.objects.filter(pk=self.plate.pk).exists())

    def test_admin_cannot_delete_job_or_plate_with_history(self):
        from django.contrib.auth.models import User
        from django.urls import reverse

        User.objects.filter(pk=self.user.pk).update(is_staff=True, is_superuser=True)
        resp = self.client.post(reverse("admin:core_printjob_delete", args=[self.job.pk]), {"post": "yes"})
        self.assertEqual(resp.status_code, 403)
        resp = self.client.post(reverse("admin:core_printjobplate_delete", args=[self.plate.pk]), {"post": "yes"})
        self.assertEqual(resp.status_code, 403)
        resp = self.client.post(
            reverse("admin:core_printjob_changelist"),
            {"action": "delete_selected", "_selected_action": [self.job.pk], "post": "yes"},
        )
        self.assertTrue(PrintJob.objects.filter(pk=self.job.pk).exists())
        self.assertTrue(PrintQueue.objects.filter(pk=self.attempt.pk).exists())

    def test_job_with_print_start_in_progress_not_deletable_or_resliceable(self):
        from django.urls import reverse

        self.attempt.delete()
        claimed = self._attempt(start_claimed_at=timezone.now())
        self._make_sliceable()
        self.client.post(reverse("core:printjob_delete", args=[self.job.pk]))
        resp, worker = self._post_slice()
        self.assertIn("cannot be re-sliced", " ".join(str(m) for m in resp.context["messages"]))
        worker.assert_not_called()
        self.assertTrue(PrintQueue.objects.filter(pk=claimed.pk).exists())
        self.assertTrue(PrintJobPlate.objects.filter(pk=self.plate.pk).exists())

    def test_admin_cannot_delete_attempt_being_started(self):
        from django.contrib.auth.models import User
        from django.urls import reverse

        User.objects.filter(pk=self.user.pk).update(is_staff=True, is_superuser=True)
        claimed = self._attempt(self.plate2, start_claimed_at=timezone.now())
        resp = self.client.post(reverse("admin:core_printqueue_delete", args=[claimed.pk]), {"post": "yes"})
        self.assertEqual(resp.status_code, 403)
        self.assertTrue(PrintQueue.objects.filter(pk=claimed.pk).exists())

    def test_admin_delete_waiting_attempt_refreshes_plate(self):
        from django.contrib.auth.models import User
        from django.urls import reverse

        User.objects.filter(pk=self.user.pk).update(is_staff=True, is_superuser=True)
        for index in range(3):
            self._attempt(
                self.plate2,
                state="awaiting_review",
                result=PrintQueue.RESULT_FAILED,
                resolved_at=timezone.now(),
                is_retry=index > 0,
            )
        waiting = PrintQueue.enqueue(plate=self.plate2, printer=self.printer)
        self.plate2.refresh_from_db()
        self.assertEqual(self.plate2.status, PrintJobPlate.STATUS_WAITING)
        resp = self.client.post(reverse("admin:core_printqueue_delete", args=[waiting.pk]), {"post": "yes"})
        self.assertEqual(resp.status_code, 302)
        self.plate2.refresh_from_db()
        self.assertEqual(self.plate2.status, PrintJobPlate.STATUS_FAILED)

    def test_admin_history_fields_readonly(self):
        from core.admin import PrintQueueAdmin

        admin_obj = PrintQueueAdmin(PrintQueue, None)
        self.assertIn("plate", admin_obj.get_readonly_fields(None, self.attempt))
        waiting = self._attempt(self.plate2)
        readonly = admin_obj.get_readonly_fields(None, waiting)
        self.assertIn("result", readonly)  # transitions only via the model methods
        self.assertIn("started_at", readonly)
        self.assertNotIn("plate", readonly)

    def test_admin_bulk_delete_refuses_history_and_starting_attempts(self):
        from django.contrib.auth.models import User
        from django.urls import reverse

        User.objects.filter(pk=self.user.pk).update(is_staff=True, is_superuser=True)
        claimed = self._attempt(self.plate2, start_claimed_at=timezone.now())
        for selection in ([self.attempt.pk], [claimed.pk]):
            with self.subTest(selection=selection):
                resp = self.client.post(
                    reverse("admin:core_printqueue_changelist"),
                    {"action": "delete_selected", "_selected_action": selection, "post": "yes"},
                )
                self.assertEqual(resp.status_code, 403)
        self.assertTrue(PrintQueue.objects.filter(pk__in=[self.attempt.pk, claimed.pk]).count() == 2)


class QueueViewRaceTests(_AttemptTestBase):
    """Removing a mid-start attempt and enqueueing concurrently are refused cleanly."""

    def setUp(self):
        super().setUp()
        self.client.login(username="testuser", password="testpass123")

    def test_operator_cannot_remove_attempt_being_started(self):
        from django.urls import reverse

        attempt = self._attempt(start_claimed_at=timezone.now())
        resp = self.client.post(reverse("core:printqueue_delete", args=[attempt.pk]))
        self.assertEqual(resp.status_code, 404)
        self.assertTrue(PrintQueue.objects.filter(pk=attempt.pk).exists())

    def test_concurrent_enqueue_shows_error_instead_of_500(self):
        from unittest import mock

        from django.urls import reverse

        self.plate.gcode_file = "gcode_jobs/race.gcode"
        self.plate.save()
        original = PrintQueue.enqueue

        def racing_enqueue(**kwargs):
            original(**kwargs)  # the concurrent request wins
            return original(**kwargs)

        with mock.patch.object(PrintQueue, "enqueue", side_effect=racing_enqueue):
            resp = self.client.post(
                reverse("core:printqueue_create"),
                {"plate": self.plate.pk, "printer": self.printer.pk, "priority": 2},
                follow=True,
            )
        self.assertEqual(resp.status_code, 200)
        self.assertIn("already queued", " ".join(str(m) for m in resp.context["messages"]))
        self.assertEqual(PrintQueue.objects.open().filter(plate=self.plate).count(), 1)
