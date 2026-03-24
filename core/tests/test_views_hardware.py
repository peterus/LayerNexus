"""Tests for hardware CRUD views."""

from django.test import Client, TestCase
from django.urls import reverse

from core.models import HardwarePart, ProjectHardware
from core.tests.mixins import TestDataMixin


class ProjectHardwareViewTests(TestDataMixin, TestCase):
    """Tests for hardware CRUD views."""

    def setUp(self):
        super().setUp()
        self.client = Client()
        self.client.login(username="testuser", password="testpass123")

    def test_create_hardware(self):
        url = reverse("core:hardware_create", args=[self.project.pk])
        resp = self.client.post(
            url,
            {
                "new_name": "M5x20",
                "new_category": "screws",
                "quantity": 10,
                "notes": "",
            },
        )
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(self.project.hardware_assignments.count(), 1)

    def test_update_hardware(self):
        hp = HardwarePart.objects.create(name="M3x10", category="screws")
        ph = ProjectHardware.objects.create(project=self.project, hardware_part=hp, quantity=5)
        url = reverse("core:hardware_update", args=[ph.pk])
        resp = self.client.post(
            url,
            {
                "hw_name": "M3x12",
                "hw_category": "screws",
                "hw_url": "",
                "hw_unit_price": "0.15",
                "hw_notes": "",
                "quantity": 10,
                "notes": "Updated",
            },
        )
        self.assertEqual(resp.status_code, 302)
        ph.refresh_from_db()
        self.assertEqual(ph.quantity, 10)
        ph.hardware_part.refresh_from_db()
        self.assertEqual(ph.hardware_part.name, "M3x12")

    def test_delete_hardware(self):
        hp = HardwarePart.objects.create(name="M3x10", category="screws")
        ph = ProjectHardware.objects.create(project=self.project, hardware_part=hp, quantity=5)
        url = reverse("core:hardware_delete", args=[ph.pk])
        resp = self.client.post(url)
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(self.project.hardware_assignments.count(), 0)
        # HardwarePart should still exist
        self.assertTrue(HardwarePart.objects.filter(pk=hp.pk).exists())

    def test_create_requires_permission(self):
        self.client.login(username="otheruser", password="otherpass123")
        url = reverse("core:hardware_create", args=[self.project.pk])
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 403)
