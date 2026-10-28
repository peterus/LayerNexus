"""Tests for deleting a project subtree and the unfinished-job guard."""

from django.contrib.auth.models import Group, User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse

from core.models import (
    HardwarePart,
    Part,
    PrintJob,
    PrintJobPart,
    PrintJobPlate,
    Project,
    ProjectDocument,
    ProjectHardware,
)
from core.tests.mixins import _RBACTestBase

UNFINISHED = ["draft", "pending", "slicing", "sliced", "uploading", "uploaded", "printing"]


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

    def _job(self, status: str, *parts: Part) -> PrintJob:
        job = PrintJob.objects.create(name=f"job-{status}", status=status)
        for part in parts:
            PrintJobPart.objects.create(print_job=job, part=part, quantity=1)
        return job

    def test_confirm_page_shows_counts(self) -> None:
        resp = self.client.get(self._url())
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(
            resp.context["summary"],
            {"subprojects": 3, "parts": 3, "documents": 1, "hardware": 1, "print_history": 0},
        )
        self.assertContains(resp, "3 sub-projects")
        self.assertNotContains(resp, "print history")

    def test_confirm_page_warns_about_print_history(self) -> None:
        self._job(PrintJob.STATUS_COMPLETED, self.part_deep)
        resp = self.client.get(self._url())
        self.assertEqual(resp.context["summary"]["print_history"], 1)
        self.assertContains(resp, "print history")

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

    def test_terminal_statuses_do_not_block(self) -> None:
        for status in PrintJob.TERMINAL_STATUSES:
            with self.subTest(status=status):
                root = Project.objects.create(name=f"R-{status}", created_by=self.user)
                part = Part.objects.create(name=f"p-{status}", project=root)
                job = self._job(status, part)
                PrintJobPlate.objects.create(print_job=job, plate_number=1, status=PrintJobPlate.STATUS_COMPLETED)
                resp = self.client.post(self._url(root))
                self.assertRedirects(resp, reverse("core:project_list"))
                self.assertFalse(Project.objects.filter(pk=root.pk).exists())
                self.assertTrue(PrintJob.objects.filter(pk=job.pk).exists())
                self.assertTrue(job.plates.exists())
                self.assertFalse(job.job_parts.exists())

    def test_mixed_job_with_outside_part_still_blocks(self) -> None:
        self._job(PrintJob.STATUS_SLICED, self.sibling_part, self.part_sub)
        self.client.post(self._url())
        self.assertTrue(Project.objects.filter(pk=self.root.pk).exists())

    def test_post_rechecks_block(self) -> None:
        resp = self.client.get(self._url())
        self.assertEqual(resp.context["blocking_jobs"], [])
        self._job(PrintJob.STATUS_DRAFT, self.part_root)
        self.client.post(self._url())
        self.assertTrue(Project.objects.filter(pk=self.root.pk).exists())

    def test_confirm_page_lists_blocking_job_links_and_hides_submit(self) -> None:
        job = self._job(PrintJob.STATUS_PRINTING, self.part_deep)
        resp = self.client.get(self._url())
        self.assertEqual(resp.context["blocking_jobs"], [job])
        self.assertContains(resp, reverse("core:printjob_detail", args=[job.pk]))
        self.assertNotContains(resp, "btn btn-danger")

    def test_job_outside_subtree_does_not_block(self) -> None:
        self._job(PrintJob.STATUS_PRINTING, self.sibling_part)
        resp = self.client.get(self._url())
        self.assertEqual(resp.context["blocking_jobs"], [])
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
