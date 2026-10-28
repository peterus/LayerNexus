"""Search-filter tests and duplicate-removal checks for the build API."""

from __future__ import annotations

from django.contrib.auth.models import Group, User
from rest_framework.authtoken.models import Token
from rest_framework.test import APITestCase

from core.models import Part, Project


def _results(data: object) -> list:
    """Unwrap a DRF list response whether or not pagination is enabled."""
    if isinstance(data, dict) and "results" in data:
        return data["results"]
    return list(data)  # type: ignore[arg-type]


class ApiSearchTests(APITestCase):
    """DRF ``SearchFilter`` narrows the project and part list endpoints."""

    def setUp(self) -> None:
        """Authenticate a plain (read-only) user — search is a read."""
        self.user = User.objects.create_user("viewer", password="x")
        token = Token.objects.create(user=self.user)
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {token.key}")

    def test_project_search_by_name(self) -> None:
        """?search matches project name and excludes non-matching projects."""
        Project.objects.create(name="Mars Rover")
        Project.objects.create(name="Moon Lander")
        resp = self.client.get("/api/v1/projects/?search=rover")
        self.assertEqual(resp.status_code, 200, resp.data)
        names = [p["name"] for p in _results(resp.data)]
        self.assertEqual(names, ["Mars Rover"])

    def test_part_search_by_name_and_material(self) -> None:
        """?search matches part name or material."""
        project = Project.objects.create(name="Rover")
        Part.objects.create(project=project, name="Bracket", material="PLA")
        Part.objects.create(project=project, name="Gear", material="PETG")
        by_name = self.client.get("/api/v1/parts/?search=bracket")
        self.assertEqual([p["name"] for p in _results(by_name.data)], ["Bracket"])
        by_material = self.client.get("/api/v1/parts/?search=petg")
        self.assertEqual([p["name"] for p in _results(by_material.data)], ["Gear"])


class ApiDuplicateRemovedTests(APITestCase):
    """Duplicate-as-variant is gone with the tree rollback (copy replaces it later)."""

    def setUp(self) -> None:
        """Authenticate an Admin — the endpoint must be absent, not merely forbidden."""
        self.user = User.objects.create_user("admin", password="x")
        self.user.groups.add(Group.objects.get(name="Admin"))
        token = Token.objects.create(user=self.user)
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {token.key}")
        self.source = Project.objects.create(name="Base Truck")

    def test_duplicate_endpoint_gone(self) -> None:
        """``POST /projects/{id}/duplicate/`` returns 404 and creates nothing."""
        resp = self.client.post(f"/api/v1/projects/{self.source.pk}/duplicate/", {"name": "Truck v2"}, format="json")
        self.assertEqual(resp.status_code, 404)
        self.assertFalse(Project.objects.filter(name="Truck v2").exists())

    def test_model_method_gone(self) -> None:
        """``Project.duplicate_as_variant`` no longer exists."""
        self.assertFalse(hasattr(Project, "duplicate_as_variant"))
