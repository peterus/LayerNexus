"""Tests for printer profile views."""

from unittest import mock

from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from core.models import PrinterProfile, PrintJob, PrintJobPlate, PrintQueue
from core.services.queue import start_next_for_printer
from core.tests.mixins import TestDataMixin, _RBACTestBase, queue_state_fields


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


@override_settings(ALLOWED_HOSTS=["testserver"])
class PrinterDeactivationTests(_RBACTestBase):
    """Printers can be deactivated; printers with print history cannot be deleted."""

    def setUp(self):
        super().setUp()
        self.printer = PrinterProfile.objects.create(name="Farm A", moonraker_url="http://printer-a:7125")
        self.job = PrintJob.objects.create(name="Deact Job", status=PrintJob.STATUS_SLICED)
        self.plate = PrintJobPlate.objects.create(
            print_job=self.job, plate_number=1, gcode_file="gcode_jobs/deact.gcode"
        )

    def test_delete_printer_with_attempts_refused_with_message(self):
        PrintQueue.objects.create(
            plate=self.plate,
            printer=self.printer,
            result=PrintQueue.RESULT_PASSED,
            resolved_at=timezone.now(),
            **queue_state_fields("awaiting_review"),
        )
        self.client.login(username="admin_user", password="testpass123")
        url = reverse("core:printerprofile_delete", args=[self.printer.pk])
        page = self.client.get(url)
        self.assertContains(page, "Cannot delete this printer.")
        resp = self.client.post(url, follow=True)
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(PrinterProfile.objects.filter(pk=self.printer.pk).exists())
        self.assertIn("it has print history", " ".join(str(m) for m in resp.context["messages"]))

    def test_delete_printer_without_attempts_works(self):
        self.client.login(username="admin_user", password="testpass123")
        resp = self.client.post(reverse("core:printerprofile_delete", args=[self.printer.pk]))
        self.assertEqual(resp.status_code, 302)
        self.assertFalse(PrinterProfile.objects.filter(pk=self.printer.pk).exists())

    @mock.patch("core.services.queue.get_printer_backend")
    def test_inactive_printer_does_not_start_waiting_attempts(self, mock_get_backend):
        entry = PrintQueue.enqueue(plate=self.plate, printer=self.printer)
        PrinterProfile.objects.filter(pk=self.printer.pk).update(is_active=False)

        # The caller's instance is stale (still active): the flag is re-read.
        result = start_next_for_printer(self.printer)
        self.assertEqual(result.outcome, "inactive")
        mock_get_backend.return_value.start_print.assert_not_called()

        self.client.login(username="operator_user", password="testpass123")
        self.client.post(reverse("core:run_all_queues"))
        mock_get_backend.return_value.start_print.assert_not_called()

        # The waiting attempt stays visible and can still be removed.
        resp = self.client.get(reverse("core:printqueue_list"))
        self.assertIn(entry.pk, [e.pk for e in resp.context["queue_entries"]])
        self.client.post(reverse("core:printqueue_delete", args=[entry.pk]))
        self.assertFalse(PrintQueue.objects.filter(pk=entry.pk).exists())

    def test_inactive_printer_hidden_from_farm_and_queue_form(self):
        self.printer.is_active = False
        self.printer.save()
        active = PrinterProfile.objects.create(name="Farm B")
        self.client.login(username="operator_user", password="testpass123")
        farm = self.client.get(reverse("core:farm_dashboard"))
        self.assertNotIn(self.printer, [c["printer"] for c in farm.context["printer_cards"]])
        form = self.client.get(reverse("core:printqueue_create")).context["form"]
        self.assertNotIn(self.printer, form.fields["printer"].queryset)
        self.assertIn(active, form.fields["printer"].queryset)
        resp = self.client.post(
            reverse("core:printqueue_create"),
            {"plate": self.plate.pk, "printer": self.printer.pk, "priority": 2},
        )
        self.assertEqual(resp.status_code, 200)  # form error, nothing queued
        self.assertFalse(PrintQueue.objects.exists())

    def test_is_active_editable_by_printer_manager_only(self):
        url = reverse("core:printerprofile_update", args=[self.printer.pk])
        data = {"name": "Farm A"}  # unchecked is_active
        self.client.login(username="designer_user", password="testpass123")
        self.assertEqual(self.client.post(url, data).status_code, 403)
        self.printer.refresh_from_db()
        self.assertTrue(self.printer.is_active)
        self.client.login(username="operator_user", password="testpass123")
        self.assertEqual(self.client.post(url, data).status_code, 302)
        self.printer.refresh_from_db()
        self.assertFalse(self.printer.is_active)

    def test_admin_cannot_delete_started_attempt(self):
        attempt = PrintQueue.objects.create(plate=self.plate, printer=self.printer, **queue_state_fields("printing"))
        self.client.login(username="admin_user", password="testpass123")
        resp = self.client.post(reverse("admin:core_printqueue_delete", args=[attempt.pk]), {"post": "yes"})
        self.assertEqual(resp.status_code, 403)
        self.assertTrue(PrintQueue.objects.filter(pk=attempt.pk).exists())
