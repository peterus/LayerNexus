"""Tests for the build-API copy endpoints ``/projects/{id}/copy/`` and ``/parts/{id}/copy/``."""

from __future__ import annotations

from unittest import mock

from django.contrib.auth.models import Permission, User
from rest_framework.authtoken.models import Token
from rest_framework.test import APITestCase

from core.models import OrcaPrintPreset, Part, Project


def _preset(name: str) -> OrcaPrintPreset:
    """Create a resolved, instantiable print preset."""
    return OrcaPrintPreset.objects.create(
        name=name, orca_name=name, state=OrcaPrintPreset.STATE_RESOLVED, instantiation=True
    )


@mock.patch("core.views.helpers._start_orcaslicer_worker")
class ApiCopyTests(APITestCase):
    """R (preset A) → S → T; plus another root O (preset B)."""

    def setUp(self) -> None:
        self.user = User.objects.create_user("builder", password="x")
        self.user.user_permissions.add(Permission.objects.get(codename="can_manage_projects"))
        token = Token.objects.create(user=self.user)
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {token.key}")
        self.preset_a = _preset("A")
        self.preset_b = _preset("B")
        self.root = Project.objects.create(name="R", default_print_preset=self.preset_a)
        self.sub = Project.objects.create(name="S", parent=self.root, quantity=2)
        self.leaf = Project.objects.create(name="T", parent=self.sub)
        self.other = Project.objects.create(name="O", default_print_preset=self.preset_b)
        self.part = Part.objects.create(name="p", project=self.sub, quantity=3)

    def _copy(self, project: Project, body: dict) -> object:
        return self.client.post(f"/api/v1/projects/{project.pk}/copy/", body, format="json")

    def test_api_copy_project_as_root_default_name(self, _worker: mock.Mock) -> None:
        resp = self._copy(self.sub, {})
        self.assertEqual(resp.status_code, 201, resp.content)
        self.assertEqual(resp.data["name"], "S (copy)")
        self.assertIsNone(resp.data["parent"])
        new = Project.objects.get(pk=resp.data["id"])
        self.assertEqual(new.default_print_preset, self.preset_a)
        self.assertEqual(new.subprojects.get().name, "T")
        self.assertEqual(new.parts.get().quantity, 3)

    def test_api_copy_project_explicit_null_parent(self, _worker: mock.Mock) -> None:
        resp = self._copy(self.sub, {"name": "Fresh", "parent": None})
        self.assertEqual(resp.status_code, 201, resp.content)
        self.assertEqual(resp.data["name"], "Fresh")
        self.assertIsNone(resp.data["parent"])

    def test_api_copy_project_into_parent(self, _worker: mock.Mock) -> None:
        resp = self._copy(self.sub, {"name": "S in O", "parent": self.other.pk})
        self.assertEqual(resp.status_code, 201, resp.content)
        new = Project.objects.get(pk=resp.data["id"])
        self.assertEqual((new.name, new.parent, new.quantity), ("S in O", self.other, 2))
        self.assertEqual(self.sub.parent, self.root)

    def test_api_copy_into_own_subtree_400(self, _worker: mock.Mock) -> None:
        count = Project.objects.count()
        for target in (self.root, self.sub, self.leaf):
            resp = self._copy(self.root, {"parent": target.pk})
            self.assertEqual(resp.status_code, 400)
            self.assertIn("parent", resp.data)
        self.assertEqual(Project.objects.count(), count)

    def test_api_copy_unknown_parent_400(self, _worker: mock.Mock) -> None:
        resp = self._copy(self.root, {"parent": 999999})
        self.assertEqual(resp.status_code, 400)
        self.assertIn("parent", resp.data)

    def test_api_copy_part_requires_project_400(self, _worker: mock.Mock) -> None:
        count = Part.objects.count()
        for body in ({}, {"project": None}, {"project": 999999}):
            resp = self.client.post(f"/api/v1/parts/{self.part.pk}/copy/", body, format="json")
            self.assertEqual(resp.status_code, 400, body)
            self.assertIn("project", resp.data)
        self.assertEqual(Part.objects.count(), count)

    def test_api_copy_part_201(self, _worker: mock.Mock) -> None:
        resp = self.client.post(f"/api/v1/parts/{self.part.pk}/copy/", {"project": self.other.pk}, format="json")
        self.assertEqual(resp.status_code, 201, resp.content)
        self.assertEqual(resp.data["project"], self.other.pk)
        self.assertEqual(resp.data["name"], "p")
        self.assertNotEqual(resp.data["id"], self.part.pk)
        self.part.refresh_from_db()
        self.assertEqual(self.part.project, self.sub)

    def test_api_copy_forbidden_read_only_token(self, _worker: mock.Mock) -> None:
        reader = User.objects.create_user("reader", password="x")
        token = Token.objects.create(user=reader)
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {token.key}")
        before = (Project.objects.count(), Part.objects.count())
        self.assertEqual(self._copy(self.root, {}).status_code, 403)
        resp = self.client.post(f"/api/v1/parts/{self.part.pk}/copy/", {"project": self.other.pk}, format="json")
        self.assertEqual(resp.status_code, 403)
        self.assertEqual((Project.objects.count(), Part.objects.count()), before)

    def test_schema_lists_copy_endpoints(self, _worker: mock.Mock) -> None:
        resp = self.client.get("/api/schema/", HTTP_ACCEPT="application/vnd.oai.openapi")
        self.assertEqual(resp.status_code, 200)
        content = resp.content.decode()
        self.assertIn("/api/v1/projects/{id}/copy/", content)
        self.assertIn("/api/v1/parts/{id}/copy/", content)
        self.assertIn("ProjectCopyRequest", content)
        self.assertIn("PartCopyRequest", content)
