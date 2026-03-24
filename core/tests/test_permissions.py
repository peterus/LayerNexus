"""Tests for role-based access control (RBAC) permissions."""

from django.test import TestCase, override_settings
from django.urls import reverse

from core.models import PrinterProfile, Project
from core.tests.mixins import _RBACTestBase


@override_settings(ALLOWED_HOSTS=["testserver"])
class DesignerPermissionTests(_RBACTestBase):
    """Verify Designer role can manage projects but NOT printers or users."""

    def test_designer_can_create_project(self):
        self.client.login(username="designer_user", password="testpass123")
        resp = self.client.post(
            reverse("core:project_create"),
            {"name": "Designer Project", "description": ""},
        )
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(Project.objects.filter(name="Designer Project").exists())

    def test_designer_cannot_create_printer(self):
        self.client.login(username="designer_user", password="testpass123")
        resp = self.client.post(reverse("core:printerprofile_create"), {"name": "Hacked Printer"})
        self.assertEqual(resp.status_code, 403)

    def test_designer_cannot_access_user_list(self):
        self.client.login(username="designer_user", password="testpass123")
        resp = self.client.get(reverse("core:user_list"))
        self.assertEqual(resp.status_code, 403)

    def test_designer_cannot_delete_orca_profile(self):
        from core.models import OrcaMachineProfile

        profile = OrcaMachineProfile.objects.create(name="Test Machine", orca_name="test_machine")
        self.client.login(username="designer_user", password="testpass123")
        resp = self.client.post(reverse("core:orcamachineprofile_delete", args=[profile.pk]))
        self.assertEqual(resp.status_code, 403)

    def test_designer_can_view_printer_list(self):
        self.client.login(username="designer_user", password="testpass123")
        resp = self.client.get(reverse("core:printerprofile_list"))
        self.assertEqual(resp.status_code, 200)


@override_settings(ALLOWED_HOSTS=["testserver"])
class OperatorPermissionTests(_RBACTestBase):
    """Verify Operator role can manage printers but NOT projects or users."""

    def test_operator_can_create_printer(self):
        self.client.login(username="operator_user", password="testpass123")
        resp = self.client.post(reverse("core:printerprofile_create"), {"name": "Op Printer"})
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(PrinterProfile.objects.filter(name="Op Printer").exists())

    def test_operator_cannot_create_project(self):
        self.client.login(username="operator_user", password="testpass123")
        resp = self.client.post(
            reverse("core:project_create"),
            {"name": "Hacked Project", "description": ""},
        )
        self.assertEqual(resp.status_code, 403)

    def test_operator_cannot_access_user_list(self):
        self.client.login(username="operator_user", password="testpass123")
        resp = self.client.get(reverse("core:user_list"))
        self.assertEqual(resp.status_code, 403)

    def test_operator_can_view_project_list(self):
        self.client.login(username="operator_user", password="testpass123")
        resp = self.client.get(reverse("core:project_list"))
        self.assertEqual(resp.status_code, 200)


@override_settings(ALLOWED_HOSTS=["testserver"])
class AdminPermissionTests(_RBACTestBase):
    """Verify Admin role has full access."""

    def test_admin_can_access_user_list(self):
        self.client.login(username="admin_user", password="testpass123")
        resp = self.client.get(reverse("core:user_list"))
        self.assertEqual(resp.status_code, 200)

    def test_admin_can_create_project(self):
        self.client.login(username="admin_user", password="testpass123")
        resp = self.client.post(
            reverse("core:project_create"),
            {"name": "Admin Project", "description": ""},
        )
        self.assertEqual(resp.status_code, 302)

    def test_admin_can_create_printer(self):
        self.client.login(username="admin_user", password="testpass123")
        resp = self.client.post(reverse("core:printerprofile_create"), {"name": "Admin Printer"})
        self.assertEqual(resp.status_code, 302)


@override_settings(ALLOWED_HOSTS=["testserver"])
class UnauthenticatedAccessTests(TestCase):
    """Verify unauthenticated users cannot access write endpoints.

    Note: RoleRequiredMixin sets raise_exception=True, so unauthenticated
    users get 403 instead of a login redirect for permission-protected views.
    """

    def test_project_create_denied(self):
        resp = self.client.get(reverse("core:project_create"))
        self.assertIn(resp.status_code, [302, 403])

    def test_printerprofile_create_denied(self):
        resp = self.client.get(reverse("core:printerprofile_create"))
        self.assertIn(resp.status_code, [302, 403])

    def test_user_list_denied(self):
        resp = self.client.get(reverse("core:user_list"))
        self.assertIn(resp.status_code, [302, 403])

    def test_printjob_create_denied(self):
        resp = self.client.get(reverse("core:printjob_create"))
        self.assertIn(resp.status_code, [302, 403])

    def test_printqueue_create_denied(self):
        resp = self.client.get(reverse("core:printqueue_create"))
        self.assertIn(resp.status_code, [302, 403])
