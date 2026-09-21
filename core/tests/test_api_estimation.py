"""Tests for the part estimate and project re-estimate API actions."""

from __future__ import annotations

from unittest import mock

from django.contrib.auth.models import Permission, User
from django.core.files.uploadedfile import SimpleUploadedFile
from rest_framework.authtoken.models import Token
from rest_framework.test import APITestCase

from core.models import OrcaPrintPreset, Part, Project, ProjectPart


def _make_preset() -> OrcaPrintPreset:
    """Create a minimal resolved, instantiable OrcaPrintPreset."""
    return OrcaPrintPreset.objects.create(
        name="Fast",
        state=OrcaPrintPreset.STATE_RESOLVED,
        instantiation=True,
    )


def _stl_file() -> SimpleUploadedFile:
    """Return a minimal fake STL upload."""
    return SimpleUploadedFile("part.stl", b"solid\nendsolid", content_type="model/stl")


class PartEstimateActionTests(APITestCase):
    """POST /api/v1/parts/{id}/estimate/ — single-part re-estimation."""

    def setUp(self) -> None:
        """Create a manage-capable user and a part with STL + preset."""
        self.user = User.objects.create_user("designer", password="x")
        self.user.user_permissions.add(Permission.objects.get(codename="can_manage_projects"))
        token = Token.objects.create(user=self.user)
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {token.key}")

        preset = _make_preset()
        self.part = Part.objects.create(name="Bracket", print_preset=preset)
        self.part.stl_file.save("bracket.stl", _stl_file(), save=True)

    def test_estimate_sets_pending_and_triggers_worker(self) -> None:
        """Action sets estimation_status to pending and calls _trigger_part_estimation."""
        with mock.patch("core.api.views._trigger_part_estimation") as triggered:
            resp = self.client.post(f"/api/v1/parts/{self.part.pk}/estimate/")
        self.assertEqual(resp.status_code, 202, resp.data)
        triggered.assert_called_once()
        called_part = triggered.call_args[0][0]
        self.assertEqual(called_part.pk, self.part.pk)

    def test_estimate_returns_serialized_part(self) -> None:
        """Response body contains the serialized part with id and name."""
        with mock.patch("core.api.views._trigger_part_estimation"):
            resp = self.client.post(f"/api/v1/parts/{self.part.pk}/estimate/")
        self.assertEqual(resp.status_code, 202)
        self.assertEqual(resp.data["id"], self.part.pk)
        self.assertEqual(resp.data["name"], "Bracket")

    def test_estimate_clears_existing_results(self) -> None:
        """Existing estimation data is cleared before re-triggering."""
        self.part.filament_used_grams = 12.5
        self.part.estimation_status = Part.ESTIMATION_SUCCESS
        self.part.save()

        with mock.patch("core.api.views._trigger_part_estimation"):
            self.client.post(f"/api/v1/parts/{self.part.pk}/estimate/")

        self.part.refresh_from_db()
        self.assertIsNone(self.part.filament_used_grams)
        self.assertEqual(self.part.estimation_status, Part.ESTIMATION_NONE)

    def test_estimate_forbidden_without_manage_perm(self) -> None:
        """A read-only user is rejected with 403."""
        reader = User.objects.create_user("reader", password="x")
        token = Token.objects.create(user=reader)
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {token.key}")
        resp = self.client.post(f"/api/v1/parts/{self.part.pk}/estimate/")
        self.assertEqual(resp.status_code, 403)


class ProjectReEstimateActionTests(APITestCase):
    """POST /api/v1/projects/{id}/re-estimate/ — bulk re-estimation for a project tree."""

    def setUp(self) -> None:
        """Create a manage-capable user, a project, and two parts (one eligible)."""
        self.user = User.objects.create_user("designer", password="x")
        self.user.user_permissions.add(Permission.objects.get(codename="can_manage_projects"))
        token = Token.objects.create(user=self.user)
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {token.key}")

        self.project = Project.objects.create(name="Assembly")
        preset = _make_preset()

        self.part_with_stl = Part.objects.create(name="WithSTL", print_preset=preset)
        self.part_with_stl.stl_file.save("with.stl", _stl_file(), save=True)
        ProjectPart.objects.create(project=self.project, part=self.part_with_stl, quantity=1)

        self.part_no_stl = Part.objects.create(name="NoSTL")
        ProjectPart.objects.create(project=self.project, part=self.part_no_stl, quantity=1)

    def test_reestimate_queues_eligible_parts(self) -> None:
        """Only parts with STL + preset are queued; response reports the count."""
        with mock.patch("core.api.views._trigger_part_estimation") as triggered:
            resp = self.client.post(f"/api/v1/projects/{self.project.pk}/re-estimate/")
        self.assertEqual(resp.status_code, 202, resp.data)
        self.assertEqual(resp.data["queued"], 1)
        triggered.assert_called_once()
        called_part = triggered.call_args[0][0]
        self.assertEqual(called_part.pk, self.part_with_stl.pk)

    def test_reestimate_clears_existing_results(self) -> None:
        """Existing estimation data is cleared for each eligible part."""
        self.part_with_stl.filament_used_grams = 7.0
        self.part_with_stl.estimation_status = Part.ESTIMATION_SUCCESS
        self.part_with_stl.save()

        with mock.patch("core.api.views._trigger_part_estimation"):
            self.client.post(f"/api/v1/projects/{self.project.pk}/re-estimate/")

        self.part_with_stl.refresh_from_db()
        self.assertIsNone(self.part_with_stl.filament_used_grams)
        self.assertEqual(self.part_with_stl.estimation_status, Part.ESTIMATION_NONE)

    def test_reestimate_zero_eligible_returns_queued_zero(self) -> None:
        """A project whose parts all lack STL returns queued=0 (still 202)."""
        project = Project.objects.create(name="Empty")
        no_stl = Part.objects.create(name="Bare")
        ProjectPart.objects.create(project=project, part=no_stl, quantity=1)

        with mock.patch("core.api.views._trigger_part_estimation") as triggered:
            resp = self.client.post(f"/api/v1/projects/{project.pk}/re-estimate/")
        self.assertEqual(resp.status_code, 202)
        self.assertEqual(resp.data["queued"], 0)
        triggered.assert_not_called()

    def test_reestimate_forbidden_without_manage_perm(self) -> None:
        """A read-only user is rejected with 403."""
        reader = User.objects.create_user("reader", password="x")
        token = Token.objects.create(user=reader)
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {token.key}")
        resp = self.client.post(f"/api/v1/projects/{self.project.pk}/re-estimate/")
        self.assertEqual(resp.status_code, 403)
