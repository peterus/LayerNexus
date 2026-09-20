"""Project & Part CRUD + tree tests for the build API."""

from __future__ import annotations

from django.contrib.auth.models import Permission, User
from rest_framework.authtoken.models import Token
from rest_framework.test import APITestCase

from core.models import Part, Project


class ApiProjectPartTests(APITestCase):
    """CRUD round-trips for projects and parts plus the assembly tree."""

    def setUp(self) -> None:
        """Create a manage-capable user and authenticate the client with its token."""
        self.user = User.objects.create_user("designer", password="x")
        self.user.user_permissions.add(Permission.objects.get(codename="can_manage_projects"))
        token = Token.objects.create(user=self.user)
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {token.key}")

    def test_project_crud_roundtrip(self) -> None:
        """Create, read, patch and delete a project."""
        resp = self.client.post("/api/v1/projects/", {"name": "Truck", "description": "d"}, format="json")
        self.assertEqual(resp.status_code, 201)
        pk = resp.data["id"]

        resp = self.client.get(f"/api/v1/projects/{pk}/")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["name"], "Truck")

        resp = self.client.patch(f"/api/v1/projects/{pk}/", {"name": "Truck v2"}, format="json")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["name"], "Truck v2")

        resp = self.client.delete(f"/api/v1/projects/{pk}/")
        self.assertEqual(resp.status_code, 204)
        self.assertFalse(Project.objects.filter(pk=pk).exists())

    def test_part_crud_roundtrip(self) -> None:
        """Create a part in a project, patch scalar fields and delete it."""
        project = Project.objects.create(name="Module")
        resp = self.client.post(
            "/api/v1/parts/",
            {"project": project.pk, "name": "Gear", "quantity": 3, "material": "PLA", "color": "red"},
            format="json",
        )
        self.assertEqual(resp.status_code, 201, resp.data)
        pk = resp.data["id"]
        self.assertEqual(resp.data["quantity"], 3)

        resp = self.client.patch(
            f"/api/v1/parts/{pk}/",
            {"color": "blue", "notes": "tight fit", "quantity": 5},
            format="json",
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["color"], "blue")
        self.assertEqual(resp.data["quantity"], 5)
        self.assertEqual(resp.data["notes"], "tight fit")

        resp = self.client.delete(f"/api/v1/parts/{pk}/")
        self.assertEqual(resp.status_code, 204)
        self.assertFalse(Part.objects.filter(pk=pk).exists())

    def test_part_create_auto_edge(self) -> None:
        """Creating a part also materialises the matching ProjectPart edge (shim)."""
        project = Project.objects.create(name="Module")
        resp = self.client.post(
            "/api/v1/parts/",
            {"project": project.pk, "name": "Bracket"},
            format="json",
        )
        self.assertEqual(resp.status_code, 201)
        self.assertTrue(project.part_links.filter(part_id=resp.data["id"]).exists())

    def test_project_tree(self) -> None:
        """The tree endpoint returns nested child modules, direct parts and hardware."""
        parent = Project.objects.create(name="Assembly")
        child = Project.objects.create(name="Sub")
        parent.child_links.create(child_project=child, quantity=2)
        part = Part.objects.create(project=child, name="Screw holder", quantity=1)
        child.part_links.get_or_create(part=part, defaults={"quantity": 1})

        resp = self.client.get(f"/api/v1/projects/{parent.pk}/tree/")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["id"], parent.pk)
        self.assertEqual(len(resp.data["components"]), 1)
        component = resp.data["components"][0]
        self.assertEqual(component["quantity"], 2)
        self.assertEqual(component["child"]["id"], child.pk)
        self.assertEqual(len(component["child"]["parts"]), 1)
        self.assertEqual(component["child"]["parts"][0]["part"], part.pk)
