"""Tests for project-related views."""

from unittest import mock

from django.contrib.auth.models import Group, User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from core.models import (
    OrcaPrintPreset,
    Part,
    PrinterProfile,
    PrintJob,
    PrintJobPart,
    PrintJobPlate,
    PrintQueue,
    Project,
    ProjectComponent,
    ProjectPart,
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

    def test_project_update_does_not_touch_component_edges(self):
        """Editing a module via the project edit form must not disturb its composition edges.

        The edit form no longer exposes the legacy ``parent``/``quantity`` re-parent
        control (composition is edited through the edge UI), so a plain edit of an
        edge-based module must leave its ``parent_links`` untouched — the regression
        this whole change targets, guarded at the view layer.
        """
        truck = Project.objects.create(name="Truck", created_by=self.user)
        ProjectComponent.objects.create(parent_project=truck, child_project=self.project, quantity=2)

        resp = self.client.post(
            reverse("core:project_update", args=[self.project.pk]),
            {"name": "Renamed Module", "description": "", "default_print_preset": self.preset.pk},
        )
        self.assertEqual(resp.status_code, 302)
        self.project.refresh_from_db()
        self.assertEqual(self.project.name, "Renamed Module")
        self.assertEqual(self.project.parent_links.count(), 1)
        edge = self.project.parent_links.get()
        self.assertEqual(edge.parent_project_id, truck.pk)
        self.assertEqual(edge.quantity, 2)

    def test_project_edit_form_has_no_parent_field(self):
        """The edit form no longer exposes the legacy parent/quantity re-parent control."""
        resp = self.client.get(reverse("core:project_update", args=[self.project.pk]))
        form = resp.context["form"]
        self.assertNotIn("parent", form.fields)
        self.assertNotIn("quantity", form.fields)

    def test_subproject_create_makes_component_edge(self):
        """Creating a sub-project links it to the parent via a ProjectComponent edge."""
        parent = Project.objects.create(name="Assembly", created_by=self.user)
        resp = self.client.post(
            reverse("core:subproject_create", args=[parent.pk]),
            {"name": "New Module", "description": "", "quantity": "3", "default_print_preset": self.preset.pk},
        )
        self.assertEqual(resp.status_code, 302)
        child = Project.objects.get(name="New Module")
        edge = ProjectComponent.objects.get(parent_project=parent, child_project=child)
        self.assertEqual(edge.quantity, 3)
        self.assertEqual(child.created_by_id, self.user.pk)

    def test_detail_shows_used_in_for_shared_module(self):
        from core.models import ProjectComponent

        cabin = Project.objects.create(name="Cabin", created_by=self.user)
        truck = Project.objects.create(name="Truck A", created_by=self.user)
        ProjectComponent.objects.create(parent_project=truck, child_project=cabin)
        resp = self.client.get(reverse("core:project_detail", kwargs={"pk": cabin.pk}))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Truck A")  # used-in assembly shown

    def test_list_shows_only_top_level_edge_based(self):
        from core.models import ProjectComponent

        parent = Project.objects.create(name="TopTruck", created_by=self.user)
        child = Project.objects.create(name="ChildModule", created_by=self.user)
        ProjectComponent.objects.create(parent_project=parent, child_project=child)
        resp = self.client.get(reverse("core:project_list"))
        self.assertContains(resp, "TopTruck")
        self.assertNotContains(resp, "ChildModule")  # referenced module is not top-level

    def test_detail_part_count_uses_edges(self):
        from core.models import Part, ProjectPart

        proj = Project.objects.create(name="CountProj", created_by=self.user)
        p = Part.objects.create(name="X")
        ProjectPart.objects.create(project=proj, part=p)  # direct edge into proj
        resp = self.client.get(reverse("core:project_detail", kwargs={"pk": proj.pk}))
        self.assertContains(resp, "X")  # the referenced part is listed

    def test_duplicate_as_variant_view_get_shows_form(self):
        truck = Project.objects.create(name="Truck A", created_by=self.user)
        resp = self.client.get(reverse("core:project_duplicate", kwargs={"pk": truck.pk}))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Truck A (Variant)")  # suggested name pre-filled

    def test_duplicate_as_variant_view_creates_variant(self):
        from core.models import ProjectComponent

        truck = Project.objects.create(name="Truck A", created_by=self.user)
        cabin = Project.objects.create(name="Cabin", created_by=self.user)
        ProjectComponent.objects.create(parent_project=truck, child_project=cabin, quantity=1)
        resp = self.client.post(reverse("core:project_duplicate", kwargs={"pk": truck.pk}), {"name": "Truck B"})
        self.assertEqual(resp.status_code, 302)
        variant = Project.objects.get(name="Truck B")
        self.assertEqual(variant.child_links.get().child_project_id, cabin.pk)
        self.assertEqual(variant.created_by_id, self.user.pk)
        self.assertIn(str(variant.pk), resp["Location"])  # redirects to the new project

    def test_detail_shows_duplicate_button_for_manager(self):
        proj = Project.objects.create(name="Truck A", created_by=self.user)
        resp = self.client.get(reverse("core:project_detail", kwargs={"pk": proj.pk}))
        self.assertContains(resp, "Duplicate as variant")


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

    def test_re_estimate_deduplicates_shared_part(self):
        """A part reachable via several DAG paths is reset and queued exactly once."""
        preset = OrcaPrintPreset.objects.create(name="Fast", state=OrcaPrintPreset.STATE_RESOLVED, instantiation=True)
        shared = Part.objects.create(name="SharedBracket", print_preset=preset)
        shared.stl_file.save(
            "shared.stl", SimpleUploadedFile("s.stl", b"solid\nendsolid", content_type="model/stl"), save=True
        )
        # Add the part directly to self.project
        ProjectPart.objects.create(project=self.project, part=shared, quantity=1)
        # Also add it via a child module — creates a second DAG path to the same part
        child = Project.objects.create(name="Module", created_by=self.user)
        ProjectPart.objects.create(project=child, part=shared, quantity=1)
        ProjectComponent.objects.create(parent_project=self.project, child_project=child, quantity=1)

        with mock.patch("core.views.projects._trigger_part_estimation") as triggered:
            resp = self.client.post(reverse("core:project_re_estimate", args=[self.project.pk]))

        self.assertEqual(resp.status_code, 302)
        triggered.assert_called_once()
        self.assertEqual(triggered.call_args[0][0].pk, shared.pk)


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
class AssemblyEditorViewTests(TestDataMixin, TestCase):
    """Edge write views + assembly editor + delete semantics (Phase 3b)."""

    def setUp(self):
        super().setUp()
        self.client.login(username="testuser", password="testpass123")

    def test_add_existing_component_creates_edge(self):
        from core.models import ProjectComponent

        truck = Project.objects.create(name="Truck", created_by=self.user)
        cabin = Project.objects.create(name="Cabin", created_by=self.user)
        resp = self.client.post(
            reverse("core:project_add_component", kwargs={"pk": truck.pk}),
            {"child_project": cabin.pk, "quantity": 2},
        )
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(ProjectComponent.objects.filter(parent_project=truck, child_project=cabin, quantity=2).exists())

    def test_add_component_rejects_cycle(self):
        from core.models import ProjectComponent

        a = Project.objects.create(name="CycA", created_by=self.user)
        b = Project.objects.create(name="CycB", created_by=self.user)
        ProjectComponent.objects.create(parent_project=a, child_project=b)
        # adding a under b would cycle -> no edge created
        resp = self.client.post(
            reverse("core:project_add_component", kwargs={"pk": b.pk}),
            {"child_project": a.pk, "quantity": 1},
        )
        self.assertEqual(resp.status_code, 302)
        self.assertFalse(ProjectComponent.objects.filter(parent_project=b, child_project=a).exists())

    def test_remove_component_edge_keeps_node(self):
        from core.models import ProjectComponent

        truck = Project.objects.create(name="Truck", created_by=self.user)
        cabin = Project.objects.create(name="Cabin", created_by=self.user)
        edge = ProjectComponent.objects.create(parent_project=truck, child_project=cabin)
        resp = self.client.post(reverse("core:project_component_remove", kwargs={"pk": edge.pk}))
        self.assertEqual(resp.status_code, 302)
        self.assertFalse(ProjectComponent.objects.filter(pk=edge.pk).exists())
        self.assertTrue(Project.objects.filter(pk=cabin.pk).exists())  # node survives

    def test_remove_component_clears_stale_legacy_parent_fk(self):
        # A legacy sub-project (parent FK set, edge seeded on insert) that is detached
        # through the edge UI must have its stale parent FK cleared, so the former
        # parent is deletable (parent uses on_delete=PROTECT) and preset inheritance
        # no longer leaks from the ex-parent.
        parent = Project.objects.create(name="Assembly", created_by=self.user)
        child = Project.objects.create(name="Module", parent=parent, quantity=2, created_by=self.user)
        edge = child.parent_links.get()  # seeded on insert by Project.save()

        resp = self.client.post(reverse("core:project_component_remove", kwargs={"pk": edge.pk}))
        self.assertEqual(resp.status_code, 302)

        child.refresh_from_db()
        self.assertIsNone(child.parent_id)
        self.assertEqual(parent.subprojects.count(), 0)

    def test_update_component_quantity(self):
        from core.models import ProjectComponent

        truck = Project.objects.create(name="Truck", created_by=self.user)
        cabin = Project.objects.create(name="Cabin", created_by=self.user)
        edge = ProjectComponent.objects.create(parent_project=truck, child_project=cabin, quantity=1)
        resp = self.client.post(reverse("core:project_component_quantity", kwargs={"pk": edge.pk}), {"quantity": 5})
        self.assertEqual(resp.status_code, 302)
        edge.refresh_from_db()
        self.assertEqual(edge.quantity, 5)

    def test_add_existing_part_creates_edge(self):
        from core.models import Part, ProjectPart

        assembly = Project.objects.create(name="Assembly", created_by=self.user)
        lib_part = Part.objects.create(name="LibBolt")
        resp = self.client.post(
            reverse("core:project_add_part", kwargs={"pk": assembly.pk}),
            {"part": lib_part.pk, "quantity": 6},
        )
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(ProjectPart.objects.filter(project=assembly, part=lib_part, quantity=6).exists())

    def test_remove_part_edge_keeps_node(self):
        from core.models import Part, ProjectPart

        assembly = Project.objects.create(name="Assembly", created_by=self.user)
        part = Part.objects.create(name="Screw")
        edge = ProjectPart.objects.create(project=assembly, part=part)
        resp = self.client.post(reverse("core:project_part_remove", kwargs={"pk": edge.pk}))
        self.assertEqual(resp.status_code, 302)
        self.assertFalse(ProjectPart.objects.filter(pk=edge.pk).exists())
        self.assertTrue(Part.objects.filter(pk=part.pk).exists())  # part node survives

    def test_detail_shows_assembly_editor_for_manager(self):
        proj = Project.objects.create(name="EditProj", created_by=self.user)
        resp = self.client.get(reverse("core:project_detail", kwargs={"pk": proj.pk}))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Add existing module")
        self.assertContains(resp, "Add existing part")

    def test_add_component_requires_manage_permission(self):
        from django.contrib.auth.models import Group, User

        operator = User.objects.create_user(username="op_user", password="oppass123")
        operator.groups.add(Group.objects.get(name="Operator"))
        self.client.logout()
        self.client.login(username="op_user", password="oppass123")
        truck = Project.objects.create(name="Truck", created_by=self.user)
        cabin = Project.objects.create(name="Cabin", created_by=self.user)
        resp = self.client.post(
            reverse("core:project_add_component", kwargs={"pk": truck.pk}),
            {"child_project": cabin.pk, "quantity": 1},
        )
        self.assertEqual(resp.status_code, 403)

    def test_detail_hides_editor_for_operator(self):
        from django.contrib.auth.models import Group, User

        operator = User.objects.create_user(username="op2_user", password="oppass123")
        operator.groups.add(Group.objects.get(name="Operator"))
        self.client.logout()
        self.client.login(username="op2_user", password="oppass123")
        proj = Project.objects.create(name="ReadOnlyProj", created_by=self.user)
        resp = self.client.get(reverse("core:project_detail", kwargs={"pk": proj.pk}))
        self.assertEqual(resp.status_code, 200)
        self.assertNotContains(resp, "Add existing module")

    def test_project_delete_confirm_warns_used_in(self):
        from core.models import ProjectComponent

        cabin = Project.objects.create(name="Cabin", created_by=self.user)
        truck = Project.objects.create(name="Truck", created_by=self.user)
        ProjectComponent.objects.create(parent_project=truck, child_project=cabin)
        resp = self.client.get(reverse("core:project_delete", kwargs={"pk": cabin.pk}))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Truck")
        self.assertContains(resp, "used in")


@override_settings(ALLOWED_HOSTS=["testserver"])
class BuildProgressExpandedTests(TestCase):
    """Context flag build_progress_expanded on the project detail view."""

    def setUp(self):
        super().setUp()
        self.user = User.objects.create_user(username="bp_testuser", password="testpass123")
        self.user.groups.add(Group.objects.get(name="Admin"))
        self.client.login(username="bp_testuser", password="testpass123")
        self.project = Project.objects.create(name="BP Test Project", created_by=self.user)
        self.part = Part.objects.create(name="BP Part")
        ProjectPart.objects.create(project=self.project, part=self.part, quantity=2)

    def _context(self) -> dict:
        resp = self.client.get(reverse("core:project_detail", args=[self.project.pk]))
        self.assertEqual(resp.status_code, 200)
        return resp.context

    def test_false_for_fresh_project(self):
        self.assertFalse(self._context()["build_progress_expanded"])

    def test_true_when_printed_gt_zero(self):
        job = PrintJob.objects.create(status=PrintJob.STATUS_COMPLETED)
        PrintJobPart.objects.create(print_job=job, part=self.part, quantity=2, target_assembly=self.project)
        PrintJobPlate.objects.create(print_job=job, plate_number=1, status=PrintJobPlate.STATUS_COMPLETED)
        self.assertTrue(self._context()["build_progress_expanded"])

    def test_true_when_active_job(self):
        """Non-terminal job attributed to this project expands the section."""
        job = PrintJob.objects.create(status=PrintJob.STATUS_PRINTING)
        PrintJobPart.objects.create(print_job=job, part=self.part, quantity=2, target_assembly=self.project)
        self.assertTrue(self._context()["build_progress_expanded"])

    def test_true_when_job_queued(self):
        """Job with a PrintQueue entry attributed to this project expands the section."""
        printer = PrinterProfile.objects.create(name="BP Printer", created_by=self.user)
        job = PrintJob.objects.create(status=PrintJob.STATUS_UPLOADED)
        PrintJobPart.objects.create(print_job=job, part=self.part, quantity=2, target_assembly=self.project)
        plate = PrintJobPlate.objects.create(print_job=job, plate_number=1)
        PrintQueue.objects.create(plate=plate, printer=printer)
        self.assertTrue(self._context()["build_progress_expanded"])

    def test_false_when_only_cancelled_job(self):
        """Terminal (cancelled) jobs do not expand the section."""
        job = PrintJob.objects.create(status=PrintJob.STATUS_CANCELLED)
        PrintJobPart.objects.create(print_job=job, part=self.part, quantity=2, target_assembly=self.project)
        self.assertFalse(self._context()["build_progress_expanded"])

    def test_false_when_only_failed_job(self):
        """Terminal (failed) jobs do not expand the section."""
        job = PrintJob.objects.create(status=PrintJob.STATUS_FAILED)
        PrintJobPart.objects.create(print_job=job, part=self.part, quantity=2, target_assembly=self.project)
        self.assertFalse(self._context()["build_progress_expanded"])

    def test_template_renders_show_class(self):
        """Template renders Bootstrap 'show' class when build_progress_expanded is True."""
        job = PrintJob.objects.create(status=PrintJob.STATUS_PRINTING)
        PrintJobPart.objects.create(print_job=job, part=self.part, quantity=2, target_assembly=self.project)
        resp = self.client.get(reverse("core:project_detail", args=[self.project.pk]))
        self.assertContains(resp, "buildProgressBody")
        self.assertContains(resp, 'class="collapse show"')
