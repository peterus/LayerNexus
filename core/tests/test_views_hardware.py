"""Tests for hardware CRUD views (project assignments and catalogue library)."""

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


class HardwareLibraryViewTests(TestDataMixin, TestCase):
    """Tests for the hardware catalogue library views."""

    def setUp(self) -> None:
        super().setUp()
        self.client = Client()
        self.client.login(username="testuser", password="testpass123")
        self.hw1 = HardwarePart.objects.create(name="M3x10 Screw", category="screws")
        self.hw2 = HardwarePart.objects.create(name="608ZZ Bearing", category="bearings")
        self.hw3 = HardwarePart.objects.create(name="M5 Nut", category="nuts")

    def test_list_view_renders(self) -> None:
        resp = self.client.get(reverse("core:hardware_library"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "M3x10 Screw")
        self.assertContains(resp, "608ZZ Bearing")

    def test_list_search_by_name(self) -> None:
        resp = self.client.get(reverse("core:hardware_library") + "?q=screw")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "M3x10 Screw")
        self.assertNotContains(resp, "608ZZ Bearing")

    def test_list_filter_by_category(self) -> None:
        resp = self.client.get(reverse("core:hardware_library") + "?category=bearings")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "608ZZ Bearing")
        self.assertNotContains(resp, "M3x10 Screw")

    def test_list_requires_login(self) -> None:
        self.client.logout()
        resp = self.client.get(reverse("core:hardware_library"))
        self.assertEqual(resp.status_code, 302)

    def test_create_hardware_part(self) -> None:
        url = reverse("core:hardware_part_create")
        resp = self.client.post(
            url, {"name": "GT2 Belt", "category": "other", "url": "", "unit_price": "", "notes": ""}
        )
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(HardwarePart.objects.filter(name="GT2 Belt").exists())

    def test_create_sets_created_by(self) -> None:
        url = reverse("core:hardware_part_create")
        self.client.post(url, {"name": "NEMA17 Motor", "category": "motors", "url": "", "unit_price": "", "notes": ""})
        part = HardwarePart.objects.get(name="NEMA17 Motor")
        self.assertEqual(part.created_by, self.user)

    def test_create_requires_permission(self) -> None:
        self.client.login(username="otheruser", password="otherpass123")
        resp = self.client.get(reverse("core:hardware_part_create"))
        self.assertEqual(resp.status_code, 403)

    def test_update_hardware_part(self) -> None:
        url = reverse("core:hardware_part_update", args=[self.hw1.pk])
        resp = self.client.post(
            url,
            {"name": "M3x12 Screw", "category": "screws", "url": "", "unit_price": "0.05", "notes": ""},
        )
        self.assertEqual(resp.status_code, 302)
        self.hw1.refresh_from_db()
        self.assertEqual(self.hw1.name, "M3x12 Screw")
        self.assertEqual(str(self.hw1.unit_price), "0.05")

    def test_update_requires_permission(self) -> None:
        self.client.login(username="otheruser", password="otherpass123")
        resp = self.client.get(reverse("core:hardware_part_update", args=[self.hw1.pk]))
        self.assertEqual(resp.status_code, 403)

    def test_delete_hardware_part(self) -> None:
        url = reverse("core:hardware_part_delete", args=[self.hw2.pk])
        resp = self.client.post(url)
        self.assertEqual(resp.status_code, 302)
        self.assertFalse(HardwarePart.objects.filter(pk=self.hw2.pk).exists())

    def test_delete_requires_permission(self) -> None:
        self.client.login(username="otheruser", password="otherpass123")
        resp = self.client.post(reverse("core:hardware_part_delete", args=[self.hw1.pk]))
        self.assertEqual(resp.status_code, 403)

    def test_delete_blocked_when_in_use(self) -> None:
        ProjectHardware.objects.create(project=self.project, hardware_part=self.hw1, quantity=3)
        url = reverse("core:hardware_part_delete", args=[self.hw1.pk])
        resp = self.client.post(url)
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(HardwarePart.objects.filter(pk=self.hw1.pk).exists())

    def test_delete_get_shows_warning_when_in_use(self) -> None:
        ProjectHardware.objects.create(project=self.project, hardware_part=self.hw1, quantity=3)
        url = reverse("core:hardware_part_delete", args=[self.hw1.pk])
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Cannot delete")

    def test_used_in_count_in_list(self) -> None:
        ProjectHardware.objects.create(project=self.project, hardware_part=self.hw1, quantity=2)
        resp = self.client.get(reverse("core:hardware_library"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, ">1<")  # badge with count 1 for hw1
