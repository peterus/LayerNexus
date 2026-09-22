"""Tests for print job views."""

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse

from core.models import OrcaPrintPreset, Part, PrintJob, PrintJobPart, PrintJobPlate, ProjectPart
from core.tests.mixins import TestDataMixin


@override_settings(ALLOWED_HOSTS=["testserver"])
class PrintJobViewTests(TestDataMixin, TestCase):
    """Tests for PrintJob views."""

    def setUp(self):
        super().setUp()
        self.client.login(username="testuser", password="testpass123")
        self.job = PrintJob.objects.create(
            name="Test Job",
            status="draft",
            created_by=self.user,
        )
        PrintJobPart.objects.create(print_job=self.job, part=self.part, quantity=1)

    def test_list_200(self):
        resp = self.client.get(reverse("core:printjob_list"))
        self.assertEqual(resp.status_code, 200)

    def test_create_get(self):
        resp = self.client.get(reverse("core:printjob_create"))
        self.assertEqual(resp.status_code, 200)

    def test_create_post(self):
        resp = self.client.post(
            reverse("core:printjob_create"),
            {
                "name": "New Draft Job",
            },
        )
        self.assertEqual(resp.status_code, 302)

    def test_detail_200(self):
        resp = self.client.get(reverse("core:printjob_detail", args=[self.job.pk]))
        self.assertEqual(resp.status_code, 200)

    def test_update_get(self):
        resp = self.client.get(reverse("core:printjob_update", args=[self.job.pk]))
        self.assertEqual(resp.status_code, 200)


class CreateJobsFromProjectViewTests(TestDataMixin, TestCase):
    """Tests for the bulk 'Create Print Job(s)' action on a project."""

    def setUp(self):
        super().setUp()
        self.client.login(username="testuser", password="testpass123")
        # Give the base part an STL file so it is eligible
        self.part.stl_file = SimpleUploadedFile("part1.stl", b"solid part1")
        self.part.save()

    def _url(self):
        return reverse("core:project_create_jobs", args=[self.project.pk])

    def test_creates_single_job_for_compatible_parts(self):
        """All parts with same preset/filament end up in one job."""
        part_two = Part.objects.create(
            name="Part Two",
            stl_file=SimpleUploadedFile("part2.stl", b"solid part2"),
        )
        ProjectPart.objects.create(project=self.project, part=part_two, quantity=2)
        resp = self.client.post(self._url())
        self.assertEqual(resp.status_code, 302)
        # One job created
        jobs = PrintJob.objects.filter(created_by=self.user)
        self.assertEqual(jobs.count(), 1)
        job = jobs.first()
        self.assertEqual(job.status, PrintJob.STATUS_DRAFT)
        # Both parts in the job
        self.assertEqual(job.job_parts.count(), 2)

    def test_creates_multiple_jobs_for_different_filaments(self):
        """Parts with different spoolman_filament_id create separate jobs."""
        self.part.spoolman_filament_id = 10
        self.part.save()

        part_two = Part.objects.create(
            name="Part Two",
            spoolman_filament_id=20,
            stl_file=SimpleUploadedFile("part2.stl", b"solid part2"),
        )
        ProjectPart.objects.create(project=self.project, part=part_two, quantity=1)
        resp = self.client.post(self._url())
        self.assertEqual(resp.status_code, 302)
        jobs = PrintJob.objects.filter(created_by=self.user)
        self.assertEqual(jobs.count(), 2)

    def test_skips_parts_without_stl(self):
        """Parts without an STL file are not added to any job."""
        no_stl = Part.objects.create(name="No STL Part")
        ProjectPart.objects.create(project=self.project, part=no_stl, quantity=1)
        resp = self.client.post(self._url())
        self.assertEqual(resp.status_code, 302)
        job = PrintJob.objects.filter(created_by=self.user).first()
        # Only the part with STL should be in the job
        self.assertEqual(job.job_parts.count(), 1)
        self.assertEqual(job.job_parts.first().part, self.part)

    def test_skips_fully_printed_parts(self):
        """Parts with a per-assembly remaining of 0 are skipped."""
        # Mark part as fully printed FOR this project via a completed, attributed job plate.
        # Edge quantity is 3 (from TestDataMixin).
        job = PrintJob.objects.create(
            name="Old Job",
            status=PrintJob.STATUS_DRAFT,
            created_by=self.user,
        )
        PrintJobPart.objects.create(print_job=job, part=self.part, quantity=3, target_assembly=self.project)
        PrintJobPlate.objects.create(print_job=job, plate_number=1, status="completed")

        resp = self.client.post(self._url())
        self.assertEqual(resp.status_code, 302)
        # No new draft job should be created (only the old one exists)
        new_jobs = PrintJob.objects.filter(created_by=self.user, status=PrintJob.STATUS_DRAFT)
        self.assertEqual(new_jobs.count(), 1)  # only the old one

    def test_uses_remaining_quantity(self):
        """Job quantity equals the per-assembly remaining (needed − printed)."""
        self.client.post(self._url())
        job = PrintJob.objects.filter(created_by=self.user).first()
        jp = job.job_parts.first()
        # Nothing printed yet; remaining == edge quantity (3 from TestDataMixin).
        self.assertEqual(jp.quantity, 3)

    def test_job_parts_attributed_to_project(self):
        """Created job parts carry target_assembly == the source project (Phase 6a)."""
        self.client.post(self._url())
        job = PrintJob.objects.filter(created_by=self.user).first()
        jp = job.job_parts.first()
        self.assertEqual(jp.target_assembly_id, self.project.pk)

    def test_uses_per_assembly_remaining(self):
        """Quantity is needed − printed-for-this-assembly, not the global remaining."""
        # 3 needed (edge quantity from TestDataMixin), 1 already printed FOR this project → 2 remaining.
        done = PrintJob.objects.create(status="completed", created_by=self.user)
        PrintJobPart.objects.create(print_job=done, part=self.part, quantity=1, target_assembly=self.project)
        PrintJobPlate.objects.create(print_job=done, plate_number=1, status="completed")

        self.client.post(self._url())
        new_job = (
            PrintJob.objects.filter(created_by=self.user, status=PrintJob.STATUS_DRAFT).order_by("-created_at").first()
        )
        jp = new_job.job_parts.get(part=self.part)
        self.assertEqual(jp.quantity, 2)
        self.assertEqual(jp.target_assembly_id, self.project.pk)

    def test_no_eligible_parts_shows_warning(self):
        """When no parts are eligible, a warning message is shown."""
        self.part.stl_file.delete(save=True)
        resp = self.client.post(self._url())
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(PrintJob.objects.filter(created_by=self.user).count(), 0)

    def test_redirects_to_job_when_single(self):
        """When only one job is created, redirect to its detail page."""
        resp = self.client.post(self._url())
        job = PrintJob.objects.filter(created_by=self.user).first()
        self.assertRedirects(resp, reverse("core:printjob_detail", args=[job.pk]))

    def test_redirects_to_project_when_multiple(self):
        """When multiple jobs are created, redirect to the project."""
        self.part.spoolman_filament_id = 10
        self.part.save()
        part_two = Part.objects.create(
            name="Part Two",
            spoolman_filament_id=20,
            stl_file=SimpleUploadedFile("part2.stl", b"solid part2"),
        )
        ProjectPart.objects.create(project=self.project, part=part_two, quantity=1)
        resp = self.client.post(self._url())
        self.assertRedirects(resp, reverse("core:project_detail", args=[self.project.pk]))

    def test_requires_permission(self):
        """Users without add_printjob permission get a 403."""
        self.client.login(username="otheruser", password="otherpass123")
        resp = self.client.post(self._url())
        self.assertEqual(resp.status_code, 403)


class CreateJobsResolvedPresetTests(TestDataMixin, TestCase):
    """The project-path job creation resolves and pins the project preset (Variant B)."""

    def setUp(self):
        super().setUp()
        self.client.login(username="testuser", password="testpass123")
        self.project_preset = OrcaPrintPreset.objects.create(
            name="ProjPreset", orca_name="ProjPreset", state=OrcaPrintPreset.STATE_RESOLVED, instantiation=True
        )
        self.project.default_print_preset = self.project_preset
        self.project.save(update_fields=["default_print_preset"])
        # self.part is a legacy part with NO own preset — must inherit via resolution.
        self.part.stl_file = SimpleUploadedFile("legacy.stl", b"solid legacy")
        self.part.save()

    def test_created_job_pins_resolved_project_preset(self):
        resp = self.client.post(reverse("core:project_create_jobs", args=[self.project.pk]))
        self.assertIn(resp.status_code, (302, 200))
        job = PrintJob.objects.latest("created_at")
        self.assertEqual(job.print_preset_id, self.project_preset.pk)


class AddPartToJobPresetTests(TestDataMixin, TestCase):
    """Part-path job creation resolves the preset, offering a dropdown when ambiguous."""

    def setUp(self):
        super().setUp()
        self.client.login(username="testuser", password="testpass123")

    def _preset(self, name):
        return OrcaPrintPreset.objects.create(
            name=name, orca_name=name, state=OrcaPrintPreset.STATE_RESOLVED, instantiation=True
        )

    def test_candidates_single_project_auto(self):
        from core.models import Part, Project, ProjectPart

        preset = self._preset("P")
        project = Project.objects.create(name="Proj", default_print_preset=preset)
        part = Part.objects.create(name="p")
        ProjectPart.objects.create(project=project, part=part, quantity=1)
        auto, choices = part.resolve_job_preset_candidates()
        self.assertEqual(auto, preset)
        self.assertEqual(choices, [])

    def test_candidates_multiple_different_offers_choices(self):
        from core.models import Part, Project, ProjectPart

        a = Project.objects.create(name="A", default_print_preset=self._preset("PA"))
        b = Project.objects.create(name="B", default_print_preset=self._preset("PB"))
        part = Part.objects.create(name="p")
        ProjectPart.objects.create(project=a, part=part, quantity=1)
        ProjectPart.objects.create(project=b, part=part, quantity=1)
        auto, choices = part.resolve_job_preset_candidates()
        self.assertIsNone(auto)
        self.assertEqual({c.name for c in choices}, {"PA", "PB"})

    def test_add_part_new_job_pins_chosen_preset(self):
        from core.models import Part, Project, ProjectPart

        pa = self._preset("PA")
        pb = self._preset("PB")
        a = Project.objects.create(name="A", default_print_preset=pa)
        b = Project.objects.create(name="B", default_print_preset=pb)
        part = Part.objects.create(name="p")
        part.stl_file.name = "stl_files/p.stl"
        part.save(update_fields=["stl_file"])
        ProjectPart.objects.create(project=a, part=part, quantity=1)
        ProjectPart.objects.create(project=b, part=part, quantity=1)

        resp = self.client.post(
            reverse("core:add_part_to_job", kwargs={"part_pk": part.pk}),
            {"job": "", "quantity": 1, "print_preset": pb.pk},
        )
        self.assertEqual(resp.status_code, 302)
        job = PrintJob.objects.latest("created_at")
        self.assertEqual(job.print_preset_id, pb.pk)

    def test_add_part_ambiguous_without_choice_is_rejected(self):
        from core.models import Part, Project, ProjectPart

        a = Project.objects.create(name="A", default_print_preset=self._preset("PA"))
        b = Project.objects.create(name="B", default_print_preset=self._preset("PB"))
        part = Part.objects.create(name="p")
        part.stl_file.name = "stl_files/p.stl"
        part.save(update_fields=["stl_file"])
        ProjectPart.objects.create(project=a, part=part, quantity=1)
        ProjectPart.objects.create(project=b, part=part, quantity=1)

        before = PrintJob.objects.count()
        resp = self.client.post(
            reverse("core:add_part_to_job", kwargs={"part_pk": part.pk}),
            {"job": "", "quantity": 1},  # no print_preset chosen
        )
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(PrintJob.objects.count(), before)  # no job created
