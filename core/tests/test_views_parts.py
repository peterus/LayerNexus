"""Tests for part-related views."""

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse

from core.models import (
    OrcaPrintPreset,
    Part,
    PrintedPartRecord,
    PrintJob,
    PrintJobPart,
    PrintJobPlatePart,
    Project,
)
from core.tests.mixins import TestDataMixin, complete_plate, make_sliced_job


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


@override_settings(ALLOWED_HOSTS=["testserver"])
class PrintedPartRecordViewTests(TestDataMixin, TestCase):
    """Manual printed-part bookings on the part detail page."""

    def setUp(self) -> None:
        super().setUp()
        self.client.login(username="testuser", password="testpass123")

    def _book(self, **data) -> object:
        payload = {"quantity": 2, "printed_on": "2026-10-30", "note": "outside"} | data
        return self.client.post(reverse("core:printed_record_create", args=[self.part.pk]), payload)

    def test_designer_can_add_record(self) -> None:
        response = self._book()
        self.assertRedirects(response, reverse("core:part_detail", args=[self.part.pk]))
        record = PrintedPartRecord.objects.get(part=self.part)
        self.assertEqual((record.quantity, str(record.printed_on), record.note), (2, "2026-10-30", "outside"))
        self.assertEqual(record.created_by, self.user)
        self.assertEqual(Part.objects.get(pk=self.part.pk).printed_quantity, 2)

    def test_quantity_zero_rejected(self) -> None:
        response = self._book(quantity=0)
        self.assertRedirects(response, reverse("core:part_detail", args=[self.part.pk]), fetch_redirect_response=False)
        self.assertFalse(PrintedPartRecord.objects.exists())
        response = self.client.get(reverse("core:part_detail", args=[self.part.pk]))
        self.assertContains(response, "Printed copies not booked")

    def test_future_date_rejected(self) -> None:
        from datetime import timedelta

        from django.utils import timezone

        tomorrow = timezone.localdate() + timedelta(days=1)
        self._book(printed_on=tomorrow.isoformat())
        self.assertFalse(PrintedPartRecord.objects.exists())

    def test_get_not_allowed(self) -> None:
        response = self.client.get(reverse("core:printed_record_create", args=[self.part.pk]))
        self.assertEqual(response.status_code, 405)

    def test_delete_record_reduces_printed_quantity(self) -> None:
        keep = PrintedPartRecord.objects.create(part=self.part, quantity=1)
        record = PrintedPartRecord.objects.create(part=self.part, quantity=4)
        self.assertEqual(Part.objects.get(pk=self.part.pk).printed_quantity, 5)
        response = self.client.post(reverse("core:printed_record_delete", args=[self.part.pk, record.pk]))
        self.assertRedirects(response, reverse("core:part_detail", args=[self.part.pk]))
        self.assertEqual(list(PrintedPartRecord.objects.all()), [keep])
        self.assertEqual(Part.objects.get(pk=self.part.pk).printed_quantity, 1)

    def test_delete_record_of_other_part_404(self) -> None:
        record = PrintedPartRecord.objects.create(part=self.other_part, quantity=1)
        response = self.client.post(reverse("core:printed_record_delete", args=[self.part.pk, record.pk]))
        self.assertEqual(response.status_code, 404)
        self.assertTrue(PrintedPartRecord.objects.filter(pk=record.pk).exists())

    def test_part_detail_shows_records_and_form(self) -> None:
        PrintedPartRecord.objects.create(part=self.part, quantity=3, note="spare set", created_by=self.user)
        response = self.client.get(reverse("core:part_detail", args=[self.part.pk]))
        self.assertContains(response, 'id="printed-records"')
        self.assertContains(response, "spare set")
        self.assertContains(response, 'id="printed-record-form"')
        self.assertContains(response, reverse("core:printed_record_create", args=[self.part.pk]))

    def test_part_detail_shows_auto_counted_plates(self) -> None:
        """The automatically counted plates are listed read-only next to the bookings."""
        job = make_sliced_job("Job 33", plates=2)
        PrintJobPart.objects.create(print_job=job, part=self.part, quantity=3)
        plate1, plate2 = job.plates.order_by("plate_number")
        for copy_index in (0, 1):
            PrintJobPlatePart.objects.create(plate=plate1, part=self.part, copy_index=copy_index)
        PrintJobPlatePart.objects.create(plate=plate2, part=self.part, copy_index=2)
        complete_plate(plate1)
        legacy = make_sliced_job("Legacy job", plates=1, plate_parts_known=False)
        PrintJobPart.objects.create(print_job=legacy, part=self.part, quantity=4)
        complete_plate(legacy.plates.get())
        PrintedPartRecord.objects.create(part=self.part, quantity=1)

        response = self.client.get(reverse("core:part_detail", args=[self.part.pk]))

        rows = response.context["printed_plates"]
        self.assertEqual(
            [(row["job"].pk, row["plate"] and row["plate"].plate_number, row["copies"]) for row in rows],
            [(legacy.pk, None, 4), (job.pk, 1, 2)],
        )
        self.assertContains(response, 'id="printed-plates"')
        self.assertContains(response, "Plate 1")
        self.assertContains(response, "all plates")
        self.assertContains(response, '<dd class="col-sm-7" id="printed-total">7</dd>', html=True)

    def test_part_detail_query_count_independent_of_history(self) -> None:
        """Printed quantity and the plate list come from one prefetch (no N+1 over jobs/plates)."""
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        def queries() -> int:
            with CaptureQueriesContext(connection) as captured:
                self.client.get(reverse("core:part_detail", args=[self.part.pk]))
            return len(captured)

        def add_job() -> None:
            job = make_sliced_job(plates=2)
            PrintJobPart.objects.create(print_job=job, part=self.part, quantity=2)
            for index, plate in enumerate(job.plates.all()):
                PrintJobPlatePart.objects.create(plate=plate, part=self.part, copy_index=index)
                complete_plate(plate)
            # With an author: the booking list shows it (guards a per-row user query).
            PrintedPartRecord.objects.create(part=self.part, quantity=1, created_by=self.user)

        add_job()
        baseline = queries()
        for _ in range(3):
            add_job()
        self.assertEqual(queries(), baseline)

    def test_part_with_records_not_deletable(self) -> None:
        PrintedPartRecord.objects.create(part=self.part, quantity=1)
        response = self.client.get(reverse("core:part_delete", args=[self.part.pk]))
        self.assertContains(response, 'id="printed-records-block"')
        self.assertNotContains(response, "Delete Part</button>")

        response = self.client.post(reverse("core:part_delete", args=[self.part.pk]))
        self.assertRedirects(response, reverse("core:part_delete", args=[self.part.pk]))
        self.assertTrue(Part.objects.filter(pk=self.part.pk).exists())

    def test_project_with_booked_part_not_deletable(self) -> None:
        PrintedPartRecord.objects.create(part=self.part, quantity=1)
        self.assertTrue(self.part.has_print_history())
        self.assertEqual(list(self.project.history_blocking_parts()), [self.part])
        response = self.client.post(reverse("core:project_delete", args=[self.project.pk]))
        self.assertRedirects(response, reverse("core:project_delete", args=[self.project.pk]))
        self.assertTrue(Project.objects.filter(pk=self.project.pk).exists())


class PrintedPlatesTests(TestCase):
    """``Part.printed_plates()`` lists copies so that they add up to the counted quantity."""

    def test_multi_mesh_copy_split_over_plates_listed_once(self) -> None:
        project = Project.objects.create(name="P")
        part = Part.objects.create(name="Multi", project=project, quantity=2)
        job = make_sliced_job(plates=2)
        PrintJobPart.objects.create(print_job=job, part=part, quantity=2)
        plate1, plate2 = job.plates.order_by("plate_number")
        # Copy 0 split over both plates, copy 1 completely on plate 1.
        PrintJobPlatePart.objects.create(plate=plate1, part=part, copy_index=0, meshes_on_plate=1, mesh_count=2)
        PrintJobPlatePart.objects.create(plate=plate2, part=part, copy_index=0, meshes_on_plate=1, mesh_count=2)
        PrintJobPlatePart.objects.create(plate=plate1, part=part, copy_index=1, meshes_on_plate=2, mesh_count=2)
        complete_plate(plate1)

        rows = {row["plate"].plate_number: (row["copies"], row["partial"]) for row in part.printed_plates()}
        self.assertEqual(rows, {1: (1, 1)})

        complete_plate(plate2)
        rows = {row["plate"].plate_number: (row["copies"], row["partial"]) for row in part.printed_plates()}
        self.assertEqual(rows, {1: (1, 0), 2: (1, 0)})
        self.assertEqual(sum(copies for copies, _partial in rows.values()), part.printed_quantity)

    def test_split_copy_listed_on_plate_that_passed_last(self) -> None:
        """Attribution follows completion time, not plate number (plate 2 passed first)."""
        from datetime import timedelta

        from django.utils import timezone

        project = Project.objects.create(name="P")
        part = Part.objects.create(name="Multi", project=project, quantity=1)
        job = make_sliced_job(plates=2)
        PrintJobPart.objects.create(print_job=job, part=part, quantity=1)
        plate1, plate2 = job.plates.order_by("plate_number")
        PrintJobPlatePart.objects.create(plate=plate1, part=part, copy_index=0, meshes_on_plate=1, mesh_count=2)
        PrintJobPlatePart.objects.create(plate=plate2, part=part, copy_index=0, meshes_on_plate=1, mesh_count=2)
        now = timezone.now()
        complete_plate(plate2, when=now - timedelta(hours=2))
        complete_plate(plate1, when=now)

        expected = {1: (1, 0)}
        rows = {row["plate"].plate_number: (row["copies"], row["partial"]) for row in part.printed_plates()}
        self.assertEqual(rows, expected)
        prefetched = Part.objects.prefetch_related(
            *Part.printed_quantity_prefetch_lookups(), "job_entries__print_job__plates__queue_entries"
        ).get(pk=part.pk)
        rows = {row["plate"].plate_number: (row["copies"], row["partial"]) for row in prefetched.printed_plates()}
        self.assertEqual(rows, expected)
