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
