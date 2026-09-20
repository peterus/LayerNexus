"""Tests for project-related views."""

from django.test import Client, TestCase, override_settings
from django.urls import reverse

from core.models import PrinterProfile, Project
from core.tests.mixins import TestDataMixin


@override_settings(ALLOWED_HOSTS=["testserver"])
class ProjectViewTests(TestDataMixin, TestCase):
    """Tests for Project CRUD views."""

    def setUp(self):
        super().setUp()
        self.client.login(username="testuser", password="testpass123")

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
            {"name": "Updated Name", "description": "", "quantity": "1"},
        )
        self.assertEqual(resp.status_code, 302)
        self.project.refresh_from_db()
        self.assertEqual(self.project.name, "Updated Name")

    def test_project_update_other_user_404(self):
        resp = self.client.post(
            reverse("core:project_update", args=[self.other_project.pk]),
            {"name": "Hacked", "description": "", "quantity": "1"},
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

    def test_project_reparent_via_update(self):
        """An existing project can be turned into a sub-project via the edit form."""
        parent = Project.objects.create(name="New Parent", created_by=self.user)
        resp = self.client.post(
            reverse("core:project_update", args=[self.project.pk]),
            {
                "name": self.project.name,
                "description": "",
                "parent": parent.pk,
                "quantity": "2",
            },
        )
        self.assertEqual(resp.status_code, 302)
        self.project.refresh_from_db()
        self.assertEqual(self.project.parent, parent)
        self.assertEqual(self.project.quantity, 2)

    def test_project_remove_parent_via_update(self):
        """A sub-project can be turned back into a top-level project."""
        parent = Project.objects.create(name="Parent", created_by=self.user)
        self.project.parent = parent
        self.project.quantity = 3
        self.project.save()
        resp = self.client.post(
            reverse("core:project_update", args=[self.project.pk]),
            {
                "name": self.project.name,
                "description": "",
                "quantity": "1",
            },
        )
        self.assertEqual(resp.status_code, 302)
        self.project.refresh_from_db()
        self.assertIsNone(self.project.parent)
        self.assertEqual(self.project.quantity, 1)

    def test_project_update_excludes_self_and_descendants_from_parent(self):
        """The parent dropdown should not include the project itself or its descendants."""
        child = Project.objects.create(name="Child", parent=self.project, created_by=self.user)
        resp = self.client.get(reverse("core:project_update", args=[self.project.pk]))
        form = resp.context["form"]
        parent_qs = form.fields["parent"].queryset
        self.assertNotIn(self.project, parent_qs)
        self.assertNotIn(child, parent_qs)

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
        home = Project.objects.create(name="home", created_by=self.user)
        p = Part.objects.create(project=home, name="X", quantity=1)
        ProjectPart.objects.create(project=proj, part=p)  # direct edge into proj
        resp = self.client.get(reverse("core:project_detail", kwargs={"pk": proj.pk}))
        self.assertContains(resp, "X")  # the referenced part is listed


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
        lib_part = Part.objects.create(project=self.other_project, name="LibBolt", quantity=1)
        resp = self.client.post(
            reverse("core:project_add_part", kwargs={"pk": assembly.pk}),
            {"part": lib_part.pk, "quantity": 6},
        )
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(ProjectPart.objects.filter(project=assembly, part=lib_part, quantity=6).exists())

    def test_remove_part_edge_keeps_node(self):
        from core.models import Part, ProjectPart

        assembly = Project.objects.create(name="Assembly", created_by=self.user)
        part = Part.objects.create(project=self.other_project, name="Screw", quantity=1)
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
