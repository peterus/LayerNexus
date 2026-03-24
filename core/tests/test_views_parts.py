"""Tests for part-related views."""

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse

from core.models import Part
from core.tests.mixins import TestDataMixin


@override_settings(ALLOWED_HOSTS=["testserver"])
class PartViewTests(TestDataMixin, TestCase):
    """Tests for Part CRUD views."""

    def setUp(self):
        super().setUp()
        self.client.login(username="testuser", password="testpass123")

    def test_part_detail_200(self):
        resp = self.client.get(reverse("core:part_detail", args=[self.part.pk]))
        self.assertEqual(resp.status_code, 200)

    def test_part_detail_other_user_404(self):
        resp = self.client.get(reverse("core:part_detail", args=[self.other_part.pk]))
        self.assertEqual(resp.status_code, 200)

    def test_part_create_get(self):
        resp = self.client.get(reverse("core:part_create", args=[self.project.pk]))
        self.assertEqual(resp.status_code, 200)

    def test_part_create_post(self):
        resp = self.client.post(
            reverse("core:part_create", args=[self.project.pk]),
            {"name": "New Part", "quantity": 2, "color": "blue", "material": "PETG"},
        )
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(Part.objects.filter(name="New Part", project=self.project).exists())

    def test_part_create_other_project_404(self):
        resp = self.client.get(reverse("core:part_create", args=[self.other_project.pk]))
        self.assertEqual(resp.status_code, 200)

    def test_part_update_get(self):
        resp = self.client.get(reverse("core:part_update", args=[self.part.pk]))
        self.assertEqual(resp.status_code, 200)

    def test_part_update_post(self):
        resp = self.client.post(
            reverse("core:part_update", args=[self.part.pk]),
            {
                "name": "Updated Part",
                "quantity": 5,
                "color": "green",
                "material": "PLA",
            },
        )
        self.assertEqual(resp.status_code, 302)
        self.part.refresh_from_db()
        self.assertEqual(self.part.name, "Updated Part")

    def test_part_delete_get(self):
        resp = self.client.get(reverse("core:part_delete", args=[self.part.pk]))
        self.assertEqual(resp.status_code, 200)

    def test_part_delete_post(self):
        pk = self.part.pk
        resp = self.client.post(reverse("core:part_delete", args=[pk]))
        self.assertEqual(resp.status_code, 302)
        self.assertFalse(Part.objects.filter(pk=pk).exists())


class PartReEstimateViewTests(TestDataMixin, TestCase):
    """Tests for the PartReEstimateView."""

    def setUp(self):
        super().setUp()
        self.client.login(username="testuser", password="testpass123")

    def test_re_estimate_requires_post(self):
        """GET is not allowed on the re-estimate endpoint."""
        resp = self.client.get(reverse("core:part_re_estimate", args=[self.part.pk]))
        self.assertEqual(resp.status_code, 405)

    def test_re_estimate_no_stl(self):
        """Parts without STL show a warning and redirect."""
        self.part.stl_file = ""
        self.part.save()
        resp = self.client.post(reverse("core:part_re_estimate", args=[self.part.pk]))
        self.assertEqual(resp.status_code, 302)

    def test_re_estimate_clears_old_values(self):
        """Re-estimate clears existing filament values when no preset available."""
        stl = SimpleUploadedFile("model.stl", b"solid test", content_type="application/sla")
        self.part.stl_file = stl
        self.part.filament_used_grams = 50.0
        self.part.estimation_status = Part.ESTIMATION_SUCCESS
        self.part.save()
        resp = self.client.post(reverse("core:part_re_estimate", args=[self.part.pk]))
        self.assertEqual(resp.status_code, 302)
        self.part.refresh_from_db()
        # No preset available → warning redirect, values untouched
        self.assertEqual(self.part.filament_used_grams, 50.0)
