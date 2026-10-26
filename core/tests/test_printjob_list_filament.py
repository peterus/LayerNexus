"""Tests for the Filament column and filter on the print job list."""

from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from core.models import Part, PrintJob, PrintJobPart, SpoolmanFilamentMapping
from core.services.filament_display import filament_label, safe_hex
from core.tests.mixins import TestDataMixin


class FilamentDisplayHelperTests(TestCase):
    """Tests for the shared label / hex helpers."""

    def test_safe_hex_accepts_with_and_without_hash(self):
        self.assertEqual(safe_hex("ff0000"), "#ff0000")
        self.assertEqual(safe_hex("#00AaFF"), "#00AaFF")

    def test_safe_hex_rejects_invalid(self):
        for value in ("", "red", "#fff", "ff00zz", "#ff0000;background:url(x)", "ff00001", None):
            self.assertEqual(safe_hex(value), "", value)

    def test_filament_label_priority(self):
        self.assertEqual(filament_label(5, "Prusament Galaxy", "PLA"), "Prusament Galaxy")
        self.assertEqual(filament_label(5, "", "PLA"), "PLA")
        self.assertEqual(filament_label(5, "", ""), "Filament #5")


@override_settings(ALLOWED_HOSTS=["testserver"])
class PrintJobListFilamentTests(TestDataMixin, TestCase):
    """Filament column and multi-select filter."""

    def setUp(self):
        super().setUp()
        self.client.login(username="testuser", password="testpass123")
        self.url = reverse("core:printjob_list")
        SpoolmanFilamentMapping.objects.create(
            spoolman_filament_id=1, spoolman_filament_name="Galaxy Black PLA", spoolman_color_hex="112233"
        )
        SpoolmanFilamentMapping.objects.create(
            spoolman_filament_id=2, spoolman_filament_name="Signal Red PETG", spoolman_color_hex="ff0000"
        )
        self.part_a = Part.objects.create(name="PA", material="PLA", color="black", spoolman_filament_id=1)
        self.part_b = Part.objects.create(name="PB", material="PETG", color="red", spoolman_filament_id=2)
        self.part_none = Part.objects.create(name="PN", material="ABS", color="#abcdef")
        self.job_a = PrintJob.objects.create(name="JobA")
        self.job_b = PrintJob.objects.create(name="JobB")
        self.job_mixed = PrintJob.objects.create(name="JobMixed")
        self.job_none = PrintJob.objects.create(name="JobNone")
        PrintJobPart.objects.create(print_job=self.job_a, part=self.part_a)
        PrintJobPart.objects.create(print_job=self.job_b, part=self.part_b)
        PrintJobPart.objects.create(print_job=self.job_mixed, part=self.part_a)
        PrintJobPart.objects.create(print_job=self.job_mixed, part=self.part_b)
        PrintJobPart.objects.create(print_job=self.job_none, part=self.part_none)

    def _jobs(self, **params):
        resp = self.client.get(self.url, params)
        self.assertEqual(resp.status_code, 200)
        return list(resp.context["print_jobs"])

    def test_column_renders_name_and_color(self):
        resp = self.client.get(self.url)
        self.assertContains(resp, "Galaxy Black PLA")
        self.assertContains(resp, "Signal Red PETG")
        self.assertContains(resp, "--swatch: #112233")
        self.assertContains(resp, "--swatch: #ff0000")

    def test_multiple_filaments_multiple_badges(self):
        job = next(j for j in self._jobs() if j.pk == self.job_mixed.pk)
        self.assertEqual([b["label"] for b in job.filament_badges], ["Galaxy Black PLA", "Signal Red PETG"])

    def test_duplicate_filament_in_job_shown_once(self):
        other = Part.objects.create(name="PA2", material="PLA", spoolman_filament_id=1)
        PrintJobPart.objects.create(print_job=self.job_a, part=other)
        job = next(j for j in self._jobs() if j.pk == self.job_a.pk)
        self.assertEqual(len(job.filament_badges), 1)

    def test_unmapped_part_uses_material_and_part_color(self):
        job = next(j for j in self._jobs() if j.pk == self.job_none.pk)
        self.assertEqual(job.filament_badges, [{"label": "ABS", "hex": "#abcdef"}])

    def test_invalid_hex_not_rendered(self):
        mapping = SpoolmanFilamentMapping.objects.get(spoolman_filament_id=1)
        mapping.spoolman_color_hex = "red;background:url(x)"
        mapping.save()
        resp = self.client.get(self.url)
        self.assertNotContains(resp, "background:url")
        self.assertNotContains(resp, "--swatch: red")
        self.assertContains(resp, "Galaxy Black PLA")

    def test_filter_single(self):
        self.assertEqual({j.pk for j in self._jobs(filament="1")}, {self.job_a.pk, self.job_mixed.pk})

    def test_filter_multiple_is_or(self):
        pks = {j.pk for j in self._jobs(filament=["1", "2"])}
        self.assertEqual(pks, {self.job_a.pk, self.job_b.pk, self.job_mixed.pk})

    def test_filter_none_option(self):
        self.assertEqual([j.pk for j in self._jobs(filament="none")], [self.job_none.pk])

    def test_filter_none_excludes_jobs_without_parts(self):
        PrintJob.objects.create(name="EmptyDraft")
        self.assertEqual([j.pk for j in self._jobs(filament="none")], [self.job_none.pk])
        pks = {j.pk for j in self._jobs(filament=["none", "1"])}
        self.assertEqual(pks, {self.job_none.pk, self.job_a.pk, self.job_mixed.pk})

    def test_filter_combined_with_status(self):
        self.job_a.status = PrintJob.STATUS_COMPLETED
        self.job_a.save()
        self.assertEqual([j.pk for j in self._jobs(filament="1", status="completed")], [self.job_a.pk])

    def test_invalid_filament_ignored(self):
        resp = self.client.get(self.url, {"filament": ["abc", "9" * 30, "-1"]})
        self.assertEqual(len(resp.context["print_jobs"]), 4)
        self.assertFalse(resp.context["filters_active"])

    def test_options_only_filaments_in_jobs(self):
        SpoolmanFilamentMapping.objects.create(spoolman_filament_id=3, spoolman_filament_name="Unused")
        resp = self.client.get(self.url)
        values = [o["value"] for o in resp.context["filament_options"]]
        self.assertEqual(values, ["1", "2", "none"])
        self.assertNotContains(resp, "Unused")

    def test_filter_persisted_and_reset(self):
        self.client.get(self.url, {"filament": "2"})
        resp = self.client.get(self.url)
        self.assertRedirects(resp, f"{self.url}?filament=2", fetch_redirect_response=False)
        self.client.get(self.url, {"reset": "1"})
        self.assertEqual(self.client.get(self.url).status_code, 200)

    def test_saved_filament_no_longer_in_jobs_discarded(self):
        self.client.get(self.url, {"filament": ["1", "2"]})
        PrintJobPart.objects.filter(part=self.part_b).delete()
        resp = self.client.get(self.url)
        self.assertRedirects(resp, f"{self.url}?filament=1", fetch_redirect_response=False)

    def test_pagination_keeps_filament(self):
        for i in range(30):
            job = PrintJob.objects.create(name=f"Bulk{i}")
            PrintJobPart.objects.create(print_job=job, part=self.part_a)
        resp = self.client.get(self.url, {"filament": "1"})
        self.assertContains(resp, "page=2&amp;filament=1")

    def test_query_count_constant(self):
        with CaptureQueriesContext(connection) as small:
            self.client.get(self.url)
        for i in range(10):
            job = PrintJob.objects.create(name=f"Extra {i}")
            PrintJobPart.objects.create(print_job=job, part=self.part_a)
            PrintJobPart.objects.create(print_job=job, part=self.part_b)
        with CaptureQueriesContext(connection) as large:
            self.client.get(self.url)
        self.assertEqual(len(small), len(large))
