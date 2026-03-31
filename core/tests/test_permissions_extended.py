"""Extended RBAC tests for untested permission mixins.

Covers: PrinterControlMixin, QueueManageMixin, QueueDequeueMixin,
OrcaProfileManageMixin, FilamentMappingManageMixin.
"""

from django.test import override_settings
from django.urls import reverse

from core.models import (
    OrcaFilamentProfile,
    OrcaMachineProfile,
    OrcaPrintPreset,
    PrintJob,
    PrintJobPlate,
    PrintQueue,
)
from core.tests.mixins import _RBACTestBase


@override_settings(ALLOWED_HOSTS=["testserver"])
class PrinterControlMixinTests(_RBACTestBase):
    """Verify PrinterControlMixin restricts printer control actions.

    Operator and Admin have can_control_printer; Designer does NOT.
    Views: RunNextQueueView, RunAllQueuesView, CancelQueueEntryView.
    """

    def setUp(self):
        super().setUp()
        self.printer.moonraker_url = "http://printer:7125"
        self.printer.save()

        self.job = PrintJob.objects.create(
            name="Control Test Job",
            status=PrintJob.STATUS_SLICED,
            created_by=self.admin_user,
        )
        self.plate = PrintJobPlate.objects.create(
            print_job=self.job,
            plate_number=1,
            status=PrintJobPlate.STATUS_WAITING,
        )
        self.queue_entry = PrintQueue.objects.create(
            plate=self.plate,
            printer=self.printer,
            status=PrintQueue.STATUS_WAITING,
            priority=2,
        )

    def test_designer_cannot_run_queue(self):
        """Designer lacks can_control_printer -- run_queue should 403."""
        self.client.login(username="designer_user", password="testpass123")
        resp = self.client.post(reverse("core:run_queue", args=[self.printer.pk]))
        self.assertEqual(resp.status_code, 403)

    def test_designer_cannot_run_all_queues(self):
        """Designer lacks can_control_printer -- run_all_queues should 403."""
        self.client.login(username="designer_user", password="testpass123")
        resp = self.client.post(reverse("core:run_all_queues"))
        self.assertEqual(resp.status_code, 403)

    def test_designer_cannot_cancel_queue_entry(self):
        """Designer lacks can_control_printer -- cancel should 403."""
        self.client.login(username="designer_user", password="testpass123")
        resp = self.client.post(reverse("core:printqueue_cancel", args=[self.queue_entry.pk]))
        self.assertEqual(resp.status_code, 403)

    def test_operator_can_access_run_queue(self):
        """Operator has can_control_printer -- run_queue endpoint accessible.

        The actual POST may fail (no gcode), but access is not denied.
        """
        self.client.login(username="operator_user", password="testpass123")
        resp = self.client.post(reverse("core:run_queue", args=[self.printer.pk]))
        # Should not be 403 (permission denied)
        self.assertNotEqual(resp.status_code, 403)

    def test_admin_can_access_run_all_queues(self):
        """Admin has can_control_printer -- run_all_queues accessible."""
        self.client.login(username="admin_user", password="testpass123")
        resp = self.client.post(reverse("core:run_all_queues"))
        self.assertNotEqual(resp.status_code, 403)

    def test_unauthenticated_cannot_run_queue(self):
        """Unauthenticated users should be denied."""
        resp = self.client.post(reverse("core:run_queue", args=[self.printer.pk]))
        self.assertIn(resp.status_code, [302, 403])


@override_settings(ALLOWED_HOSTS=["testserver"])
class QueueManageMixinTests(_RBACTestBase):
    """Verify QueueManageMixin restricts queue creation.

    Both Operator and Designer have can_manage_print_queue.
    """

    def test_operator_can_access_queue_create(self):
        """Operator has can_manage_print_queue."""
        self.client.login(username="operator_user", password="testpass123")
        resp = self.client.get(reverse("core:printqueue_create"))
        self.assertNotEqual(resp.status_code, 403)

    def test_designer_can_access_queue_create(self):
        """Designer has can_manage_print_queue."""
        self.client.login(username="designer_user", password="testpass123")
        resp = self.client.get(reverse("core:printqueue_create"))
        self.assertNotEqual(resp.status_code, 403)

    def test_unauthenticated_cannot_access_queue_create(self):
        """Unauthenticated users should be denied."""
        resp = self.client.get(reverse("core:printqueue_create"))
        self.assertIn(resp.status_code, [302, 403])


@override_settings(ALLOWED_HOSTS=["testserver"])
class QueueDequeueMixinTests(_RBACTestBase):
    """Verify QueueDequeueMixin restricts queue deletion.

    Both Operator and Designer have can_dequeue_job.
    """

    def setUp(self):
        super().setUp()
        self.job = PrintJob.objects.create(
            name="Dequeue Test",
            status=PrintJob.STATUS_SLICED,
            created_by=self.admin_user,
        )
        self.plate = PrintJobPlate.objects.create(
            print_job=self.job,
            plate_number=1,
        )
        self.queue_entry = PrintQueue.objects.create(
            plate=self.plate,
            printer=self.printer,
            status=PrintQueue.STATUS_WAITING,
        )

    def test_operator_can_delete_queue_entry(self):
        """Operator has can_dequeue_job."""
        self.client.login(username="operator_user", password="testpass123")
        resp = self.client.post(reverse("core:printqueue_delete", args=[self.queue_entry.pk]))
        # Should succeed (302 redirect) or display confirmation
        self.assertNotEqual(resp.status_code, 403)

    def test_designer_can_delete_queue_entry(self):
        """Designer has can_dequeue_job."""
        self.client.login(username="designer_user", password="testpass123")
        resp = self.client.post(reverse("core:printqueue_delete", args=[self.queue_entry.pk]))
        self.assertNotEqual(resp.status_code, 403)

    def test_unauthenticated_cannot_delete_queue_entry(self):
        """Unauthenticated users should be denied."""
        resp = self.client.post(reverse("core:printqueue_delete", args=[self.queue_entry.pk]))
        self.assertIn(resp.status_code, [302, 403])


@override_settings(ALLOWED_HOSTS=["testserver"])
class OrcaProfileManageMixinTests(_RBACTestBase):
    """Verify OrcaProfileManageMixin restricts OrcaSlicer profile management.

    Operator and Admin have can_manage_orca_profiles; Designer does NOT.
    """

    def setUp(self):
        super().setUp()
        self.machine_profile = OrcaMachineProfile.objects.create(
            name="Test Machine",
            orca_name="test_machine",
            state=OrcaMachineProfile.STATE_RESOLVED,
        )
        self.filament_profile = OrcaFilamentProfile.objects.create(
            name="Test Filament",
            orca_name="test_filament",
            state=OrcaFilamentProfile.STATE_RESOLVED,
        )
        self.print_preset = OrcaPrintPreset.objects.create(
            name="Test Preset",
            orca_name="test_preset",
            state=OrcaPrintPreset.STATE_RESOLVED,
        )

    def test_designer_cannot_import_machine_profile(self):
        """Designer lacks can_manage_orca_profiles."""
        self.client.login(username="designer_user", password="testpass123")
        resp = self.client.get(reverse("core:orcamachineprofile_import"))
        self.assertEqual(resp.status_code, 403)

    def test_designer_cannot_delete_filament_profile(self):
        """Designer lacks can_manage_orca_profiles."""
        self.client.login(username="designer_user", password="testpass123")
        resp = self.client.post(reverse("core:orcafilamentprofile_delete", args=[self.filament_profile.pk]))
        self.assertEqual(resp.status_code, 403)

    def test_designer_cannot_delete_print_preset(self):
        """Designer lacks can_manage_orca_profiles."""
        self.client.login(username="designer_user", password="testpass123")
        resp = self.client.post(reverse("core:orcaprintpreset_delete", args=[self.print_preset.pk]))
        self.assertEqual(resp.status_code, 403)

    def test_operator_can_access_machine_import(self):
        """Operator has can_manage_orca_profiles."""
        self.client.login(username="operator_user", password="testpass123")
        resp = self.client.get(reverse("core:orcamachineprofile_import"))
        self.assertNotEqual(resp.status_code, 403)

    def test_operator_can_delete_machine_profile(self):
        """Operator has can_manage_orca_profiles."""
        self.client.login(username="operator_user", password="testpass123")
        resp = self.client.post(reverse("core:orcamachineprofile_delete", args=[self.machine_profile.pk]))
        # 302 = redirect after delete, 200 = confirmation page
        self.assertNotEqual(resp.status_code, 403)

    def test_admin_can_import_filament_profile(self):
        """Admin has can_manage_orca_profiles."""
        self.client.login(username="admin_user", password="testpass123")
        resp = self.client.get(reverse("core:orcafilamentprofile_import"))
        self.assertNotEqual(resp.status_code, 403)

    def test_designer_can_view_profile_lists(self):
        """Designer can view profile lists (read-only)."""
        self.client.login(username="designer_user", password="testpass123")

        resp = self.client.get(reverse("core:orcamachineprofile_list"))
        self.assertEqual(resp.status_code, 200)

        resp = self.client.get(reverse("core:orcafilamentprofile_list"))
        self.assertEqual(resp.status_code, 200)

        resp = self.client.get(reverse("core:orcaprintpreset_list"))
        self.assertEqual(resp.status_code, 200)


@override_settings(ALLOWED_HOSTS=["testserver"])
class FilamentMappingManageMixinTests(_RBACTestBase):
    """Verify FilamentMappingManageMixin restricts filament mapping management.

    Operator and Admin have can_manage_filament_mappings; Designer does NOT.
    """

    def test_designer_cannot_save_filament_mapping(self):
        """Designer lacks can_manage_filament_mappings."""
        self.client.login(username="designer_user", password="testpass123")
        resp = self.client.post(
            reverse("core:save_filament_mapping"),
            {"spoolman_filament_id": "1", "filament_name": "PLA", "orca_filament_profile_id": ""},
        )
        self.assertEqual(resp.status_code, 403)

    def test_operator_can_save_filament_mapping(self):
        """Operator has can_manage_filament_mappings."""
        self.client.login(username="operator_user", password="testpass123")
        resp = self.client.post(
            reverse("core:save_filament_mapping"),
            {"spoolman_filament_id": "1", "filament_name": "PLA", "orca_filament_profile_id": ""},
        )
        # Should not be 403 (may redirect or show success)
        self.assertNotEqual(resp.status_code, 403)

    def test_admin_can_save_filament_mapping(self):
        """Admin has can_manage_filament_mappings."""
        self.client.login(username="admin_user", password="testpass123")
        resp = self.client.post(
            reverse("core:save_filament_mapping"),
            {"spoolman_filament_id": "2", "filament_name": "PETG", "orca_filament_profile_id": ""},
        )
        self.assertNotEqual(resp.status_code, 403)

    def test_unauthenticated_cannot_save_filament_mapping(self):
        """Unauthenticated users should be denied."""
        resp = self.client.post(
            reverse("core:save_filament_mapping"),
            {"spoolman_filament_id": "1", "filament_name": "PLA"},
        )
        self.assertIn(resp.status_code, [302, 403])
