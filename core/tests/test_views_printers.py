"""Tests for printer profile views."""

from django.test import TestCase, override_settings
from django.urls import reverse

from core.models import PrinterProfile
from core.tests.mixins import TestDataMixin


@override_settings(ALLOWED_HOSTS=["testserver"])
class PrinterProfileViewTests(TestDataMixin, TestCase):
    """Tests for PrinterProfile CRUD views."""

    def setUp(self):
        super().setUp()
        self.client.login(username="testuser", password="testpass123")

    def test_list_200(self):
        resp = self.client.get(reverse("core:printerprofile_list"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Test Printer")

    def test_list_only_own(self):
        resp = self.client.get(reverse("core:printerprofile_list"))
        self.assertContains(resp, "Other Printer")

    def test_create_get(self):
        resp = self.client.get(reverse("core:printerprofile_create"))
        self.assertEqual(resp.status_code, 200)

    def test_create_post(self):
        resp = self.client.post(
            reverse("core:printerprofile_create"),
            {
                "name": "New Printer",
            },
        )
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(PrinterProfile.objects.filter(name="New Printer", created_by=self.user).exists())

    def test_update_get(self):
        resp = self.client.get(reverse("core:printerprofile_update", args=[self.printer.pk]))
        self.assertEqual(resp.status_code, 200)

    def test_update_post(self):
        resp = self.client.post(
            reverse("core:printerprofile_update", args=[self.printer.pk]),
            {
                "name": "Renamed Printer",
            },
        )
        self.assertEqual(resp.status_code, 302)
        self.printer.refresh_from_db()
        self.assertEqual(self.printer.name, "Renamed Printer")

    def test_update_other_user_404(self):
        resp = self.client.post(
            reverse("core:printerprofile_update", args=[self.other_printer.pk]),
            {"name": "Hacked"},
        )
        self.assertEqual(resp.status_code, 302)

    def test_delete_get(self):
        resp = self.client.get(reverse("core:printerprofile_delete", args=[self.printer.pk]))
        self.assertEqual(resp.status_code, 200)

    def test_delete_post(self):
        pk = self.printer.pk
        resp = self.client.post(reverse("core:printerprofile_delete", args=[pk]))
        self.assertEqual(resp.status_code, 302)
        self.assertFalse(PrinterProfile.objects.filter(pk=pk).exists())


@override_settings(ALLOWED_HOSTS=["testserver"])
class PrinterOperatorPromptsFlagTests(TestDataMixin, TestCase):
    """Tests for the per-printer ``operator_prompts_enabled`` opt-in flag."""

    def setUp(self):
        super().setUp()
        from django.contrib.auth.models import Group, User

        self.operator = User.objects.create_user(username="op", password="testpass123")
        self.operator.groups.add(Group.objects.get(name="Operator"))
        self.client.login(username="op", password="testpass123")

    def test_operator_prompts_enabled_defaults_false(self):
        printer = PrinterProfile.objects.create(name="Fresh")
        self.assertFalse(printer.operator_prompts_enabled)

    def test_update_printer_sets_operator_prompts_enabled(self):
        resp = self.client.post(
            reverse("core:printerprofile_update", args=[self.printer.pk]),
            {"name": "Test Printer", "operator_prompts_enabled": "on"},
        )
        self.assertEqual(resp.status_code, 302)
        self.printer.refresh_from_db()
        self.assertTrue(self.printer.operator_prompts_enabled)

    def test_update_printer_clears_operator_prompts_enabled(self):
        self.printer.operator_prompts_enabled = True
        self.printer.save()
        self.client.post(reverse("core:printerprofile_update", args=[self.printer.pk]), {"name": "Test Printer"})
        self.printer.refresh_from_db()
        self.assertFalse(self.printer.operator_prompts_enabled)

    def test_form_renders_checkbox(self):
        resp = self.client.get(reverse("core:printerprofile_update", args=[self.printer.pk]))
        self.assertContains(resp, 'name="operator_prompts_enabled"')
        self.assertContains(resp, "Show operator dialogs on the printer")
