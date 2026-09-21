"""Hardware catalogue + assignment tests for the build API."""

from __future__ import annotations

from django.contrib.auth.models import Permission, User
from rest_framework.authtoken.models import Token
from rest_framework.test import APITestCase

from core.models import HardwarePart, Project, ProjectHardware


class ApiHardwareTests(APITestCase):
    """Hardware catalogue CRUD and project hardware assignments."""

    def setUp(self) -> None:
        """Authenticate a manage-capable user and create a project."""
        self.user = User.objects.create_user("designer", password="x")
        self.user.user_permissions.add(Permission.objects.get(codename="can_manage_projects"))
        token = Token.objects.create(user=self.user)
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {token.key}")
        self.project = Project.objects.create(name="Module")

    def test_hardware_catalog_create_and_list(self) -> None:
        """A hardware part can be created in the catalogue and listed."""
        resp = self.client.post(
            "/api/v1/hardware-parts/",
            {"name": "M3x8 screw", "category": "screws", "unit_price": "0.05"},
            format="json",
        )
        self.assertEqual(resp.status_code, 201, resp.data)
        hp = HardwarePart.objects.get(pk=resp.data["id"])
        self.assertEqual(hp.created_by, self.user)

        resp = self.client.get("/api/v1/hardware-parts/")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(resp.data), 1)

    def test_assign_hardware_to_project(self) -> None:
        """A hardware part can be assigned to a project and removed."""
        hp = HardwarePart.objects.create(name="Bearing 608", category="bearings")
        resp = self.client.post(
            f"/api/v1/projects/{self.project.pk}/hardware/",
            {"hardware_part": hp.pk, "quantity": 8},
            format="json",
        )
        self.assertEqual(resp.status_code, 201, resp.data)
        assignment = ProjectHardware.objects.get(project=self.project, hardware_part=hp)
        self.assertEqual(assignment.quantity, 8)

        resp = self.client.delete(f"/api/v1/projects/{self.project.pk}/hardware/{assignment.pk}/")
        self.assertEqual(resp.status_code, 204)
        self.assertFalse(ProjectHardware.objects.filter(pk=assignment.pk).exists())

    def test_assign_hardware_idempotent(self) -> None:
        """Re-assigning the same hardware part returns the existing assignment (200)."""
        hp = HardwarePart.objects.create(name="Nut M3", category="nuts")
        self.client.post(
            f"/api/v1/projects/{self.project.pk}/hardware/",
            {"hardware_part": hp.pk, "quantity": 4},
            format="json",
        )
        resp = self.client.post(
            f"/api/v1/projects/{self.project.pk}/hardware/",
            {"hardware_part": hp.pk, "quantity": 10},
            format="json",
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(ProjectHardware.objects.filter(project=self.project, hardware_part=hp).count(), 1)

    def test_hardware_write_requires_manage(self) -> None:
        """A reader without manage permission cannot create catalogue entries (403)."""
        reader = User.objects.create_user("reader", password="x")
        token = Token.objects.create(user=reader)
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {token.key}")
        resp = self.client.post(
            "/api/v1/hardware-parts/",
            {"name": "Spring", "category": "springs"},
            format="json",
        )
        self.assertEqual(resp.status_code, 403)

    def test_delete_in_use_part_returns_409(self) -> None:
        """DELETE on a catalogue entry that is still assigned returns 409 Conflict."""
        hp = HardwarePart.objects.create(name="In-use bolt", category="screws")
        ProjectHardware.objects.create(project=self.project, hardware_part=hp, quantity=1)
        resp = self.client.delete(f"/api/v1/hardware-parts/{hp.pk}/")
        self.assertEqual(resp.status_code, 409)
        self.assertTrue(HardwarePart.objects.filter(pk=hp.pk).exists())
