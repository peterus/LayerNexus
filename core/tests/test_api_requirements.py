"""Requirements + completeness (validate) read tests for the build API."""

from __future__ import annotations

from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from rest_framework.authtoken.models import Token
from rest_framework.test import APITestCase

from core.models import Part, Project, ProjectPart


class ApiRequirementsTests(APITestCase):
    """The ``requirements`` and ``validate`` actions expose aggregate + completeness data."""

    def setUp(self) -> None:
        """Authenticate a plain (read-only) user — both actions are reads."""
        self.user = User.objects.create_user("viewer", password="x")
        token = Token.objects.create(user=self.user)
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {token.key}")

    def test_requirements_aggregate_keys(self) -> None:
        """GET /projects/{id}/requirements/ returns the JSON-safe aggregate payload."""
        project = Project.objects.create(name="Assembly")
        gear = Part.objects.create(
            name="Gear",
            spoolman_filament_id=7,
            material="PLA",
            filament_used_grams=10.0,
            filament_used_meters=3.0,
            estimation_status=Part.ESTIMATION_SUCCESS,
        )
        ProjectPart.objects.create(project=project, part=gear, quantity=2)
        resp = self.client.get(f"/api/v1/projects/{project.pk}/requirements/")
        self.assertEqual(resp.status_code, 200, resp.data)
        data = resp.data
        self.assertEqual(data["total_parts_count"], 2)
        self.assertEqual(data["total_filament_grams"], 20.0)
        self.assertEqual(data["total_filament_meters"], 6.0)
        self.assertIn("total_hardware_cost", data)
        # Nested model objects must be sanitised to JSON-safe id/name dicts.
        self.assertEqual(len(data["filament_requirements"]), 1)
        fil = data["filament_requirements"][0]
        self.assertEqual(fil["filament_id"], 7)
        self.assertTrue(all(set(p) >= {"id", "name"} for p in fil["parts"]))
        self.assertIn("hardware_requirements", data)
        self.assertIn("percent", data["variant_progress"])
        self.assertTrue(all(set(row["part"]) >= {"id", "name"} for row in data["variant_progress"]["parts"]))

    def test_validate_flags_missing_stl(self) -> None:
        """A part without an STL is reported as an issue and the project is not ``ok``."""
        project = Project.objects.create(name="Assembly")
        part = Part.objects.create(name="Bracket", spoolman_filament_id=7)
        ProjectPart.objects.create(project=project, part=part, quantity=1)
        resp = self.client.get(f"/api/v1/projects/{project.pk}/validate/")
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertFalse(resp.data["ok"])
        issues = resp.data["issues"]
        self.assertTrue(any(i["part_id"] == part.pk and "stl" in i["issue"].lower() for i in issues))

    def test_validate_flags_missing_filament_and_error(self) -> None:
        """Missing spoolman id and estimation errors are both flagged."""
        project = Project.objects.create(name="Assembly")
        stl = SimpleUploadedFile("m.stl", b"solid m\nendsolid m\n")
        part = Part.objects.create(
            name="Panel",
            stl_file=stl,
            spoolman_filament_id=None,
            estimation_status=Part.ESTIMATION_ERROR,
        )
        ProjectPart.objects.create(project=project, part=part, quantity=1)
        resp = self.client.get(f"/api/v1/projects/{project.pk}/validate/")
        self.assertFalse(resp.data["ok"])
        my_issues = [i["issue"].lower() for i in resp.data["issues"] if i["part_id"] == part.pk]
        self.assertTrue(any("filament" in i for i in my_issues))
        self.assertTrue(any("error" in i or "estimation" in i for i in my_issues))

    def test_validate_flags_empty_project(self) -> None:
        """A project with no parts is flagged as not ``ok`` with an empty-project issue."""
        project = Project.objects.create(name="Nothing")
        resp = self.client.get(f"/api/v1/projects/{project.pk}/validate/")
        self.assertFalse(resp.data["ok"])
        self.assertTrue(any(i["part_id"] is None for i in resp.data["issues"]))

    def test_validate_ok_for_complete_project(self) -> None:
        """A fully-specified project validates as ``ok`` with no issues."""
        project = Project.objects.create(name="Complete")
        stl = SimpleUploadedFile("g.stl", b"solid g\nendsolid g\n")
        gear = Part.objects.create(
            name="Gear",
            stl_file=stl,
            spoolman_filament_id=7,
            filament_used_grams=5.0,
            estimation_status=Part.ESTIMATION_SUCCESS,
        )
        ProjectPart.objects.create(project=project, part=gear, quantity=1)
        resp = self.client.get(f"/api/v1/projects/{project.pk}/validate/")
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertTrue(resp.data["ok"], resp.data["issues"])
        self.assertEqual(resp.data["issues"], [])
