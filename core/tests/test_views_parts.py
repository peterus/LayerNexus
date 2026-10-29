"""Tests for part-related views."""

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse

from core.models import OrcaPrintPreset, Part, PrintJob, PrintJobPart, Project
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

    def test_part_detail_has_no_build_target_or_preset_choice(self):
        self.part.stl_file = SimpleUploadedFile("m.stl", b"solid m")
        self.part.save()
        resp = self.client.get(reverse("core:part_detail", kwargs={"pk": self.part.pk}))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, reverse("core:add_part_to_job", args=[self.part.pk]))
        self.assertNotContains(resp, 'name="target_assembly"')
        self.assertNotContains(resp, 'name="print_preset"')
        for key in ("candidate_assemblies", "job_preset_auto", "job_preset_choices"):
            self.assertNotIn(key, resp.context)

    def test_part_detail_breadcrumb_shows_ancestors(self):
        sub = Project.objects.create(name="Axle", parent=self.project)
        part = Part.objects.create(name="Hub", project=sub)
        resp = self.client.get(reverse("core:part_detail", kwargs={"pk": part.pk}))
        self.assertEqual(resp.context["ancestors"], [self.project])
        self.assertContains(resp, reverse("core:project_detail", args=[self.project.pk]))
        self.assertContains(resp, reverse("core:project_detail", args=[sub.pk]))

    def test_draft_jobs_filtered_by_inherited_preset(self):
        preset_a = OrcaPrintPreset.objects.create(name="A", state=OrcaPrintPreset.STATE_RESOLVED, instantiation=True)
        preset_b = OrcaPrintPreset.objects.create(name="B", state=OrcaPrintPreset.STATE_RESOLVED, instantiation=True)
        self.project.default_print_preset = preset_a
        self.project.save()
        sub = Project.objects.create(name="Sub", parent=self.project)
        part = Part.objects.create(name="Inherits A", project=sub)
        other = Part.objects.create(name="Other", project=sub)
        job_a = PrintJob.objects.create(name="Job A", print_preset=preset_a)
        PrintJobPart.objects.create(print_job=job_a, part=other, quantity=1)
        job_b = PrintJob.objects.create(name="Job B", print_preset=preset_b)
        PrintJobPart.objects.create(print_job=job_b, part=other, quantity=1)
        resp = self.client.get(reverse("core:part_detail", kwargs={"pk": part.pk}))
        self.assertIn(job_a, resp.context["draft_jobs"])
        self.assertNotIn(job_b, resp.context["draft_jobs"])

    def test_part_create_get(self):
        resp = self.client.get(reverse("core:part_create", args=[self.project.pk]))
        self.assertEqual(resp.status_code, 200)

    def test_part_create_post(self):
        resp = self.client.post(
            reverse("core:part_create", args=[self.project.pk]),
            {"name": "New Part", "quantity": 2, "color": "blue", "material": "PETG"},
        )
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(Part.objects.filter(name="New Part").exists())

    def test_part_create_sets_project_and_quantity(self):
        resp = self.client.post(
            reverse("core:part_create", args=[self.project.pk]),
            {"name": "Five", "quantity": 5, "color": "blue", "material": "PETG"},
        )
        self.assertEqual(resp.status_code, 302)
        part = Part.objects.get(name="Five")
        self.assertEqual(part.project, self.project)
        self.assertEqual(part.quantity, 5)

    def test_part_create_other_project_404(self):
        resp = self.client.get(reverse("core:part_create", args=[self.other_project.pk]))
        self.assertEqual(resp.status_code, 200)

    def test_part_update_get(self):
        resp = self.client.get(reverse("core:part_update", args=[self.part.pk]))
        self.assertEqual(resp.status_code, 200)

    def test_part_update_breadcrumb_links_back_to_part(self):
        resp = self.client.get(reverse("core:part_update", args=[self.part.pk]))
        part_url = reverse("core:part_detail", args=[self.part.pk])
        self.assertContains(
            resp, f'<li class="breadcrumb-item"><a href="{part_url}">{self.part.name}</a></li>', html=True
        )

    def test_part_update_post(self):
        resp = self.client.post(
            reverse("core:part_update", args=[self.part.pk]),
            {
                "name": "Updated Part",
                "project": self.project.pk,
                "quantity": 5,
                "color": "green",
                "material": "PLA",
            },
        )
        self.assertEqual(resp.status_code, 302)
        self.part.refresh_from_db()
        self.assertEqual(self.part.name, "Updated Part")

    def test_part_update_quantity(self):
        resp = self.client.post(
            reverse("core:part_update", args=[self.part.pk]),
            {"name": self.part.name, "project": self.project.pk, "quantity": 7, "color": "red", "material": "PLA"},
        )
        self.assertEqual(resp.status_code, 302)
        self.part.refresh_from_db()
        self.assertEqual(self.part.quantity, 7)
        self.assertEqual(self.part.project, self.project)

    def test_part_delete_get(self):
        resp = self.client.get(reverse("core:part_delete", args=[self.part.pk]))
        self.assertEqual(resp.status_code, 200)

    def test_part_delete_post(self):
        pk = self.part.pk
        resp = self.client.post(reverse("core:part_delete", args=[pk]))
        self.assertEqual(resp.status_code, 302)
        self.assertFalse(Part.objects.filter(pk=pk).exists())

    def test_part_delete_redirects_to_project(self):
        resp = self.client.post(reverse("core:part_delete", args=[self.part.pk]))
        self.assertRedirects(resp, reverse("core:project_detail", args=[self.project.pk]))

    def test_nav_has_no_parts_library_link(self):
        resp = self.client.get(reverse("core:project_list"))
        self.assertNotContains(resp, "Parts Library")
        self.assertNotContains(resp, 'href="/parts/"')


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
