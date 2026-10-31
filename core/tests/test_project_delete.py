"""Tests for deleting a project subtree and the print-job guards (unfinished jobs, print history)."""

from django.contrib.auth.models import Group, User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse

from core.models import (
    HardwarePart,
    Part,
    PrintJob,
    Project,
    ProjectDocument,
    ProjectHardware,
)
from core.tests.mixins import _RBACTestBase
from core.tests.test_delete_guards import UNFINISHED, job_in_display_status


class ProjectSubtreeDeleteTests(TestCase):
    """Root R → subs S1 (→ grandchild G) and S2, with parts, a document and hardware."""

    def setUp(self) -> None:
        self.user = User.objects.create_user(username="deleter", password="testpass123")
        self.user.groups.add(Group.objects.get(name="Admin"))
        self.client.login(username="deleter", password="testpass123")
        self.root = Project.objects.create(name="R", created_by=self.user)
        self.sub1 = Project.objects.create(name="S1", parent=self.root, created_by=self.user)
        self.sub2 = Project.objects.create(name="S2", parent=self.root, created_by=self.user)
        self.grandchild = Project.objects.create(name="G", parent=self.sub1, created_by=self.user)
        self.part_root = Part.objects.create(name="root part", project=self.root)
        self.part_sub = Part.objects.create(name="sub part", project=self.sub2)
        self.part_deep = Part.objects.create(name="deep part", project=self.grandchild)
        self.doc = ProjectDocument.objects.create(
            project=self.sub1, name="Manual", file=SimpleUploadedFile("manual.pdf", b"%PDF")
        )
        hardware = HardwarePart.objects.create(name="M3 bolt", category="bolts")
        self.hw = ProjectHardware.objects.create(project=self.grandchild, hardware_part=hardware, quantity=4)
        self.sibling = Project.objects.create(name="Sibling root", created_by=self.user)
        self.sibling_part = Part.objects.create(name="sibling part", project=self.sibling)

    def _url(self, project: Project | None = None) -> str:
        return reverse("core:project_delete", args=[(project or self.root).pk])

    def _job(self, display_status: str, *parts: Part) -> PrintJob:
        return job_in_display_status(display_status, *parts)

    def test_confirm_page_shows_counts(self) -> None:
        resp = self.client.get(self._url())
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(
            resp.context["summary"],
            {"subprojects": 3, "parts": 3, "documents": 1, "hardware": 1, "print_history": 0},
        )
        self.assertContains(resp, "3 sub-projects")
        self.assertNotContains(resp, "print history")

    def test_confirm_page_lists_parts_with_print_history_and_hides_submit(self) -> None:
        """Print history now blocks the delete (was: a warning that the history goes too)."""
        self._job(PrintJob.DISPLAY_COMPLETED, self.part_deep, self.part_sub)
        self._job(PrintJob.DISPLAY_PARTIALLY_FAILED, self.part_deep)
        self._job(PrintJob.DISPLAY_COMPLETED, self.sibling_part)
        resp = self.client.get(self._url())
        self.assertEqual(resp.context["summary"]["print_history"], 2)
        self.assertEqual(resp.context["history_parts"], [self.part_deep, self.part_sub])
        self.assertEqual(resp.context["blocking_jobs"], [])
        self.assertContains(resp, "print history")
        self.assertContains(resp, reverse("core:part_detail", args=[self.part_deep.pk]))
        self.assertNotContains(resp, "btn btn-danger")

    def test_delete_cascades_subtree(self) -> None:
        resp = self.client.post(self._url())
        self.assertRedirects(resp, reverse("core:project_list"))
        subtree = [self.root.pk, self.sub1.pk, self.sub2.pk, self.grandchild.pk]
        self.assertFalse(Project.objects.filter(pk__in=subtree).exists())
        self.assertFalse(Part.objects.filter(pk__in=[self.part_root.pk, self.part_sub.pk, self.part_deep.pk]).exists())
        self.assertFalse(ProjectDocument.objects.filter(pk=self.doc.pk).exists())
        self.assertFalse(ProjectHardware.objects.filter(pk=self.hw.pk).exists())
        self.assertTrue(Project.objects.filter(pk=self.sibling.pk).exists())
        self.assertTrue(Part.objects.filter(pk=self.sibling_part.pk).exists())

    def test_each_unfinished_status_blocks_delete(self) -> None:
        for status in UNFINISHED:
            with self.subTest(status=status):
                job = self._job(status, self.part_deep)
                resp = self.client.post(self._url(), follow=True)
                self.assertRedirects(resp, self._url())
                self.assertTrue(Project.objects.filter(pk=self.root.pk).exists())
                self.assertTrue(Part.objects.filter(pk=self.part_deep.pk).exists())
                self.assertContains(resp, "cannot be deleted")
                job.delete()

    def test_finished_jobs_block_delete_as_print_history(self) -> None:
        """Finished jobs are print history and block (was: terminal jobs did not block, history dropped)."""
        for status in PrintJob.FINISHED_DISPLAY_STATUSES:
            with self.subTest(status=status):
                root = Project.objects.create(name=f"R-{status}", created_by=self.user)
                part = Part.objects.create(name=f"p-{status}", project=root)
                job = self._job(status, part)
                resp = self.client.post(self._url(root), follow=True)
                self.assertRedirects(resp, self._url(root))
                self.assertContains(resp, "parts of it have print history")
                self.assertTrue(Project.objects.filter(pk=root.pk).exists())
                self.assertTrue(job.job_parts.filter(part=part).exists())

    def test_mixed_job_with_outside_part_still_blocks(self) -> None:
        self._job(PrintJob.DISPLAY_READY, self.sibling_part, self.part_sub)
        self.client.post(self._url())
        self.assertTrue(Project.objects.filter(pk=self.root.pk).exists())

    def test_post_rechecks_block(self) -> None:
        resp = self.client.get(self._url())
        self.assertEqual(resp.context["blocking_jobs"], [])
        self._job(PrintJob.DISPLAY_DRAFT, self.part_root)
        self.client.post(self._url())
        self.assertTrue(Project.objects.filter(pk=self.root.pk).exists())

    def test_confirm_page_lists_blocking_job_links_and_hides_submit(self) -> None:
        job = self._job(PrintJob.DISPLAY_READY, self.part_deep)
        resp = self.client.get(self._url())
        self.assertEqual(resp.context["blocking_jobs"], [job])
        self.assertEqual(resp.context["history_parts"], [])
        self.assertContains(resp, reverse("core:printjob_detail", args=[job.pk]))
        self.assertNotContains(resp, "btn btn-danger")

    def test_job_outside_subtree_does_not_block(self) -> None:
        self._job(PrintJob.DISPLAY_PRINTING, self.sibling_part)
        self._job(PrintJob.DISPLAY_COMPLETED, self.sibling_part)
        resp = self.client.get(self._url())
        self.assertEqual(resp.context["blocking_jobs"], [])
        self.assertEqual(resp.context["history_parts"], [])
        self.assertContains(resp, "btn btn-danger")

    def test_redirects_to_parent_after_sub_delete(self) -> None:
        resp = self.client.post(self._url(self.sub1))
        self.assertRedirects(resp, reverse("core:project_detail", args=[self.root.pk]))
        self.assertFalse(Project.objects.filter(pk=self.grandchild.pk).exists())
        self.assertTrue(Project.objects.filter(pk=self.sub2.pk).exists())

    def test_redirects_to_list_after_root_delete(self) -> None:
        resp = self.client.post(self._url())
        self.assertRedirects(resp, reverse("core:project_list"))


class ProjectDeletePermissionTests(_RBACTestBase):
    """Only project managers may delete projects."""

    def test_delete_forbidden_for_operator(self) -> None:
        self.client.login(username="operator_user", password="testpass123")
        resp = self.client.post(reverse("core:project_delete", args=[self.project.pk]))
        self.assertEqual(resp.status_code, 403)
        self.assertTrue(Project.objects.filter(pk=self.project.pk).exists())

    def test_delete_allowed_for_designer(self) -> None:
        self.client.login(username="designer_user", password="testpass123")
        resp = self.client.post(reverse("core:project_delete", args=[self.project.pk]))
        self.assertEqual(resp.status_code, 302)
        self.assertFalse(Project.objects.filter(pk=self.project.pk).exists())
