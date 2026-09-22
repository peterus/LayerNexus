"""Composition edge tests: components + project↔part edges (cycle-guarded, idempotent)."""

from __future__ import annotations

from django.contrib.auth.models import Permission, User
from rest_framework.authtoken.models import Token
from rest_framework.test import APITestCase

from core.models import Part, Project, ProjectComponent, ProjectPart


class ApiComponentEdgeTests(APITestCase):
    """Sub-project composition edges under ``/projects/{id}/components/``."""

    def setUp(self) -> None:
        """Authenticate a manage-capable user and create two projects."""
        self.user = User.objects.create_user("designer", password="x")
        self.user.user_permissions.add(Permission.objects.get(codename="can_manage_projects"))
        token = Token.objects.create(user=self.user)
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {token.key}")
        self.parent = Project.objects.create(name="Assembly")
        self.child = Project.objects.create(name="Module")

    def test_create_component_edge(self) -> None:
        """POST creates a ProjectComponent edge with quantity."""
        resp = self.client.post(
            f"/api/v1/projects/{self.parent.pk}/components/",
            {"child_project": self.child.pk, "quantity": 4},
            format="json",
        )
        self.assertEqual(resp.status_code, 201, resp.data)
        edge = ProjectComponent.objects.get(parent_project=self.parent, child_project=self.child)
        self.assertEqual(edge.quantity, 4)

    def test_cycle_is_400(self) -> None:
        """A component edge that would close a cycle returns 400, not 500."""
        # parent -> child
        self.parent.child_links.create(child_project=self.child, quantity=1)
        # attempt child -> parent (cycle)
        resp = self.client.post(
            f"/api/v1/projects/{self.child.pk}/components/",
            {"child_project": self.parent.pk, "quantity": 1},
            format="json",
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn("child_project", resp.data)

    def test_self_parent_is_400(self) -> None:
        """Attaching a project to itself returns 400."""
        resp = self.client.post(
            f"/api/v1/projects/{self.parent.pk}/components/",
            {"child_project": self.parent.pk, "quantity": 1},
            format="json",
        )
        self.assertEqual(resp.status_code, 400)

    def test_duplicate_attach_is_idempotent(self) -> None:
        """Re-attaching the same child returns the existing edge (200), not a 500/duplicate."""
        first = self.client.post(
            f"/api/v1/projects/{self.parent.pk}/components/",
            {"child_project": self.child.pk, "quantity": 2},
            format="json",
        )
        self.assertEqual(first.status_code, 201)
        second = self.client.post(
            f"/api/v1/projects/{self.parent.pk}/components/",
            {"child_project": self.child.pk, "quantity": 9},
            format="json",
        )
        self.assertEqual(second.status_code, 200)
        self.assertEqual(ProjectComponent.objects.filter(parent_project=self.parent).count(), 1)
        # original quantity preserved (get_or_create did not overwrite)
        self.assertEqual(ProjectComponent.objects.get(pk=second.data["id"]).quantity, 2)

    def test_patch_and_delete_component(self) -> None:
        """PATCH updates quantity; DELETE removes the edge."""
        edge = self.parent.child_links.create(child_project=self.child, quantity=1)
        resp = self.client.patch(
            f"/api/v1/projects/{self.parent.pk}/components/{edge.pk}/",
            {"quantity": 7},
            format="json",
        )
        self.assertEqual(resp.status_code, 200)
        edge.refresh_from_db()
        self.assertEqual(edge.quantity, 7)

        resp = self.client.delete(f"/api/v1/projects/{self.parent.pk}/components/{edge.pk}/")
        self.assertEqual(resp.status_code, 204)
        self.assertFalse(ProjectComponent.objects.filter(pk=edge.pk).exists())

    def test_delete_component_clears_stale_legacy_parent_fk(self) -> None:
        """Deleting an edge via the API clears the child's stale legacy parent FK too.

        The detach durability (clearing the mirror FK so the former parent stays
        deletable under ``on_delete=PROTECT``) lives in ``ProjectComponent.delete``, so
        it applies to the DRF path as well as the HTML view.
        """
        child = Project.objects.create(name="LegacyModule", parent=self.parent, quantity=2)
        edge = child.parent_links.get()  # seeded on insert by Project.save()

        resp = self.client.delete(f"/api/v1/projects/{self.parent.pk}/components/{edge.pk}/")
        self.assertEqual(resp.status_code, 204)

        child.refresh_from_db()
        self.assertIsNone(child.parent_id)
        self.assertEqual(self.parent.subprojects.count(), 0)


class ApiPartEdgeTests(APITestCase):
    """Project↔part composition edges under ``/projects/{id}/parts/``."""

    def setUp(self) -> None:
        """Authenticate a manage-capable user and create a project and a part."""
        self.user = User.objects.create_user("designer", password="x")
        self.user.user_permissions.add(Permission.objects.get(codename="can_manage_projects"))
        token = Token.objects.create(user=self.user)
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {token.key}")
        self.owner = Project.objects.create(name="Owner")
        self.other = Project.objects.create(name="Other")
        self.part = Part.objects.create(name="Shared bracket")

    def test_attach_part_edge(self) -> None:
        """POST attaches an existing part to another project via an edge."""
        resp = self.client.post(
            f"/api/v1/projects/{self.other.pk}/parts/",
            {"part": self.part.pk, "quantity": 3},
            format="json",
        )
        self.assertEqual(resp.status_code, 201, resp.data)
        edge = ProjectPart.objects.get(project=self.other, part=self.part)
        self.assertEqual(edge.quantity, 3)

    def test_attach_part_idempotent(self) -> None:
        """Re-attaching the same part returns the existing edge (200)."""
        self.client.post(
            f"/api/v1/projects/{self.other.pk}/parts/",
            {"part": self.part.pk, "quantity": 3},
            format="json",
        )
        resp = self.client.post(
            f"/api/v1/projects/{self.other.pk}/parts/",
            {"part": self.part.pk, "quantity": 8},
            format="json",
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(ProjectPart.objects.filter(project=self.other, part=self.part).count(), 1)

    def test_patch_and_delete_part_edge(self) -> None:
        """PATCH updates quantity; DELETE removes the edge."""
        edge = self.other.part_links.create(part=self.part, quantity=1)
        resp = self.client.patch(
            f"/api/v1/projects/{self.other.pk}/parts/{edge.pk}/",
            {"quantity": 6},
            format="json",
        )
        self.assertEqual(resp.status_code, 200)
        edge.refresh_from_db()
        self.assertEqual(edge.quantity, 6)

        resp = self.client.delete(f"/api/v1/projects/{self.other.pk}/parts/{edge.pk}/")
        self.assertEqual(resp.status_code, 204)
        self.assertFalse(ProjectPart.objects.filter(pk=edge.pk).exists())
