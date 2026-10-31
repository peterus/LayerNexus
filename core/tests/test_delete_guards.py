"""Tests for the print-job guards (unfinished jobs, print history) on part deletes and on Django admin deletes."""

from unittest import mock

from django.contrib.admin.helpers import ACTION_CHECKBOX_NAME
from django.contrib.auth.models import Group, User
from django.test import TestCase
from django.urls import reverse
from rest_framework.authtoken.models import Token
from rest_framework.test import APITestCase

from core.models import OrcaPrintPreset, Part, PrintJob, PrintJobPart, Project
from core.tests.mixins import add_attempt, complete_plate, fail_plate, make_sliced_job

#: Display statuses of unfinished jobs (every one blocks the delete of its parts).
UNFINISHED = ["draft", "pending", "slicing", "slicing_failed", "ready", "printing", "in_progress"]

_SLICING_STATUS = {
    PrintJob.DISPLAY_DRAFT: PrintJob.STATUS_DRAFT,
    PrintJob.DISPLAY_PENDING: PrintJob.STATUS_PENDING,
    PrintJob.DISPLAY_SLICING: PrintJob.STATUS_SLICING,
    PrintJob.DISPLAY_SLICING_FAILED: PrintJob.STATUS_FAILED,
}


def job_in_display_status(display_status: str, *parts: Part) -> PrintJob:
    """Create a print job in the given display status that contains ``parts``.

    Jobs without print attempts get the slicing status; ``ready`` and later states
    are sliced jobs whose plates get print attempts.
    """
    if display_status in _SLICING_STATUS:
        job = PrintJob.objects.create(name=f"job-{display_status}", status=_SLICING_STATUS[display_status])
    else:
        job = make_sliced_job(name=f"job-{display_status}", plates=2)
        plate1, plate2 = job.plates.order_by("plate_number")
        if display_status == PrintJob.DISPLAY_PRINTING:
            add_attempt(plate1, None, "printing")
        elif display_status == PrintJob.DISPLAY_IN_PROGRESS:
            complete_plate(plate1)
        elif display_status == PrintJob.DISPLAY_COMPLETED:
            complete_plate(plate1)
            complete_plate(plate2)
        elif display_status == PrintJob.DISPLAY_PARTIALLY_FAILED:
            complete_plate(plate1)
            fail_plate(plate2)
        elif display_status != PrintJob.DISPLAY_READY:
            raise ValueError(f"Unknown display status: {display_status}")
    for part in parts:
        PrintJobPart.objects.create(print_job=job, part=part, quantity=1)
    return job


class PartBlockingJobsTests(TestCase):
    """``Part.blocking_jobs`` uses the same "unfinished" definition as the project guard."""

    def setUp(self) -> None:
        self.project = Project.objects.create(name="R")
        self.part = Part.objects.create(name="p", project=self.project)
        self.other = Part.objects.create(name="other", project=self.project)

    def test_job_helper_builds_requested_display_status(self) -> None:
        for display_status, _ in PrintJob.DISPLAY_CHOICES:
            with self.subTest(display_status=display_status):
                job = job_in_display_status(display_status)
                self.assertEqual(PrintJob.objects.get(pk=job.pk).display_status, display_status)
                self.assertEqual(PrintJob.objects.with_display_status().get(pk=job.pk).display_status, display_status)

    def test_unfinished_statuses_block(self) -> None:
        for status in UNFINISHED:
            with self.subTest(status=status):
                job = job_in_display_status(status, self.part)
                self.assertEqual(list(self.part.blocking_jobs()), [job])
                job.delete()

    def test_finished_statuses_and_other_parts_do_not_block(self) -> None:
        for status in PrintJob.FINISHED_DISPLAY_STATUSES:
            job_in_display_status(status, self.part)
        job_in_display_status(PrintJob.DISPLAY_PRINTING, self.other)
        self.assertFalse(self.part.blocking_jobs().exists())

    def test_statuses_match_project_guard(self) -> None:
        self.assertEqual(
            set(UNFINISHED) | set(PrintJob.FINISHED_DISPLAY_STATUSES), {s for s, _ in PrintJob.DISPLAY_CHOICES}
        )

    def test_print_history_is_any_job_with_attempts(self) -> None:
        """Jobs with print attempts are the part's history — finished or not; jobs without are not."""
        for status in ("draft", "slicing_failed", "ready"):
            job_in_display_status(status, self.part)
        self.assertFalse(self.part.has_print_history())
        history = {
            job_in_display_status(status, self.part)
            for status in ("printing", "in_progress", "completed", "partially_failed")
        }
        job_in_display_status(PrintJob.DISPLAY_COMPLETED, self.other)
        self.assertEqual(set(self.part.history_jobs()), history)
        self.assertTrue(self.part.has_print_history())


class PartDeleteViewGuardTests(TestCase):
    """The web part delete refuses while a job contains the part (unfinished or print history)."""

    def setUp(self) -> None:
        self.user = User.objects.create_user(username="deleter", password="testpass123")
        self.user.groups.add(Group.objects.get(name="Designer"))
        self.client.login(username="deleter", password="testpass123")
        self.project = Project.objects.create(name="R")
        self.part = Part.objects.create(name="p", project=self.project)
        self.url = reverse("core:part_delete", args=[self.part.pk])

    def test_confirm_page_lists_blocking_jobs_and_hides_submit(self) -> None:
        job = job_in_display_status(PrintJob.DISPLAY_READY, self.part)
        resp = self.client.get(self.url)
        self.assertEqual(resp.context["blocking_jobs"], [job])
        self.assertEqual(resp.context["history_jobs"], [])
        self.assertContains(resp, reverse("core:printjob_detail", args=[job.pk]))
        self.assertNotContains(resp, "btn btn-danger")

    def test_post_with_unfinished_job_is_refused(self) -> None:
        for status in UNFINISHED:
            with self.subTest(status=status):
                job = job_in_display_status(status, self.part)
                resp = self.client.post(self.url, follow=True)
                self.assertRedirects(resp, self.url)
                self.assertContains(resp, "cannot be deleted")
                self.assertTrue(Part.objects.filter(pk=self.part.pk).exists())
                job.delete()

    def test_post_rechecks_after_get(self) -> None:
        resp = self.client.get(self.url)
        self.assertEqual(resp.context["blocking_jobs"], [])
        job_in_display_status(PrintJob.DISPLAY_DRAFT, self.part)
        self.client.post(self.url)
        self.assertTrue(Part.objects.filter(pk=self.part.pk).exists())

    def test_finished_job_refuses_delete_and_keeps_history(self) -> None:
        """A finished job is print history: the part cannot be deleted (was: deleted, history dropped)."""
        for status in PrintJob.FINISHED_DISPLAY_STATUSES:
            with self.subTest(status=status):
                job = job_in_display_status(status, self.part)
                resp = self.client.post(self.url, follow=True)
                self.assertRedirects(resp, self.url)
                self.assertContains(resp, "it has print history")
                self.assertTrue(Part.objects.filter(pk=self.part.pk).exists())
                self.assertTrue(job.job_parts.filter(part=self.part).exists())
                job.delete()

    def test_part_without_jobs_is_deleted(self) -> None:
        resp = self.client.post(self.url)
        self.assertRedirects(resp, reverse("core:project_detail", args=[self.project.pk]))
        self.assertFalse(Part.objects.filter(pk=self.part.pk).exists())

    def test_confirm_page_lists_print_history_and_hides_submit(self) -> None:
        job = job_in_display_status(PrintJob.DISPLAY_COMPLETED, self.part)
        resp = self.client.get(self.url)
        self.assertEqual(resp.context["history_jobs"], [job])
        self.assertEqual(resp.context["blocking_jobs"], [])
        self.assertContains(resp, "print history")
        self.assertContains(resp, reverse("core:printjob_detail", args=[job.pk]))
        self.assertNotContains(resp, "btn btn-danger")


class PartApiDeleteGuardTests(APITestCase):
    """``DELETE /api/v1/parts/{id}/`` answers 409 with the reason and the blocking job ids."""

    def setUp(self) -> None:
        user = User.objects.create_user(username="api", password="x")
        user.groups.add(Group.objects.get(name="Designer"))
        token = Token.objects.create(user=user)
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {token.key}")
        self.project = Project.objects.create(name="R")
        self.part = Part.objects.create(name="p", project=self.project)

    def test_delete_with_unfinished_job_409(self) -> None:
        job = job_in_display_status(PrintJob.DISPLAY_READY, self.part)
        resp = self.client.delete(f"/api/v1/parts/{self.part.pk}/")
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.data["reason"], "unfinished_jobs")
        self.assertEqual(resp.data["blocking_jobs"], [job.pk])
        self.assertTrue(Part.objects.filter(pk=self.part.pk).exists())

    def test_delete_with_printing_job_409_print_history(self) -> None:
        job = job_in_display_status(PrintJob.DISPLAY_PRINTING, self.part)
        resp = self.client.delete(f"/api/v1/parts/{self.part.pk}/")
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.data["reason"], "print_history")
        self.assertEqual(resp.data["blocking_jobs"], [job.pk])
        self.assertTrue(Part.objects.filter(pk=self.part.pk).exists())

    def test_delete_with_finished_job_409_print_history(self) -> None:
        """A finished job is print history: refused (was: 204, part deleted)."""
        job = job_in_display_status(PrintJob.DISPLAY_COMPLETED, self.part)
        resp = self.client.delete(f"/api/v1/parts/{self.part.pk}/")
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.data["reason"], "print_history")
        self.assertEqual(resp.data["blocking_jobs"], [job.pk])
        self.assertTrue(Part.objects.filter(pk=self.part.pk).exists())

    def test_delete_with_slicing_failed_job_409(self) -> None:
        """A slicing-failed job is unfinished now (was: terminal, 204)."""
        job = job_in_display_status(PrintJob.DISPLAY_SLICING_FAILED, self.part)
        resp = self.client.delete(f"/api/v1/parts/{self.part.pk}/")
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.data["reason"], "unfinished_jobs")
        self.assertEqual(resp.data["blocking_jobs"], [job.pk])

    def test_delete_without_jobs_204(self) -> None:
        resp = self.client.delete(f"/api/v1/parts/{self.part.pk}/")
        self.assertEqual(resp.status_code, 204)
        self.assertFalse(Part.objects.filter(pk=self.part.pk).exists())


class AdminDeleteGuardTests(TestCase):
    """Django admin refuses to delete projects/parts that print jobs still contain."""

    def setUp(self) -> None:
        self.admin = User.objects.create_superuser(username="root", password="pw", email="r@example.com")
        self.client.login(username="root", password="pw")
        self.root = Project.objects.create(name="R")
        self.sub = Project.objects.create(name="S", parent=self.root)
        self.part = Part.objects.create(name="p", project=self.sub)
        self.free = Project.objects.create(name="Free")
        self.free_part = Part.objects.create(name="free part", project=self.free)
        self.job = job_in_display_status(PrintJob.DISPLAY_READY, self.part)

    def _bulk_delete(self, model: str, *pks: int):  # noqa: ANN202
        return self.client.post(
            reverse(f"admin:core_{model}_changelist"),
            {"action": "delete_selected", ACTION_CHECKBOX_NAME: [str(pk) for pk in pks], "post": "yes"},
        )

    def test_project_delete_view_blocked(self) -> None:
        url = reverse("admin:core_project_delete", args=[self.root.pk])
        resp = self.client.get(url)
        self.assertContains(resp, "unfinished print job")
        self.client.post(url, {"post": "yes"})
        self.assertTrue(Project.objects.filter(pk=self.root.pk).exists())

    def test_part_delete_view_blocked(self) -> None:
        url = reverse("admin:core_part_delete", args=[self.part.pk])
        resp = self.client.get(url)
        self.assertContains(resp, "unfinished print job")
        self.client.post(url, {"post": "yes"})
        self.assertTrue(Part.objects.filter(pk=self.part.pk).exists())

    def test_project_bulk_delete_blocked(self) -> None:
        resp = self._bulk_delete("project", self.root.pk, self.free.pk)
        self.assertContains(resp, "unfinished print job")
        self.assertTrue(Project.objects.filter(pk__in=[self.root.pk, self.free.pk]).count() == 2)

    def test_part_bulk_delete_blocked(self) -> None:
        resp = self._bulk_delete("part", self.part.pk, self.free_part.pk)
        self.assertContains(resp, "unfinished print job")
        self.assertEqual(Part.objects.filter(pk__in=[self.part.pk, self.free_part.pk]).count(), 2)

    def test_print_history_blocks_delete(self) -> None:
        """A finished job (print history) blocks too (was: finishing the job unblocked the delete)."""
        self.job.delete()
        job_in_display_status(PrintJob.DISPLAY_COMPLETED, self.part)
        for url in (
            reverse("admin:core_part_delete", args=[self.part.pk]),
            reverse("admin:core_project_delete", args=[self.root.pk]),
        ):
            with self.subTest(url=url):
                self.assertContains(self.client.get(url), "has print history")
                self.client.post(url, {"post": "yes"})
        self.assertContains(self._bulk_delete("part", self.part.pk), "has print history")
        self.assertTrue(Part.objects.filter(pk=self.part.pk).exists())
        self.assertTrue(Project.objects.filter(pk=self.root.pk).exists())

    def test_unblocked_deletes_still_work(self) -> None:
        self.client.post(reverse("admin:core_project_delete", args=[self.free.pk]), {"post": "yes"})
        self.assertFalse(Project.objects.filter(pk=self.free.pk).exists())
        self.job.delete()
        self._bulk_delete("part", self.part.pk)
        self.assertFalse(Part.objects.filter(pk=self.part.pk).exists())

    def test_race_between_confirm_and_delete_is_refused(self) -> None:
        """A job that appears after the confirmation check still blocks the delete.

        Since ``PrintJobPart.part`` is ``PROTECT``, Django's own collector lists the
        job entries as protected too, so the whole confirmation result is mocked
        empty to reach the re-check inside the delete transaction.
        """
        from core.admin import PartAdmin

        with mock.patch.object(PartAdmin, "get_deleted_objects", return_value=([], {}, set(), [])):
            resp = self.client.post(reverse("admin:core_part_delete", args=[self.part.pk]), {"post": "yes"})
        self.assertEqual(resp.status_code, 403)
        self.assertTrue(Part.objects.filter(pk=self.part.pk).exists())


@mock.patch("core.views.helpers._start_orcaslicer_worker")
class ApiMoveRequeueTests(APITestCase):
    """``PATCH parent`` / ``PATCH project`` re-queue parts whose effective preset changed."""

    def setUp(self) -> None:
        user = User.objects.create_user(username="api", password="x")
        user.groups.add(Group.objects.get(name="Designer"))
        token = Token.objects.create(user=user)
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {token.key}")
        self.preset_x = OrcaPrintPreset.objects.create(
            name="X", orca_name="X", state=OrcaPrintPreset.STATE_RESOLVED, instantiation=True
        )
        self.preset_y = OrcaPrintPreset.objects.create(
            name="Y", orca_name="Y", state=OrcaPrintPreset.STATE_RESOLVED, instantiation=True
        )
        self.root = Project.objects.create(name="R", default_print_preset=self.preset_x)
        self.sub = Project.objects.create(name="S", parent=self.root)
        self.other = Project.objects.create(name="B", default_print_preset=self.preset_y)
        self.part = Part.objects.create(
            name="p",
            project=self.sub,
            stl_file="stl_files/p.stl",
            estimation_status=Part.ESTIMATION_SUCCESS,
            estimated_with_preset=self.preset_x,
        )

    def test_patch_parent_requeues(self, worker: mock.Mock) -> None:
        resp = self.client.patch(f"/api/v1/projects/{self.sub.pk}/", {"parent": self.other.pk}, format="json")
        self.assertEqual(resp.status_code, 200)
        self.part.refresh_from_db()
        self.assertEqual(self.part.estimation_status, Part.ESTIMATION_PENDING)
        worker.assert_called()

    def test_patch_project_requeues(self, worker: mock.Mock) -> None:
        resp = self.client.patch(f"/api/v1/parts/{self.part.pk}/", {"project": self.other.pk}, format="json")
        self.assertEqual(resp.status_code, 200)
        self.part.refresh_from_db()
        self.assertEqual(self.part.estimation_status, Part.ESTIMATION_PENDING)

    def test_patch_name_does_not_requeue(self, worker: mock.Mock) -> None:
        self.client.patch(f"/api/v1/projects/{self.sub.pk}/", {"name": "S2"}, format="json")
        self.client.patch(f"/api/v1/parts/{self.part.pk}/", {"name": "p2"}, format="json")
        self.part.refresh_from_db()
        self.assertEqual(self.part.estimation_status, Part.ESTIMATION_SUCCESS)
        worker.assert_not_called()

    def test_patch_parent_rechecks_cycle_inside_transaction(self, worker: mock.Mock) -> None:
        """A cycle that appears after serializer validation is still refused (400, nothing saved)."""
        from core.api.serializers import ProjectSerializer

        leaf = Project.objects.create(name="L", parent=self.sub)
        original = ProjectSerializer.validate

        def _race(serializer: ProjectSerializer, attrs: dict) -> dict:
            attrs = original(serializer, attrs)
            Project.objects.filter(pk=self.other.pk).update(parent=leaf)  # concurrent move
            return attrs

        with mock.patch.object(ProjectSerializer, "validate", _race):
            resp = self.client.patch(f"/api/v1/projects/{self.sub.pk}/", {"parent": self.other.pk}, format="json")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("parent", resp.data)
        self.sub.refresh_from_db()
        self.assertEqual(self.sub.parent, self.root)
