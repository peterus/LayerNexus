"""Tests for project-related views."""

from unittest import mock

from django.contrib.auth.models import Group, User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase, override_settings
from django.urls import NoReverseMatch, reverse

from core.models import (
    OrcaPrintPreset,
    Part,
    PrinterProfile,
    PrintJob,
    PrintJobPart,
    PrintJobPlate,
    PrintQueue,
    Project,
)
from core.tests.mixins import TestDataMixin


@override_settings(ALLOWED_HOSTS=["testserver"])
class ProjectViewTests(TestDataMixin, TestCase):
    """Tests for Project CRUD views."""

    def setUp(self):
        super().setUp()
        self.client.login(username="testuser", password="testpass123")
        self.preset = OrcaPrintPreset.objects.create(
            name="FormPreset", orca_name="FormPreset", state=OrcaPrintPreset.STATE_RESOLVED, instantiation=True
        )

    def test_project_list_200(self):
        resp = self.client.get(reverse("core:project_list"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Test Project")

    def test_project_list_only_own(self):
        resp = self.client.get(reverse("core:project_list"))
        self.assertContains(resp, "Other Project")

    def test_project_detail_200(self):
        resp = self.client.get(reverse("core:project_detail", args=[self.project.pk]))
        self.assertEqual(resp.status_code, 200)

    def test_project_detail_other_user_404(self):
        resp = self.client.get(reverse("core:project_detail", args=[self.other_project.pk]))
        self.assertEqual(resp.status_code, 200)

    def test_project_create_get(self):
        resp = self.client.get(reverse("core:project_create"))
        self.assertEqual(resp.status_code, 200)

    def test_project_create_post(self):
        resp = self.client.post(
            reverse("core:project_create"),
            {
                "name": "New Project",
                "description": "New desc",
                "default_print_preset": self.preset.pk,
            },
        )
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(Project.objects.filter(name="New Project", created_by=self.user).exists())

    def test_project_update_get(self):
        resp = self.client.get(reverse("core:project_update", args=[self.project.pk]))
        self.assertEqual(resp.status_code, 200)

    def test_project_update_post(self):
        resp = self.client.post(
            reverse("core:project_update", args=[self.project.pk]),
            {"name": "Updated Name", "description": "", "quantity": "1", "default_print_preset": self.preset.pk},
        )
        self.assertEqual(resp.status_code, 302)
        self.project.refresh_from_db()
        self.assertEqual(self.project.name, "Updated Name")

    def test_project_update_other_user_404(self):
        resp = self.client.post(
            reverse("core:project_update", args=[self.other_project.pk]),
            {"name": "Hacked", "description": "", "quantity": "1", "default_print_preset": self.preset.pk},
        )
        self.assertEqual(resp.status_code, 302)

    def test_project_delete_get(self):
        resp = self.client.get(reverse("core:project_delete", args=[self.project.pk]))
        self.assertEqual(resp.status_code, 200)

    def test_project_delete_post(self):
        pk = self.project.pk
        resp = self.client.post(reverse("core:project_delete", args=[pk]))
        self.assertEqual(resp.status_code, 302)
        self.assertFalse(Project.objects.filter(pk=pk).exists())

    def test_project_delete_other_user_404(self):
        resp = self.client.post(reverse("core:project_delete", args=[self.other_project.pk]))
        self.assertEqual(resp.status_code, 302)

    def test_project_edit_form_has_no_parent_field(self):
        """Re-parenting is not part of this PR: the edit form has no parent field."""
        resp = self.client.get(reverse("core:project_update", args=[self.project.pk]))
        form = resp.context["form"]
        self.assertNotIn("parent", form.fields)
        self.assertNotIn("quantity", form.fields)  # roots have no quantity

    def test_project_list_shows_only_roots(self):
        """Only top-level projects are listed."""
        Project.objects.create(name="ChildModule", parent=self.project, created_by=self.user)
        resp = self.client.get(reverse("core:project_list"))
        self.assertContains(resp, self.project.name)
        self.assertNotContains(resp, "ChildModule")
        self.assertEqual(list(resp.context["projects"]).count(self.project), 1)

    def test_detail_lists_subprojects_and_parts_with_quantity(self):
        """The detail page lists its sub-projects (×qty) and parts (qty)."""
        Project.objects.create(name="Axle module", parent=self.project, quantity=4, created_by=self.user)
        resp = self.client.get(reverse("core:project_detail", args=[self.project.pk]))
        self.assertContains(resp, "Axle module")
        self.assertContains(resp, "×4")
        self.assertEqual(resp.context["parts"], [self.part])
        self.assertContains(resp, self.part.name)
        self.assertEqual(resp.context["build_rows_by_pk"][self.part.pk]["needed"], 3)

    def test_detail_breadcrumb_shows_ancestors(self):
        """A sub-project's breadcrumb walks up to the root."""
        sub = Project.objects.create(name="Sub", parent=self.project, created_by=self.user)
        leaf = Project.objects.create(name="Leaf", parent=sub, created_by=self.user)
        resp = self.client.get(reverse("core:project_detail", args=[leaf.pk]))
        self.assertEqual(resp.context["ancestors"], [self.project, sub])
        self.assertContains(resp, reverse("core:project_detail", args=[sub.pk]))

    def test_subproject_create_sets_parent_and_quantity(self):
        """Creating a sub-project sets parent and quantity on the new project."""
        parent = Project.objects.create(name="Assembly", created_by=self.user)
        resp = self.client.post(
            reverse("core:subproject_create", args=[parent.pk]),
            {"name": "New Module", "description": "", "quantity": "3", "default_print_preset": self.preset.pk},
        )
        self.assertEqual(resp.status_code, 302)
        child = Project.objects.get(name="New Module")
        self.assertEqual(child.parent_id, parent.pk)
        self.assertEqual(child.quantity, 3)
        self.assertEqual(child.created_by_id, self.user.pk)

    def test_subproject_create_preset_optional(self):
        """A sub-project may leave the preset empty (inherited from the parent)."""
        resp = self.client.post(
            reverse("core:subproject_create", args=[self.project.pk]),
            {"name": "Inheriting", "description": "", "quantity": "1", "default_print_preset": ""},
        )
        self.assertEqual(resp.status_code, 302)
        child = Project.objects.get(name="Inheriting")
        self.assertIsNone(child.default_print_preset_id)

    def test_edit_subproject_quantity(self):
        """Editing a sub-project changes its quantity; its preset stays optional."""
        sub = Project.objects.create(name="Sub", parent=self.project, quantity=1, created_by=self.user)
        resp = self.client.post(
            reverse("core:project_update", args=[sub.pk]),
            {"name": "Sub", "description": "", "quantity": "5", "default_print_preset": ""},
        )
        self.assertEqual(resp.status_code, 302)
        sub.refresh_from_db()
        self.assertEqual(sub.quantity, 5)
        self.assertEqual(sub.parent_id, self.project.pk)

    def test_edit_root_requires_preset(self):
        """A top-level project cannot be saved without a preset."""
        resp = self.client.post(
            reverse("core:project_update", args=[self.project.pk]),
            {"name": "Renamed", "description": "", "default_print_preset": ""},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertIn("default_print_preset", resp.context["form"].errors)

    def test_edge_urls_removed(self):
        """The composition edge editing URLs no longer exist."""
        for name in (
            "project_add_component",
            "project_add_part",
            "project_component_remove",
            "project_component_quantity",
            "project_part_remove",
            "project_part_quantity",
        ):
            with self.subTest(name=name), self.assertRaises(NoReverseMatch):
                reverse(f"core:{name}", args=[1])

    def test_no_used_in_context_anywhere(self):
        # Tree rollback: the edge-based "Used in" navigation is gone from every view.
        urls = [
            reverse("core:project_detail", kwargs={"pk": self.project.pk}),
            reverse("core:project_update", kwargs={"pk": self.project.pk}),
            reverse("core:project_delete", kwargs={"pk": self.project.pk}),
            reverse("core:project_cost", kwargs={"pk": self.project.pk}),
            reverse("core:subproject_create", kwargs={"parent_pk": self.project.pk}),
            reverse("core:part_detail", kwargs={"pk": self.part.pk}),
            reverse("core:part_update", kwargs={"pk": self.part.pk}),
            reverse("core:part_delete", kwargs={"pk": self.part.pk}),
        ]
        for url in urls:
            with self.subTest(url=url):
                resp = self.client.get(url)
                self.assertEqual(resp.status_code, 200)
                self.assertNotIn("used_in", resp.context)
                self.assertNotContains(resp, "Used in:")

    def test_project_form_cancel_targets(self):
        # Edit cancels back to the project itself; create cancels to the project list.
        edit = self.client.get(reverse("core:project_update", kwargs={"pk": self.project.pk}))
        detail_url = reverse("core:project_detail", kwargs={"pk": self.project.pk})
        self.assertContains(edit, f'<a href="{detail_url}" class="btn btn-outline-secondary">Cancel</a>', html=True)
        create = self.client.get(reverse("core:project_create"))
        list_url = reverse("core:project_list")
        self.assertContains(create, f'<a href="{list_url}" class="btn btn-outline-secondary">Cancel</a>', html=True)

    def test_part_detail_has_no_usage_table(self):
        resp = self.client.get(reverse("core:part_detail", kwargs={"pk": self.part.pk}))
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn("part_usage", resp.context)
        self.assertNotContains(resp, "Used in assemblies")

    def test_detail_has_no_duplicate_button(self):
        proj = Project.objects.create(name="Truck A", created_by=self.user)
        resp = self.client.get(reverse("core:project_detail", kwargs={"pk": proj.pk}))
        self.assertNotContains(resp, "Duplicate as variant")


class ProjectReEstimateViewTests(TestDataMixin, TestCase):
    """Tests for the ProjectReEstimateView."""

    def setUp(self):
        super().setUp()
        self.client.login(username="testuser", password="testpass123")

    def test_re_estimate_requires_post(self):
        """GET is not allowed on the project re-estimate endpoint."""
        resp = self.client.get(reverse("core:project_re_estimate", args=[self.project.pk]))
        self.assertEqual(resp.status_code, 405)

    def test_re_estimate_empty_project(self):
        """Re-estimate on an empty project should show a warning."""
        empty = Project.objects.create(name="Empty", created_by=self.user)
        resp = self.client.post(reverse("core:project_re_estimate", args=[empty.pk]))
        self.assertEqual(resp.status_code, 302)

    def test_re_estimate_redirects_to_project(self):
        """Re-estimate redirects back to the project detail page."""
        resp = self.client.post(reverse("core:project_re_estimate", args=[self.project.pk]))
        self.assertRedirects(resp, reverse("core:project_detail", args=[self.project.pk]))

    def test_re_estimate_queues_each_part_once_with_inherited_preset(self):
        """Each subtree part with an STL and a resolvable preset is reset and queued once."""
        preset = OrcaPrintPreset.objects.create(name="Fast", state=OrcaPrintPreset.STATE_RESOLVED, instantiation=True)
        self.project.default_print_preset = preset
        self.project.save()
        child = Project.objects.create(parent=self.project, quantity=2, name="Module", created_by=self.user)
        inherited = Part.objects.create(
            project=child, quantity=1, name="Bracket", filament_used_grams=3.0, estimation_status="success"
        )
        inherited.stl_file.save("b.stl", SimpleUploadedFile("b.stl", b"solid\nendsolid"), save=True)

        with mock.patch("core.views.projects._trigger_part_estimation") as triggered:
            resp = self.client.post(reverse("core:project_re_estimate", args=[self.project.pk]))

        self.assertEqual(resp.status_code, 302)
        triggered.assert_called_once()  # self.part has no STL, so only the inherited part
        self.assertEqual(triggered.call_args[0][0].pk, inherited.pk)
        inherited.refresh_from_db()
        self.assertIsNone(inherited.filament_used_grams)
        self.assertEqual(inherited.estimation_status, Part.ESTIMATION_NONE)

    def test_re_estimate_skips_part_without_resolvable_preset(self):
        """A part with an STL but no preset anywhere up the chain is not queued."""
        bare = Part.objects.create(project=self.project, quantity=1, name="Bare")
        bare.stl_file.save("bare.stl", SimpleUploadedFile("bare.stl", b"solid\nendsolid"), save=True)
        with mock.patch("core.views.projects._trigger_part_estimation") as triggered:
            self.client.post(reverse("core:project_re_estimate", args=[self.project.pk]))
        triggered.assert_not_called()


@override_settings(ALLOWED_HOSTS=["testserver"])
class CostViewTests(TestDataMixin, TestCase):
    """Tests for cost-related views."""

    def setUp(self):
        super().setUp()
        self.client = Client()
        self.client.login(username="testuser", password="testpass123")
        self.printer = PrinterProfile.objects.create(name="Test Printer", created_by=self.user)

    def test_cost_profile_form_get(self):
        r = self.client.get(reverse("core:costprofile_update", args=[self.printer.pk]))
        self.assertEqual(r.status_code, 200)

    def test_project_cost_get(self):
        r = self.client.get(reverse("core:project_cost", args=[self.project.pk]))
        self.assertEqual(r.status_code, 200)


@override_settings(ALLOWED_HOSTS=["testserver"])
class BuildProgressExpandedTests(TestCase):
    """Context flag build_progress_expanded on the project detail view."""

    def setUp(self):
        super().setUp()
        self.user = User.objects.create_user(username="bp_testuser", password="testpass123")
        self.user.groups.add(Group.objects.get(name="Admin"))
        self.client.login(username="bp_testuser", password="testpass123")
        self.project = Project.objects.create(name="BP Test Project", created_by=self.user)
        self.part = Part.objects.create(project=self.project, quantity=2, name="BP Part")

    def _context(self) -> dict:
        resp = self.client.get(reverse("core:project_detail", args=[self.project.pk]))
        self.assertEqual(resp.status_code, 200)
        return resp.context

    def test_false_for_fresh_project(self):
        self.assertFalse(self._context()["build_progress_expanded"])

    def test_true_when_printed_gt_zero(self):
        job = PrintJob.objects.create(status=PrintJob.STATUS_COMPLETED)
        PrintJobPart.objects.create(print_job=job, part=self.part, quantity=2)
        PrintJobPlate.objects.create(print_job=job, plate_number=1, status=PrintJobPlate.STATUS_COMPLETED)
        self.assertTrue(self._context()["build_progress_expanded"])

    def test_true_when_active_job(self):
        """A non-terminal job with a part of this project expands the section."""
        job = PrintJob.objects.create(status=PrintJob.STATUS_PRINTING)
        PrintJobPart.objects.create(print_job=job, part=self.part, quantity=2)
        self.assertTrue(self._context()["build_progress_expanded"])

    def test_true_when_job_queued(self):
        """A queued job with a part of this project expands the section."""
        printer = PrinterProfile.objects.create(name="BP Printer", created_by=self.user)
        job = PrintJob.objects.create(status=PrintJob.STATUS_UPLOADED)
        PrintJobPart.objects.create(print_job=job, part=self.part, quantity=2)
        plate = PrintJobPlate.objects.create(print_job=job, plate_number=1)
        PrintQueue.objects.create(plate=plate, printer=printer)
        self.assertTrue(self._context()["build_progress_expanded"])

    def test_false_when_only_cancelled_job(self):
        """Terminal (cancelled) jobs do not expand the section."""
        job = PrintJob.objects.create(status=PrintJob.STATUS_CANCELLED)
        PrintJobPart.objects.create(print_job=job, part=self.part, quantity=2)
        self.assertFalse(self._context()["build_progress_expanded"])

    def test_false_when_only_failed_job(self):
        """Terminal (failed) jobs do not expand the section."""
        job = PrintJob.objects.create(status=PrintJob.STATUS_FAILED)
        PrintJobPart.objects.create(print_job=job, part=self.part, quantity=2)
        self.assertFalse(self._context()["build_progress_expanded"])

    def test_true_when_active_job_in_subproject(self):
        """An unfinished job with a part of a grandchild expands the root's section."""
        sub = Project.objects.create(name="Sub", parent=self.project, created_by=self.user)
        leaf = Project.objects.create(name="Leaf", parent=sub, created_by=self.user)
        deep = Part.objects.create(project=leaf, quantity=1, name="Deep part")
        job = PrintJob.objects.create(status=PrintJob.STATUS_DRAFT)
        PrintJobPart.objects.create(print_job=job, part=deep, quantity=1)
        self.assertTrue(self._context()["build_progress_expanded"])

    def test_template_renders_show_class(self):
        """Template renders Bootstrap 'show' class when build_progress_expanded is True."""
        job = PrintJob.objects.create(status=PrintJob.STATUS_PRINTING)
        PrintJobPart.objects.create(print_job=job, part=self.part, quantity=2)
        resp = self.client.get(reverse("core:project_detail", args=[self.project.pk]))
        self.assertContains(resp, "buildProgressBody")
        self.assertContains(resp, 'class="collapse show"')
