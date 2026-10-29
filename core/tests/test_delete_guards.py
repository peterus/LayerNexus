"""Tests for the unfinished-print-job guard on part deletes and on Django admin deletes."""

from unittest import mock

from django.contrib.admin.helpers import ACTION_CHECKBOX_NAME
from django.contrib.auth.models import Group, User
from django.test import TestCase
from django.urls import reverse
from rest_framework.authtoken.models import Token
from rest_framework.test import APITestCase

from core.models import OrcaPrintPreset, Part, PrintJob, PrintJobPart, Project

UNFINISHED = ["draft", "pending", "slicing", "sliced", "uploading", "uploaded", "printing"]


def _job(status: str, *parts: Part) -> PrintJob:
    """Create a print job with the given status that contains ``parts``."""
    job = PrintJob.objects.create(name=f"job-{status}", status=status)
    for part in parts:
        PrintJobPart.objects.create(print_job=job, part=part, quantity=1)
    return job


class PartBlockingJobsTests(TestCase):
    """``Part.blocking_jobs`` uses the same "unfinished" definition as the project guard."""

    def setUp(self) -> None:
        self.project = Project.objects.create(name="R")
        self.part = Part.objects.create(name="p", project=self.project)
        self.other = Part.objects.create(name="other", project=self.project)

    def test_unfinished_statuses_block(self) -> None:
        for status in UNFINISHED:
            with self.subTest(status=status):
                job = _job(status, self.part)
                self.assertEqual(list(self.part.blocking_jobs()), [job])
                job.delete()

    def test_terminal_statuses_and_other_parts_do_not_block(self) -> None:
        for status in PrintJob.TERMINAL_STATUSES:
            _job(status, self.part)
        _job(PrintJob.STATUS_PRINTING, self.other)
        self.assertFalse(self.part.blocking_jobs().exists())

    def test_statuses_match_project_guard(self) -> None:
        self.assertEqual(set(UNFINISHED) | set(PrintJob.TERMINAL_STATUSES), {s for s, _ in PrintJob.STATUS_CHOICES})


class PartDeleteViewGuardTests(TestCase):
    """The web part delete refuses while an unfinished job contains the part."""

    def setUp(self) -> None:
        self.user = User.objects.create_user(username="deleter", password="testpass123")
        self.user.groups.add(Group.objects.get(name="Designer"))
        self.client.login(username="deleter", password="testpass123")
        self.project = Project.objects.create(name="R")
        self.part = Part.objects.create(name="p", project=self.project)
        self.url = reverse("core:part_delete", args=[self.part.pk])

    def test_confirm_page_lists_blocking_jobs_and_hides_submit(self) -> None:
        job = _job(PrintJob.STATUS_SLICED, self.part)
        resp = self.client.get(self.url)
        self.assertEqual(resp.context["blocking_jobs"], [job])
        self.assertContains(resp, reverse("core:printjob_detail", args=[job.pk]))
        self.assertNotContains(resp, "btn btn-danger")

    def test_post_with_unfinished_job_is_refused(self) -> None:
        for status in UNFINISHED:
            with self.subTest(status=status):
                job = _job(status, self.part)
                resp = self.client.post(self.url, follow=True)
                self.assertRedirects(resp, self.url)
                self.assertContains(resp, "cannot be deleted")
                self.assertTrue(Part.objects.filter(pk=self.part.pk).exists())
                job.delete()

    def test_post_rechecks_after_get(self) -> None:
        resp = self.client.get(self.url)
        self.assertEqual(resp.context["blocking_jobs"], [])
        _job(PrintJob.STATUS_DRAFT, self.part)
        self.client.post(self.url)
        self.assertTrue(Part.objects.filter(pk=self.part.pk).exists())

    def test_finished_job_is_kept_and_part_deleted(self) -> None:
        job = _job(PrintJob.STATUS_COMPLETED, self.part)
        resp = self.client.post(self.url)
        self.assertRedirects(resp, reverse("core:project_detail", args=[self.project.pk]))
        self.assertFalse(Part.objects.filter(pk=self.part.pk).exists())
        self.assertTrue(PrintJob.objects.filter(pk=job.pk).exists())
        self.assertFalse(job.job_parts.exists())

    def test_confirm_page_warns_about_print_history(self) -> None:
        _job(PrintJob.STATUS_COMPLETED, self.part)
        resp = self.client.get(self.url)
        self.assertEqual(resp.context["print_history"], 1)
        self.assertContains(resp, "print history")


class PartApiDeleteGuardTests(APITestCase):
    """``DELETE /api/v1/parts/{id}/`` answers 409 with the blocking job ids."""

    def setUp(self) -> None:
        user = User.objects.create_user(username="api", password="x")
        user.groups.add(Group.objects.get(name="Designer"))
        token = Token.objects.create(user=user)
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {token.key}")
        self.project = Project.objects.create(name="R")
        self.part = Part.objects.create(name="p", project=self.project)

    def test_delete_with_unfinished_job_409(self) -> None:
        job = _job(PrintJob.STATUS_PRINTING, self.part)
        resp = self.client.delete(f"/api/v1/parts/{self.part.pk}/")
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.data["blocking_jobs"], [job.pk])
        self.assertTrue(Part.objects.filter(pk=self.part.pk).exists())

    def test_delete_with_finished_job_204(self) -> None:
        job = _job(PrintJob.STATUS_FAILED, self.part)
        resp = self.client.delete(f"/api/v1/parts/{self.part.pk}/")
        self.assertEqual(resp.status_code, 204)
        self.assertFalse(Part.objects.filter(pk=self.part.pk).exists())
        self.assertTrue(PrintJob.objects.filter(pk=job.pk).exists())


class AdminDeleteGuardTests(TestCase):
    """Django admin refuses to delete projects/parts that unfinished jobs still use."""

    def setUp(self) -> None:
        self.admin = User.objects.create_superuser(username="root", password="pw", email="r@example.com")
        self.client.login(username="root", password="pw")
        self.root = Project.objects.create(name="R")
        self.sub = Project.objects.create(name="S", parent=self.root)
        self.part = Part.objects.create(name="p", project=self.sub)
        self.free = Project.objects.create(name="Free")
        self.free_part = Part.objects.create(name="free part", project=self.free)
        self.job = _job(PrintJob.STATUS_PRINTING, self.part)

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

    def test_unblocked_deletes_still_work(self) -> None:
        self.client.post(reverse("admin:core_project_delete", args=[self.free.pk]), {"post": "yes"})
        self.assertFalse(Project.objects.filter(pk=self.free.pk).exists())
        self.job.status = PrintJob.STATUS_COMPLETED
        self.job.save()
        self._bulk_delete("part", self.part.pk)
        self.assertFalse(Part.objects.filter(pk=self.part.pk).exists())

    def test_race_between_confirm_and_delete_is_refused(self) -> None:
        """A job that appears after the confirmation check still blocks the delete."""
        from core.admin import PartAdmin

        with mock.patch.object(PartAdmin, "_blocking_messages", return_value=[]):
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
