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


class JobDetailEffectivePresetTests(TestCase):
    def test_detail_uses_job_pinned_preset(self):
        from django.contrib.auth.models import User

        job_preset = OrcaPrintPreset.objects.create(
            name="JobPreset", orca_name="JobPreset", state=OrcaPrintPreset.STATE_RESOLVED, instantiation=True
        )
        part_preset = OrcaPrintPreset.objects.create(
            name="PartPreset", orca_name="PartPreset", state=OrcaPrintPreset.STATE_RESOLVED, instantiation=True
        )
        part = Part.objects.create(name="p", print_preset=part_preset)
        job = PrintJob.objects.create(name="J", print_preset=job_preset)
        PrintJobPart.objects.create(print_job=job, part=part, quantity=1)

        user = User.objects.create_user("viewer", password="pw")
        self.client.force_login(user)
        resp = self.client.get(reverse("core:printjob_detail", kwargs={"pk": job.pk}))
        self.assertEqual(resp.context["effective_print_preset"], job_preset)


class CreateJobsPerPathPresetTests(TestDataMixin, TestCase):
    """Project-path job creation splits a shared part across differing per-path presets."""

    def setUp(self):
        super().setUp()
        self.client.login(username="testuser", password="testpass123")

    def _preset(self, name):
        return OrcaPrintPreset.objects.create(
            name=name, orca_name=name, state=OrcaPrintPreset.STATE_RESOLVED, instantiation=True
        )

    def test_shared_part_split_into_two_preset_bundles(self):
        from core.models import Project, ProjectComponent

        cabin_preset = self._preset("CabinPreset")
        frame_preset = self._preset("FramePreset")
        top = Project.objects.create(
            name="Truck", default_print_preset=self._preset("TruckPreset"), created_by=self.user
        )
        cabin = Project.objects.create(name="Cabin", default_print_preset=cabin_preset, created_by=self.user)
        frame = Project.objects.create(name="Frame", default_print_preset=frame_preset, created_by=self.user)
        ProjectComponent.objects.create(parent_project=top, child_project=cabin, quantity=1)
        ProjectComponent.objects.create(parent_project=top, child_project=frame, quantity=1)
        bolt = Part.objects.create(name="bolt", stl_file=SimpleUploadedFile("bolt.stl", b"solid"))
        ProjectPart.objects.create(project=cabin, part=bolt, quantity=4)
        ProjectPart.objects.create(project=frame, part=bolt, quantity=10)

        resp = self.client.post(reverse("core:project_create_jobs", args=[top.pk]))
        self.assertEqual(resp.status_code, 302)
        jobs = PrintJob.objects.filter(created_by=self.user)
        # Two bundles: one per distinct resolved preset — not one collapsed to the last path.
        presets = sorted(j.print_preset.name for j in jobs)
        self.assertEqual(presets, ["CabinPreset", "FramePreset"])
        by_preset = {j.print_preset.name: j for j in jobs}
        self.assertEqual(by_preset["CabinPreset"].job_parts.get(part=bolt).quantity, 4)
        self.assertEqual(by_preset["FramePreset"].job_parts.get(part=bolt).quantity, 10)


class AddPartToJobCompatibilityTests(TestDataMixin, TestCase):
    """Add-to-job uses the resolved/pinned preset for compatibility and validation."""

    def setUp(self):
        super().setUp()
        self.client.login(username="testuser", password="testpass123")

    def _preset(self, name):
        return OrcaPrintPreset.objects.create(
            name=name, orca_name=name, state=OrcaPrintPreset.STATE_RESOLVED, instantiation=True
        )

    def _url(self, part):
        return reverse("core:add_part_to_job", kwargs={"part_pk": part.pk})

    def test_forged_preset_id_rejected(self):
        from core.models import Project

        a = Project.objects.create(name="A", default_print_preset=self._preset("PA"))
        b = Project.objects.create(name="B", default_print_preset=self._preset("PB"))
        part = Part.objects.create(name="p", stl_file=SimpleUploadedFile("p.stl", b"solid"))
        ProjectPart.objects.create(project=a, part=part, quantity=1)
        ProjectPart.objects.create(project=b, part=part, quantity=1)
        before = PrintJob.objects.count()
        # 999999 is not among the offered choices → rejected, no job.
        resp = self.client.post(self._url(part), {"job": "", "quantity": 1, "print_preset": 999999})
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(PrintJob.objects.count(), before)

    def test_incompatible_with_pinned_job_rejected(self):
        from core.models import Project

        pa = self._preset("PA")
        pb = self._preset("PB")
        proj_a = Project.objects.create(name="A", default_print_preset=pa)
        proj_b = Project.objects.create(name="B", default_print_preset=pb)
        part_a = Part.objects.create(name="pa", stl_file=SimpleUploadedFile("pa.stl", b"solid"))
        ProjectPart.objects.create(project=proj_a, part=part_a, quantity=1)
        part_b = Part.objects.create(name="pb", stl_file=SimpleUploadedFile("pb.stl", b"solid"))
        ProjectPart.objects.create(project=proj_b, part=part_b, quantity=1)

        job = PrintJob.objects.create(name="J", status=PrintJob.STATUS_DRAFT, created_by=self.user, print_preset=pa)
        PrintJobPart.objects.create(print_job=job, part=part_a, quantity=1)

        # part_b resolves to pb, job pinned to pa → incompatible.
        resp = self.client.post(self._url(part_b), {"job": job.pk, "quantity": 1})
        self.assertEqual(resp.status_code, 302)
        self.assertFalse(job.job_parts.filter(part=part_b).exists())

    def test_empty_draft_gets_pinned_on_add(self):
        from core.models import Project

        preset = self._preset("P")
        proj = Project.objects.create(name="P", default_print_preset=preset)
        part = Part.objects.create(name="p", stl_file=SimpleUploadedFile("p.stl", b"solid"))
        ProjectPart.objects.create(project=proj, part=part, quantity=1)
        # Blank draft created via the job form (no pinned preset).
        job = PrintJob.objects.create(name="Blank", status=PrintJob.STATUS_DRAFT, created_by=self.user)

        resp = self.client.post(self._url(part), {"job": job.pk, "quantity": 1})
        self.assertEqual(resp.status_code, 302)
        job.refresh_from_db()
        self.assertEqual(job.print_preset_id, preset.pk)
        self.assertTrue(job.job_parts.filter(part=part).exists())


class PartDetailDraftJobFilterTests(TestDataMixin, TestCase):
    """PartDetailView only offers draft jobs whose pinned preset the part can resolve to."""

    def setUp(self):
        super().setUp()
        self.client.login(username="testuser", password="testpass123")

    def _preset(self, name):
        return OrcaPrintPreset.objects.create(
            name=name, orca_name=name, state=OrcaPrintPreset.STATE_RESOLVED, instantiation=True
        )

    def test_only_matching_pinned_jobs_offered(self):
        from core.models import Project

        preset = self._preset("P")
        other = self._preset("Other")
        proj = Project.objects.create(name="P", default_print_preset=preset)
        part = Part.objects.create(name="p", stl_file=SimpleUploadedFile("p.stl", b"solid"))
        ProjectPart.objects.create(project=proj, part=part, quantity=1)

        match = PrintJob.objects.create(
            name="Match", status=PrintJob.STATUS_DRAFT, created_by=self.user, print_preset=preset
        )
        mismatch = PrintJob.objects.create(
            name="Mismatch", status=PrintJob.STATUS_DRAFT, created_by=self.user, print_preset=other
        )
        # Give each a part so neither is treated as the always-compatible empty job.
        other_part = Part.objects.create(name="x")
        PrintJobPart.objects.create(print_job=match, part=other_part, quantity=1)
        PrintJobPart.objects.create(print_job=mismatch, part=other_part, quantity=1)

        resp = self.client.get(reverse("core:part_detail", kwargs={"pk": part.pk}))
        offered = {j.pk for j in resp.context["draft_jobs"]}
        self.assertIn(match.pk, offered)
        self.assertNotIn(mismatch.pk, offered)


class CreateJobsPrintedByPresetTests(TestDataMixin, TestCase):
    """Prints made under one preset's job only deplete that preset's bundle (Copilot #54)."""

    def setUp(self):
        super().setUp()
        self.client.login(username="testuser", password="testpass123")

    def _preset(self, name):
        return OrcaPrintPreset.objects.create(
            name=name, orca_name=name, state=OrcaPrintPreset.STATE_RESOLVED, instantiation=True
        )

    def test_completed_prints_deplete_only_their_own_preset_bundle(self):
        from core.models import Project, ProjectComponent

        cabin_preset = self._preset("CabinPreset")
        frame_preset = self._preset("FramePreset")
        top = Project.objects.create(name="Truck", default_print_preset=self._preset("Truck"), created_by=self.user)
        cabin = Project.objects.create(name="Cabin", default_print_preset=cabin_preset, created_by=self.user)
        frame = Project.objects.create(name="Frame", default_print_preset=frame_preset, created_by=self.user)
        ProjectComponent.objects.create(parent_project=top, child_project=cabin, quantity=1)
        ProjectComponent.objects.create(parent_project=top, child_project=frame, quantity=1)
        bolt = Part.objects.create(name="bolt", stl_file=SimpleUploadedFile("bolt.stl", b"solid"))
        ProjectPart.objects.create(project=cabin, part=bolt, quantity=4)  # CabinPreset bundle
        ProjectPart.objects.create(project=frame, part=bolt, quantity=10)  # FramePreset bundle

        # 3 already printed under a FramePreset-pinned job attributed to Truck.
        done = PrintJob.objects.create(name="done", status="completed", created_by=self.user, print_preset=frame_preset)
        PrintJobPart.objects.create(print_job=done, part=bolt, quantity=3, target_assembly=top)
        PrintJobPlate.objects.create(print_job=done, plate_number=1, status="completed")

        resp = self.client.post(reverse("core:project_create_jobs", args=[top.pk]))
        self.assertEqual(resp.status_code, 302)
        jobs = {j.print_preset.name: j for j in PrintJob.objects.filter(status=PrintJob.STATUS_DRAFT)}
        # Cabin bundle stays at 4 (its prints were NOT touched); Frame bundle is 10-3=7.
        self.assertEqual(jobs["CabinPreset"].job_parts.get(part=bolt).quantity, 4)
        self.assertEqual(jobs["FramePreset"].job_parts.get(part=bolt).quantity, 7)


class AddPartToJobNoPresetRejectTests(TestDataMixin, TestCase):
    """Adding a part with no resolvable preset is rejected, not turned into a None-preset job."""

    def setUp(self):
        super().setUp()
        self.client.login(username="testuser", password="testpass123")

    def test_orphan_part_without_preset_rejected(self):
        # Part in NO project and no override → no resolvable preset.
        part = Part.objects.create(name="orphan", stl_file=SimpleUploadedFile("o.stl", b"solid"))
        before = PrintJob.objects.count()
        resp = self.client.post(
            reverse("core:add_part_to_job", kwargs={"part_pk": part.pk}),
            {"job": "", "quantity": 1},
        )
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(PrintJob.objects.count(), before)  # no unsliceable job created


class CreateJobsSkipNoPresetTests(TestDataMixin, TestCase):
    """Project-path job creation skips parts with no resolvable preset (Copilot #54 r4)."""

    def setUp(self):
        super().setUp()
        self.client.login(username="testuser", password="testpass123")

    def test_none_preset_part_is_skipped_not_bundled(self):
        from core.models import Project

        # Project with NO default preset (legacy/nullable) and a part with no override.
        project = Project.objects.create(name="Legacy", created_by=self.user)  # default_print_preset NULL
        part = Part.objects.create(name="p", stl_file=SimpleUploadedFile("p.stl", b"solid"))
        ProjectPart.objects.create(project=project, part=part, quantity=1)

        before = PrintJob.objects.count()
        resp = self.client.post(reverse("core:project_create_jobs", args=[project.pk]))
        self.assertEqual(resp.status_code, 302)
        # No None-preset job created.
        self.assertEqual(PrintJob.objects.count(), before)


class AddPartToJobAtomicPinTests(TestDataMixin, TestCase):
    """An empty draft already pinned to a different preset rejects a mismatched part (Copilot #54 r4)."""

    def setUp(self):
        super().setUp()
        self.client.login(username="testuser", password="testpass123")

    def _preset(self, name):
        return OrcaPrintPreset.objects.create(
            name=name, orca_name=name, state=OrcaPrintPreset.STATE_RESOLVED, instantiation=True
        )

    def test_empty_but_pinned_draft_rejects_mismatched_part(self):
        from core.models import Project

        pa = self._preset("PA")
        pb = self._preset("PB")
        proj = Project.objects.create(name="B", default_print_preset=pb)
        part = Part.objects.create(name="pb", stl_file=SimpleUploadedFile("pb.stl", b"solid"))
        ProjectPart.objects.create(project=proj, part=part, quantity=1)
        # Empty draft already pinned to PA (no parts yet).
        job = PrintJob.objects.create(
            name="Pinned", status=PrintJob.STATUS_DRAFT, created_by=self.user, print_preset=pa
        )

        resp = self.client.post(
            reverse("core:add_part_to_job", kwargs={"part_pk": part.pk}),
            {"job": job.pk, "quantity": 1},
        )
        self.assertEqual(resp.status_code, 302)
        self.assertFalse(job.job_parts.filter(part=part).exists())  # rejected, not added
