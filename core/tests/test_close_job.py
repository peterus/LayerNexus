"""Tests for closing a print job before all of its plates are printed (and reopening it).

A closed job is finished (display status ``closed``) although not every plate
passed: its remaining plates cannot be queued, its waiting attempts are removed
on close, and its printed copies keep counting exactly as before.
"""

from __future__ import annotations

from unittest import mock

from django.contrib.auth.models import Group, User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from core.models import Part, PrintJob, PrintJobPart, PrintJobPlate, PrintJobPlatePart, PrintQueue, Project
from core.tests.mixins import TestDataMixin, _RBACTestBase, add_attempt, complete_plate, fail_plate, make_sliced_job
from core.tests.test_reslice_requeue import _JobFixture


def _display(job: PrintJob) -> str:
    """Return the display status of ``job``; the property and the SQL annotation must agree."""
    prop = PrintJob.objects.get(pk=job.pk).display_status
    annotated = PrintJob.objects.with_display_status().get(pk=job.pk).annotated_display_status
    assert prop == annotated, f"property {prop!r} != annotation {annotated!r}"
    return prop


def _job_45(printer) -> PrintJob:
    """The prod case: plates 1, 2, 5, 6 passed, 3 failed without a retry, 4 never queued."""
    job = make_sliced_job("Job 45", plates=6)
    plates = {plate.plate_number: plate for plate in job.plates.all()}
    for number in (1, 2, 5, 6):
        complete_plate(plates[number], printer)
    add_attempt(plates[3], printer, PrintQueue.RESULT_FAILED, max_retries=3)
    return job


class CloseJobModelTests(TestDataMixin, TestCase):
    """``PrintJob.close()`` / ``reopen()`` / ``can_close`` and the derived status."""

    def test_job_45_is_in_progress_before_and_closed_after(self) -> None:
        job = _job_45(self.printer)
        self.assertEqual(_display(job), PrintJob.DISPLAY_IN_PROGRESS)
        self.assertTrue(PrintJob.objects.get(pk=job.pk).can_close)

        job.close(self.user)

        job.refresh_from_db()
        self.assertIsNotNone(job.closed_at)
        self.assertEqual(job.closed_by, self.user)
        self.assertTrue(job.is_closed)
        self.assertEqual(_display(job), PrintJob.DISPLAY_CLOSED)
        self.assertEqual(job.get_display_status_display(), "Closed (incomplete)")
        self.assertTrue(PrintJob.objects.get(pk=job.pk).is_finished)
        self.assertIn(job, PrintJob.objects.finished())
        self.assertNotIn(job, PrintJob.objects.unfinished())
        self.assertFalse(PrintJob.objects.get(pk=job.pk).can_close)

    def test_reopen_restores_the_previous_status(self) -> None:
        job = _job_45(self.printer)
        job.close(self.user)

        job.reopen()

        job.refresh_from_db()
        self.assertIsNone(job.closed_at)
        self.assertIsNone(job.closed_by)
        self.assertFalse(job.is_closed)
        self.assertEqual(_display(job), PrintJob.DISPLAY_IN_PROGRESS)
        self.assertIn(job, PrintJob.objects.unfinished())

    def test_reopen_of_an_open_job_raises(self) -> None:
        job = _job_45(self.printer)
        with self.assertRaises(ValueError):
            job.reopen()

    def test_ready_job_without_attempts_can_be_closed(self) -> None:
        job = make_sliced_job(plates=2)
        self.assertTrue(job.can_close)
        job.close(self.user)
        self.assertEqual(_display(job), PrintJob.DISPLAY_CLOSED)
        job.reopen()
        self.assertEqual(_display(job), PrintJob.DISPLAY_READY)

    def test_unsliced_jobs_cannot_be_closed(self) -> None:
        for status in (
            PrintJob.STATUS_DRAFT,
            PrintJob.STATUS_PENDING,
            PrintJob.STATUS_SLICING,
            PrintJob.STATUS_FAILED,
        ):
            with self.subTest(status=status):
                job = PrintJob.objects.create(name=status, status=status)
                self.assertFalse(job.can_close)
                with self.assertRaises(ValueError):
                    job.close(self.user)
                job.refresh_from_db()
                self.assertIsNone(job.closed_at)

    def test_finished_jobs_cannot_be_closed(self) -> None:
        completed = make_sliced_job("Completed", plates=1)
        complete_plate(completed.plates.get(), self.printer)
        partially = make_sliced_job("Partially failed", plates=2)
        complete_plate(partially.plates.first(), self.printer)
        fail_plate(partially.plates.last(), self.printer)
        for job in (completed, partially):
            with self.subTest(job=job.name):
                job = PrintJob.objects.get(pk=job.pk)
                self.assertFalse(job.can_close)
                with self.assertRaises(ValueError):
                    job.close(self.user)

    def test_closed_job_cannot_be_closed_again(self) -> None:
        job = _job_45(self.printer)
        job.close(self.user)
        with self.assertRaises(ValueError):
            PrintJob.objects.get(pk=job.pk).close(self.user)

    def test_active_or_starting_plate_blocks_close(self) -> None:
        for state in ("printing", "awaiting_review", "starting"):
            with self.subTest(state=state):
                job = make_sliced_job(state, plates=2)
                complete_plate(job.plates.first(), self.printer)
                if state == "starting":
                    add_attempt(job.plates.last(), self.printer, "waiting", start_claimed_at=timezone.now())
                else:
                    add_attempt(job.plates.last(), self.printer, state)
                for loaded in (
                    PrintJob.objects.get(pk=job.pk),
                    PrintJob.objects.with_display_status().get(pk=job.pk),
                    PrintJob.objects.prefetch_related("plates__queue_entries").get(pk=job.pk),
                ):
                    self.assertFalse(loaded.can_close)
                with self.assertRaises(ValueError):
                    job.close(self.user)
                job.refresh_from_db()
                self.assertIsNone(job.closed_at)

    def test_close_removes_waiting_attempts(self) -> None:
        job = make_sliced_job(plates=3)
        first, second, third = job.plates.order_by("plate_number")
        complete_plate(first, self.printer)
        waiting = add_attempt(second, self.printer, "waiting")
        # An expired start claim does not keep the printer busy: the attempt is just waiting.
        expired = timezone.now() - timezone.timedelta(hours=1)
        stale = add_attempt(third, self.printer, "waiting", start_claimed_at=expired)
        self.assertTrue(job.can_close)

        removed = job.close(self.user)

        self.assertEqual(removed, 2)
        self.assertFalse(PrintQueue.objects.filter(pk__in=[waiting.pk, stale.pk]).exists())
        self.assertEqual(PrintQueue.objects.filter(plate__print_job=job).count(), 1)
        self.assertEqual(_display(job), PrintJob.DISPLAY_CLOSED)

    def test_waiting_attempts_and_open_plates_are_listed_for_the_confirmation(self) -> None:
        job = _job_45(self.printer)
        plate4 = job.plates.get(plate_number=4)
        add_attempt(plate4, self.printer, "waiting")
        job = PrintJob.objects.get(pk=job.pk)
        self.assertEqual([plate.plate_number for plate in job.open_plates()], [3, 4])
        self.assertEqual(job.waiting_attempt_count(), 1)

    def test_display_status_of_all_paths_agree(self) -> None:
        """Annotation, plain property and prefetched property give the same closed status."""
        job = _job_45(self.printer)
        job.close(self.user)
        prefetched = PrintJob.objects.prefetch_related("plates__queue_entries").get(pk=job.pk)
        self.assertEqual(prefetched.display_status, PrintJob.DISPLAY_CLOSED)
        self.assertEqual(_display(job), PrintJob.DISPLAY_CLOSED)

    def test_closed_job_whose_plates_all_passed_stays_completed(self) -> None:
        """A closed flag set on a job that is completed anyway (e.g. via admin) shows ``completed``."""
        job = make_sliced_job(plates=1)
        complete_plate(job.plates.get(), self.printer)
        PrintJob.objects.filter(pk=job.pk).update(closed_at=timezone.now())
        self.assertEqual(_display(job), PrintJob.DISPLAY_COMPLETED)

    def test_status_filter_and_counts_include_closed(self) -> None:
        job = _job_45(self.printer)
        job.close(self.user)
        self.assertIn(PrintJob.DISPLAY_CLOSED, dict(PrintJob.DISPLAY_CHOICES))
        self.assertEqual(
            list(PrintJob.objects.with_display_status().filter(annotated_display_status=PrintJob.DISPLAY_CLOSED)),
            [job],
        )


class ClosedJobGuardTests(TestDataMixin, TestCase):
    """A closed job's plates are not queueable and its contents are fixed."""

    def setUp(self) -> None:
        super().setUp()
        self.job = _job_45(self.printer)
        self.job.plates.update(gcode_file="gcode_jobs/plate.gcode")
        self.job.close(self.user)
        self.job = PrintJob.objects.get(pk=self.job.pk)

    def test_no_plate_is_queueable(self) -> None:
        self.assertFalse(PrintJobPlate.objects.queueable().filter(print_job=self.job).exists())
        for plate in self.job.plates.all():
            self.assertFalse(plate.is_queueable)
        self.job.reopen()
        self.assertEqual(
            sorted(PrintJobPlate.objects.queueable().filter(print_job=self.job).values_list("plate_number", flat=True)),
            [3, 4],
        )

    def test_enqueue_refuses_a_plate_of_a_closed_job(self) -> None:
        plate = self.job.plates.get(plate_number=4)
        with self.assertRaises(ValueError):
            PrintQueue.enqueue(plate=plate, printer=self.printer)
        self.assertFalse(PrintQueue.objects.filter(plate=plate).exists())

    def test_enqueue_plates_queues_nothing(self) -> None:
        self.assertEqual(self.job.enqueue_plates(printer=self.printer), [])
        self.assertFalse(PrintQueue.objects.filter(plate__print_job=self.job, result="").exists())

    def test_contents_and_reslice_are_locked(self) -> None:
        ready = make_sliced_job("Ready", plates=1)
        ready.close(self.user)
        ready = PrintJob.objects.get(pk=ready.pk)
        self.assertFalse(ready.can_change_contents)
        self.assertFalse(ready.can_reslice)
        self.assertFalse(ready.can_reslice_and_requeue)
        self.assertNotIn(ready, PrintJob.objects.contents_editable())
        with self.assertRaises(ValueError):
            ready.discard_slicing()
        ready.reopen()
        self.assertTrue(PrintJob.objects.get(pk=ready.pk).can_change_contents)

    def test_closed_job_can_still_be_deleted(self) -> None:
        self.assertTrue(self.job.can_delete)


class ClosedJobCountingTests(TestCase):
    """Closing changes nothing about what counts as printed."""

    def setUp(self) -> None:
        self.user = User.objects.create_user(username="closer", password="pw")
        project = Project.objects.create(name="P")
        self.part = Part.objects.create(name="R02", project=project, quantity=10)
        self.job = make_sliced_job(plates=3)
        PrintJobPart.objects.create(print_job=self.job, part=self.part, quantity=3)
        for copy_index, plate in enumerate(self.job.plates.order_by("plate_number")):
            PrintJobPlatePart.objects.create(plate=plate, part=self.part, copy_index=copy_index)
        plate1, plate2, _plate3 = self.job.plates.order_by("plate_number")
        complete_plate(plate1)
        fail_plate(plate2)

    def _printed(self) -> int:
        part = Part.objects.prefetch_related(*Part.printed_quantity_prefetch_lookups()).get(pk=self.part.pk)
        self.assertEqual(part.printed_quantity, Part.objects.get(pk=self.part.pk).printed_quantity)
        return part.printed_quantity

    def test_printed_quantity_unchanged_by_close_and_reopen(self) -> None:
        self.assertEqual(self._printed(), 1)
        self.assertEqual(self.job.printed_copies(), 1)
        self.job.close(self.user)
        self.assertEqual(self._printed(), 1)
        self.assertEqual(PrintJob.objects.get(pk=self.job.pk).printed_copies(), 1)
        self.job.reopen()
        self.assertEqual(self._printed(), 1)

    def test_legacy_job_without_plate_parts_still_counts_nothing(self) -> None:
        PrintJob.objects.filter(pk=self.job.pk).update(plate_parts_known=False)
        PrintJobPlatePart.objects.filter(plate__print_job=self.job).delete()
        self.assertEqual(self._printed(), 0)
        PrintJob.objects.get(pk=self.job.pk).close(self.user)
        self.assertEqual(self._printed(), 0)


class CloseJobViewTests(_RBACTestBase):
    """``PrintJobCloseView`` / ``PrintJobReopenView``: POST only, role-gated."""

    def setUp(self) -> None:
        super().setUp()
        self.job = _job_45(self.printer)
        self.close_url = reverse("core:printjob_close", kwargs={"pk": self.job.pk})
        self.reopen_url = reverse("core:printjob_reopen", kwargs={"pk": self.job.pk})
        self.detail_url = reverse("core:printjob_detail", kwargs={"pk": self.job.pk})
        self.viewer = User.objects.create_user(username="viewer", password="testpass123")

    def _login(self, user: User) -> None:
        self.client.force_login(user)

    def test_operator_and_designer_can_close_and_reopen(self) -> None:
        for user in (self.operator_user, self.designer_user, self.admin_user):
            with self.subTest(user=user.username):
                self._login(user)
                response = self.client.post(self.close_url)
                self.assertRedirects(response, self.detail_url, fetch_redirect_response=False)
                self.assertIsNotNone(PrintJob.objects.get(pk=self.job.pk).closed_at)
                self.assertEqual(PrintJob.objects.get(pk=self.job.pk).closed_by, user)
                response = self.client.post(self.reopen_url)
                self.assertRedirects(response, self.detail_url, fetch_redirect_response=False)
                self.assertIsNone(PrintJob.objects.get(pk=self.job.pk).closed_at)

    def test_user_without_role_gets_403(self) -> None:
        self._login(self.viewer)
        self.assertEqual(self.client.post(self.close_url).status_code, 403)
        self.assertIsNone(PrintJob.objects.get(pk=self.job.pk).closed_at)
        self.job.close(self.admin_user)
        self.assertEqual(self.client.post(self.reopen_url).status_code, 403)
        self.assertIsNotNone(PrintJob.objects.get(pk=self.job.pk).closed_at)

    def test_close_needs_dequeue_permission(self) -> None:
        group = Group.objects.create(name="Job editors")
        group.permissions.add(*Group.objects.get(name="Designer").permissions.filter(codename="change_printjob"))
        self.viewer.groups.add(group)
        self._login(self.viewer)
        self.assertEqual(self.client.post(self.close_url).status_code, 403)

    def test_anonymous_is_denied(self) -> None:
        # RoleRequiredMixin raises (403) for anonymous users too, see test_permissions.
        self.assertEqual(self.client.post(self.close_url).status_code, 403)
        self.assertIsNone(PrintJob.objects.get(pk=self.job.pk).closed_at)

    def test_get_is_not_allowed(self) -> None:
        self._login(self.admin_user)
        self.assertEqual(self.client.get(self.close_url).status_code, 405)
        self.assertEqual(self.client.get(self.reopen_url).status_code, 405)
        self.assertIsNone(PrintJob.objects.get(pk=self.job.pk).closed_at)

    def test_refused_close_shows_an_error(self) -> None:
        add_attempt(self.job.plates.get(plate_number=4), self.printer, "printing")
        self._login(self.admin_user)
        response = self.client.post(self.close_url, follow=True)
        self.assertIsNone(PrintJob.objects.get(pk=self.job.pk).closed_at)
        self.assertContains(response, "cannot be closed")

    def test_close_reports_removed_waiting_attempts(self) -> None:
        add_attempt(self.job.plates.get(plate_number=4), self.printer, "waiting")
        self._login(self.admin_user)
        response = self.client.post(self.close_url, follow=True)
        self.assertContains(response, "1 waiting queue entry removed")
        self.assertFalse(PrintQueue.objects.filter(plate__print_job=self.job, result="").exists())

    def test_refused_reopen_shows_an_error(self) -> None:
        self._login(self.admin_user)
        response = self.client.post(self.reopen_url, follow=True)
        self.assertContains(response, "is not closed")

    def test_unknown_job_is_404(self) -> None:
        self._login(self.admin_user)
        self.assertEqual(self.client.post(reverse("core:printjob_close", kwargs={"pk": 999999})).status_code, 404)


class CloseJobTemplateTests(_RBACTestBase):
    """Buttons and badges on the job pages."""

    def setUp(self) -> None:
        super().setUp()
        self.job = _job_45(self.printer)
        self.detail_url = reverse("core:printjob_detail", kwargs={"pk": self.job.pk})
        self.close_url = reverse("core:printjob_close", kwargs={"pk": self.job.pk})
        self.reopen_url = reverse("core:printjob_reopen", kwargs={"pk": self.job.pk})

    def test_detail_offers_close_with_open_plates(self) -> None:
        self.client.force_login(self.designer_user)
        response = self.client.get(self.detail_url)
        self.assertContains(response, self.close_url)
        self.assertContains(response, "closeJobModal")
        self.assertContains(response, "Plate 3")
        self.assertContains(response, "Plate 4")
        self.assertNotContains(response, self.reopen_url)

    def test_detail_offers_reopen_on_closed_job(self) -> None:
        self.job.close(self.designer_user)
        self.client.force_login(self.designer_user)
        response = self.client.get(self.detail_url)
        self.assertContains(response, self.reopen_url)
        self.assertNotContains(response, self.close_url)
        self.assertContains(response, "Closed (incomplete)")
        self.assertContains(response, "designer_user")

    def test_no_buttons_without_permission(self) -> None:
        viewer = User.objects.create_user(username="viewer", password="testpass123")
        self.client.force_login(viewer)
        response = self.client.get(self.detail_url)
        self.assertNotContains(response, self.close_url)
        self.job.close(self.admin_user)
        response = self.client.get(self.detail_url)
        self.assertNotContains(response, self.reopen_url)

    def test_no_close_button_while_a_plate_prints(self) -> None:
        add_attempt(self.job.plates.get(plate_number=4), self.printer, "printing")
        self.client.force_login(self.designer_user)
        self.assertNotContains(self.client.get(self.detail_url), self.close_url)

    def test_list_shows_closed_badge_and_filter(self) -> None:
        self.job.close(self.designer_user)
        self.client.force_login(self.designer_user)
        response = self.client.get(reverse("core:printjob_list"), {"status": PrintJob.DISPLAY_CLOSED})
        self.assertContains(response, "Closed (incomplete)")
        self.assertIn(self.job, list(response.context["print_jobs"]))


class ClosedJobDeleteGuardTests(TestCase):
    """A closed job keeps blocking the deletion of its parts unless it is print history."""

    def setUp(self) -> None:
        self.project = Project.objects.create(name="P")
        self.part = Part.objects.create(name="p", project=self.project)

    def _job_with_part(self) -> PrintJob:
        job = make_sliced_job(plates=2)
        PrintJobPart.objects.create(print_job=job, part=self.part, quantity=1)
        return job

    def test_closed_job_without_attempts_blocks_like_a_draft(self) -> None:
        job = self._job_with_part()
        job.close()
        self.assertEqual(list(self.part.blocking_jobs()), [job])
        self.assertEqual(list(self.project.blocking_jobs()), [job])
        self.assertFalse(self.part.has_print_history())

    def test_closed_job_with_history_is_guarded_as_history(self) -> None:
        job = self._job_with_part()
        complete_plate(job.plates.first())
        job.close()
        self.assertFalse(self.part.blocking_jobs().exists())
        self.assertFalse(self.project.blocking_jobs().exists())
        self.assertTrue(self.part.has_print_history())

    def test_part_delete_page_names_the_closed_job(self) -> None:
        job = self._job_with_part()
        job.close()
        user = User.objects.create_user(username="d", password="pw")
        user.groups.add(Group.objects.get(name="Designer"))
        self.client.force_login(user)
        response = self.client.post(reverse("core:part_delete", kwargs={"pk": self.part.pk}), follow=True)
        self.assertTrue(Part.objects.filter(pk=self.part.pk).exists())
        response = self.client.get(reverse("core:part_delete", kwargs={"pk": self.part.pk}))
        self.assertContains(response, reverse("core:printjob_detail", kwargs={"pk": job.pk}))


class ClosedJobSliceAndQueueViewTests(_JobFixture):
    """Re-slicing and single-plate queueing refuse a closed job, also when it closed meanwhile."""

    def test_reslice_refuses_a_closed_job(self) -> None:
        self.job.close()
        with mock.patch("core.views.print_jobs._start_orcaslicer_worker") as worker:
            response = self._client().post(reverse("core:printjob_slice", kwargs={"pk": self.job.pk}), follow=True)
        worker.assert_not_called()
        self.assertContains(response, "is closed and cannot be re-sliced")
        self.assertEqual(PrintJob.objects.get(pk=self.job.pk).status, PrintJob.STATUS_SLICED)
        self.assertEqual(self.job.plates.count(), 3)

    def test_reslice_rechecks_a_job_closed_after_it_was_loaded(self) -> None:
        stale = PrintJob.objects.prefetch_related("job_parts__part").get(pk=self.job.pk)
        self.job.close()
        with (
            mock.patch("core.views.print_jobs.get_object_or_404", return_value=stale),
            mock.patch("core.views.print_jobs._start_orcaslicer_worker") as worker,
        ):
            self._client().post(reverse("core:printjob_slice", kwargs={"pk": self.job.pk}))
        worker.assert_not_called()
        job = PrintJob.objects.get(pk=self.job.pk)
        self.assertEqual(job.status, PrintJob.STATUS_SLICED)
        self.assertTrue(job.is_closed)
        self.assertEqual(job.plates.count(), 3)

    def test_queue_create_reports_a_job_closed_meanwhile(self) -> None:
        PrintJob.objects.filter(pk=self.job.pk).update(requeue_error="not re-queued")
        with mock.patch("core.views.queue.PrintQueue.enqueue", side_effect=ValueError("closed")):
            response = self._client().post(
                reverse("core:printqueue_create"),
                {"plate": self.plates[0].pk, "printer": self.printer.pk, "priority": 2},
                follow=True,
            )
        self.assertRedirects(response, reverse("core:printqueue_list"))
        self.assertContains(response, "is closed; its plates cannot be queued")
        self.assertFalse(PrintQueue.objects.filter(plate=self.plates[0]).exists())
        self.assertEqual(PrintJob.objects.get(pk=self.job.pk).requeue_error, "not re-queued")


class ClosedJobAdminTests(TestCase):
    """The admin cannot bypass ``close()`` / ``reopen()``."""

    def test_closed_fields_are_read_only(self) -> None:
        from django.contrib.admin import site

        job = make_sliced_job(plates=1)
        for obj in (None, job):
            with self.subTest(obj=obj):
                readonly = site._registry[PrintJob].get_readonly_fields(None, obj)
                self.assertIn("closed_at", readonly)
                self.assertIn("closed_by", readonly)
