"""Search-filter and duplicate-as-variant tests for the build API."""

from __future__ import annotations

from django.contrib.auth.models import Permission, User
from rest_framework.authtoken.models import Token
from rest_framework.test import APITestCase

from core.models import Part, Project, ProjectComponent


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
        project = Project.objects.create(name="Module")
        Part.objects.create(project=project, name="Bracket", material="PLA")
        Part.objects.create(project=project, name="Gear", material="PETG")
        by_name = self.client.get("/api/v1/parts/?search=bracket")
        self.assertEqual([p["name"] for p in _results(by_name.data)], ["Bracket"])
        by_material = self.client.get("/api/v1/parts/?search=petg")
        self.assertEqual([p["name"] for p in _results(by_material.data)], ["Gear"])


class ApiDuplicateTests(APITestCase):
    """``POST /projects/{id}/duplicate/`` clones an assembly into a new variant."""

    def setUp(self) -> None:
        """Prepare a manage-capable client and a source assembly with a child edge."""
        self.user = User.objects.create_user("designer", password="x")
        self.user.user_permissions.add(Permission.objects.get(codename="can_manage_projects"))
        token = Token.objects.create(user=self.user)
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {token.key}")

        self.source = Project.objects.create(name="Base Truck")
        self.child = Project.objects.create(name="Hood")
        ProjectComponent.objects.create(parent_project=self.source, child_project=self.child, quantity=2)

    def test_duplicate_creates_variant(self) -> None:
        """Duplicating returns 201 with a new project that shares the child edges."""
        resp = self.client.post(
            f"/api/v1/projects/{self.source.pk}/duplicate/",
            {"name": "Truck v2"},
            format="json",
        )
        self.assertEqual(resp.status_code, 201, resp.data)
        new_id = resp.data["id"]
        self.assertNotEqual(new_id, self.source.pk)
        variant = Project.objects.get(pk=new_id)
        self.assertEqual(variant.name, "Truck v2")
        self.assertEqual(variant.created_by, self.user)
        # The child composition edge is shared (same child project, same quantity).
        edges = list(variant.child_links.all())
        self.assertEqual(len(edges), 1)
        self.assertEqual(edges[0].child_project_id, self.child.pk)
        self.assertEqual(edges[0].quantity, 2)

    def test_duplicate_requires_name(self) -> None:
        """A missing/blank name is rejected with 400."""
        resp = self.client.post(f"/api/v1/projects/{self.source.pk}/duplicate/", {}, format="json")
        self.assertEqual(resp.status_code, 400, resp.data)

    def test_duplicate_forbidden_without_manage(self) -> None:
        """A user without manage permission gets 403 (duplicate is a write)."""
        viewer = User.objects.create_user("viewer", password="x")
        token = Token.objects.create(user=viewer)
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {token.key}")
        resp = self.client.post(
            f"/api/v1/projects/{self.source.pk}/duplicate/",
            {"name": "Nope"},
            format="json",
        )
        self.assertEqual(resp.status_code, 403, resp.data)
