"""Project & Part CRUD + tree tests for the build API."""

from __future__ import annotations

from django.contrib.auth.models import Permission, User
from rest_framework.authtoken.models import Token
from rest_framework.test import APITestCase

from core.models import OrcaPrintPreset, Part, PrintJob, PrintJobPart, Project


class ApiProjectPartTests(APITestCase):
    """CRUD round-trips for projects and parts."""

    def setUp(self) -> None:
        """Create a manage-capable user and authenticate the client with its token."""
        self.user = User.objects.create_user("designer", password="x")
        self.user.user_permissions.add(Permission.objects.get(codename="can_manage_projects"))
        token = Token.objects.create(user=self.user)
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {token.key}")
        self.preset = OrcaPrintPreset.objects.create(
            name="P", orca_name="P", state=OrcaPrintPreset.STATE_RESOLVED, instantiation=True
        )

    def test_project_crud_roundtrip(self) -> None:
        """Create, read, patch and delete a project."""
        resp = self.client.post(
            "/api/v1/projects/",
            {"name": "Truck", "description": "d", "default_print_preset": self.preset.pk},
            format="json",
        )
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
        project = Project.objects.create(name="Module", default_print_preset=self.preset)
        resp = self.client.post(
            "/api/v1/parts/",
            {"project": project.pk, "name": "Gear", "material": "PLA", "color": "red"},
            format="json",
        )
        self.assertEqual(resp.status_code, 201, resp.data)
        pk = resp.data["id"]
        self.assertEqual(resp.data["quantity"], 1)

        resp = self.client.patch(
            f"/api/v1/parts/{pk}/",
            {"color": "blue", "notes": "tight fit"},
            format="json",
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["color"], "blue")
        self.assertEqual(resp.data["notes"], "tight fit")

        resp = self.client.delete(f"/api/v1/parts/{pk}/")
        self.assertEqual(resp.status_code, 204)
        self.assertFalse(Part.objects.filter(pk=pk).exists())


class ApiTreeContractTests(APITestCase):
    """The tree contract: parent/quantity on projects, project/quantity on parts."""

    def setUp(self) -> None:
        """Authenticate a manage-capable user; build R (preset) → S → T."""
        self.user = User.objects.create_user("designer", password="x")
        self.user.user_permissions.add(Permission.objects.get(codename="can_manage_projects"))
        token = Token.objects.create(user=self.user)
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {token.key}")
        self.preset = OrcaPrintPreset.objects.create(
            name="P", orca_name="P", state=OrcaPrintPreset.STATE_RESOLVED, instantiation=True
        )
        self.root = Project.objects.create(name="R", default_print_preset=self.preset)
        self.sub = Project.objects.create(name="S", parent=self.root, quantity=2)
        self.leaf = Project.objects.create(name="T", parent=self.sub, quantity=3)

    def test_root_quantity_is_always_one(self) -> None:
        resp = self.client.patch(f"/api/v1/projects/{self.root.pk}/", {"quantity": 5}, format="json")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("quantity", resp.data)
        resp = self.client.patch(
            f"/api/v1/projects/{self.sub.pk}/",
            {"parent": None, "default_print_preset": self.preset.pk},
            format="json",
        )
        self.assertEqual(resp.status_code, 200, resp.data)
        self.sub.refresh_from_db()
        self.assertEqual(self.sub.quantity, 1)

    def test_subproject_requirements_are_consistent(self) -> None:
        Part.objects.create(name="p", project=self.leaf, quantity=4, filament_used_grams=1.0)
        resp = self.client.get(f"/api/v1/projects/{self.sub.pk}/requirements/")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["total_parts_count"], 24)
        self.assertEqual(resp.data["total_filament_grams"], resp.data["filament_requirements"][0]["total_grams"])

    def test_tree_and_validate_query_count_flat(self) -> None:
        """tree/validate cost the same number of queries regardless of the part count."""
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        def count(action: str) -> int:
            with CaptureQueriesContext(connection) as ctx:
                resp = self.client.get(f"/api/v1/projects/{self.root.pk}/{action}/")
            self.assertEqual(resp.status_code, 200)
            return len(ctx.captured_queries)

        Part.objects.create(name="first", project=self.leaf)
        before = {action: count(action) for action in ("tree", "validate")}
        for i in range(3):
            Part.objects.create(name=f"p{i}", project=self.sub)
        self.assertEqual({action: count(action) for action in ("tree", "validate")}, before)

    def test_unresolved_preset_rejected(self) -> None:
        """Only resolved, instantiable presets can be assigned through the API."""
        raw = OrcaPrintPreset.objects.create(name="Raw", orca_name="Raw", instantiation=False)
        resp = self.client.patch(f"/api/v1/projects/{self.sub.pk}/", {"default_print_preset": raw.pk}, format="json")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("default_print_preset", resp.data)
        resp = self.client.post(
            "/api/v1/parts/", {"name": "x", "project": self.leaf.pk, "print_preset": raw.pk}, format="json"
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn("print_preset", resp.data)

    def test_create_part_requires_project(self) -> None:
        resp = self.client.post("/api/v1/parts/", {"name": "Bracket"}, format="json")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("project", resp.data)

    def test_create_part_with_project_and_quantity(self) -> None:
        resp = self.client.post(
            "/api/v1/parts/", {"name": "Bracket", "project": self.leaf.pk, "quantity": 4}, format="json"
        )
        self.assertEqual(resp.status_code, 201, resp.data)
        part = Part.objects.get(pk=resp.data["id"])
        self.assertEqual(part.project_id, self.leaf.pk)
        self.assertEqual(part.quantity, 4)
        self.assertEqual(part.effective_print_preset_id, self.preset.pk)

    def test_create_root_requires_preset(self) -> None:
        resp = self.client.post("/api/v1/projects/", {"name": "Bare"}, format="json")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("default_print_preset", resp.data)

    def test_create_subproject_without_preset_ok(self) -> None:
        resp = self.client.post(
            "/api/v1/projects/", {"name": "Axle", "parent": self.root.pk, "quantity": 2}, format="json"
        )
        self.assertEqual(resp.status_code, 201, resp.data)
        sub = Project.objects.get(pk=resp.data["id"])
        self.assertEqual(sub.parent_id, self.root.pk)
        self.assertEqual(sub.quantity, 2)
        self.assertIsNone(sub.default_print_preset_id)

    def test_patch_subproject_to_root_without_preset_400(self) -> None:
        resp = self.client.patch(f"/api/v1/projects/{self.sub.pk}/", {"parent": None}, format="json")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("default_print_preset", resp.data)
        self.sub.refresh_from_db()
        self.assertEqual(self.sub.parent_id, self.root.pk)

    def test_patch_subproject_to_root_with_preset_ok(self) -> None:
        resp = self.client.patch(
            f"/api/v1/projects/{self.sub.pk}/",
            {"parent": None, "default_print_preset": self.preset.pk},
            format="json",
        )
        self.assertEqual(resp.status_code, 200, resp.data)
        self.sub.refresh_from_db()
        self.assertIsNone(self.sub.parent_id)

    def test_patch_root_clear_preset_400(self) -> None:
        resp = self.client.patch(f"/api/v1/projects/{self.root.pk}/", {"default_print_preset": None}, format="json")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("default_print_preset", resp.data)

    def test_patch_parent_cycle_400(self) -> None:
        resp = self.client.patch(f"/api/v1/projects/{self.root.pk}/", {"parent": self.leaf.pk}, format="json")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("parent", resp.data)
        resp = self.client.patch(f"/api/v1/projects/{self.sub.pk}/", {"parent": self.sub.pk}, format="json")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("parent", resp.data)
        self.root.refresh_from_db()
        self.assertIsNone(self.root.parent_id)

    def test_patch_reparent_ok(self) -> None:
        other = Project.objects.create(name="Other", default_print_preset=self.preset)
        resp = self.client.patch(f"/api/v1/projects/{self.leaf.pk}/", {"parent": other.pk}, format="json")
        self.assertEqual(resp.status_code, 200, resp.data)
        self.leaf.refresh_from_db()
        self.assertEqual(self.leaf.parent_id, other.pk)

    def test_tree_is_nested_subprojects(self) -> None:
        part = Part.objects.create(name="Screw holder", project=self.leaf, quantity=5)
        resp = self.client.get(f"/api/v1/projects/{self.root.pk}/tree/")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["id"], self.root.pk)
        sub = resp.data["subprojects"][0]
        self.assertEqual(sub["id"], self.sub.pk)
        self.assertEqual(sub["quantity"], 2)
        leaf = sub["subprojects"][0]
        self.assertEqual(leaf["id"], self.leaf.pk)
        self.assertEqual(leaf["quantity"], 3)
        self.assertEqual(leaf["parts"], [{"part": part.pk, "name": "Screw holder", "quantity": 5}])
        self.assertNotIn("components", resp.data)

    def test_tree_terminates_on_corrupt_cycle(self) -> None:
        Project.objects.filter(pk=self.root.pk).update(parent=self.leaf)  # bypasses save()
        resp = self.client.get(f"/api/v1/projects/{self.root.pk}/tree/")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["subprojects"][0]["subprojects"][0]["subprojects"], [])

    def test_edge_endpoints_gone(self) -> None:
        for suffix in ("components/", "parts/", "components/1/", "parts/1/"):
            with self.subTest(suffix=suffix):
                resp = self.client.get(f"/api/v1/projects/{self.root.pk}/{suffix}")
                self.assertEqual(resp.status_code, 404)

    def test_api_delete_project_with_unfinished_job_409(self) -> None:
        part = Part.objects.create(name="deep", project=self.leaf)
        job = PrintJob.objects.create(name="busy", status=PrintJob.STATUS_SLICED)
        PrintJobPart.objects.create(print_job=job, part=part, quantity=1)
        resp = self.client.delete(f"/api/v1/projects/{self.root.pk}/")
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.data["blocking_jobs"], [job.pk])
        self.assertTrue(Project.objects.filter(pk=self.leaf.pk).exists())

    def test_api_delete_project_cascades_subtree_204(self) -> None:
        part = Part.objects.create(name="deep", project=self.leaf)
        job = PrintJob.objects.create(name="done", status=PrintJob.STATUS_COMPLETED)
        PrintJobPart.objects.create(print_job=job, part=part, quantity=1)
        resp = self.client.delete(f"/api/v1/projects/{self.sub.pk}/")
        self.assertEqual(resp.status_code, 204)
        self.assertFalse(Project.objects.filter(pk__in=[self.sub.pk, self.leaf.pk]).exists())
        self.assertFalse(Part.objects.filter(pk=part.pk).exists())
        self.assertTrue(Project.objects.filter(pk=self.root.pk).exists())
        self.assertTrue(PrintJob.objects.filter(pk=job.pk).exists())


class ProjectSerializerPresetRequiredTests(APITestCase):
    """The ProjectSerializer requires default_print_preset on top-level projects."""

    def test_create_project_without_preset_is_rejected(self) -> None:
        from core.api.serializers import ProjectSerializer

        serializer = ProjectSerializer(data={"name": "P", "description": ""})
        self.assertFalse(serializer.is_valid())
        self.assertIn("default_print_preset", serializer.errors)

    def test_create_project_with_preset_is_valid(self) -> None:
        from core.api.serializers import ProjectSerializer
        from core.models import OrcaPrintPreset

        preset = OrcaPrintPreset.objects.create(
            name="P", orca_name="P", state=OrcaPrintPreset.STATE_RESOLVED, instantiation=True
        )
        serializer = ProjectSerializer(data={"name": "P", "description": "", "default_print_preset": preset.pk})
        self.assertTrue(serializer.is_valid(), serializer.errors)
