"""Tests for print job views."""

from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection
from django.test import Client, TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from core.models import (
    OrcaMachineProfile,
    OrcaPrintPreset,
    Part,
    PrinterProfile,
    PrintJob,
    PrintJobPart,
    PrintJobPlate,
    Project,
)
from core.tests.mixins import TestDataMixin
from core.views.print_jobs import PrintJobListView


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

    def test_list_filter_bar_has_overlay_class(self):
        """The filter bar opts out of the card hover-lift so its dropdowns overlay the table."""
        resp = self.client.get(reverse("core:printjob_list"))
        self.assertContains(resp, "card card-body filter-bar")

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

    def test_job_detail_shows_root_path(self):
        """The job detail Project column shows the root-down path of the part's project."""
        root = Project.objects.create(name="R", created_by=self.user)
        sub = Project.objects.create(name="S", parent=root, quantity=1, created_by=self.user)
        part = Part.objects.create(project=sub, quantity=1, name="Sub Part")
        PrintJobPart.objects.create(print_job=self.job, part=part, quantity=1)
        resp = self.client.get(reverse("core:printjob_detail", args=[self.job.pk]))
        self.assertContains(resp, "R › S")
        self.assertContains(resp, reverse("core:project_detail", args=[sub.pk]))

    def test_job_detail_project_paths_no_query_per_part(self):
        """Rendering path labels does not cost a query per job part."""
        root = Project.objects.create(name="R", created_by=self.user)
        mid = Project.objects.create(name="M", parent=root, created_by=self.user)
        leaf = Project.objects.create(name="L", parent=mid, created_by=self.user)
        url = reverse("core:printjob_detail", args=[self.job.pk])

        def count() -> int:
            with CaptureQueriesContext(connection) as ctx:
                self.client.get(url)
            return len(ctx.captured_queries)

        PrintJobPart.objects.create(print_job=self.job, part=Part.objects.create(project=leaf, name="a"), quantity=1)
        few = count()
        for i in range(4):
            part = Part.objects.create(project=leaf, name=f"b{i}")
            PrintJobPart.objects.create(print_job=self.job, part=part, quantity=1)
        self.assertEqual(count(), few)


class CreateJobsFromProjectViewTests(TestDataMixin, TestCase):
    """Tests for the bulk 'Create Print Job(s)' action on a project."""

    def setUp(self):
        super().setUp()
        self.client.login(username="testuser", password="testpass123")
        # The project needs a default preset (now mandatory) so its parts resolve to a
        # preset and are not skipped as unsliceable.
        self.project.default_print_preset = OrcaPrintPreset.objects.create(
            name="Proj", orca_name="Proj", state=OrcaPrintPreset.STATE_RESOLVED, instantiation=True
        )
        self.project.save(update_fields=["default_print_preset"])
        # Give the base part an STL file so it is eligible
        self.part.stl_file = SimpleUploadedFile("part1.stl", b"solid part1")
        self.part.save()

    def _url(self):
        return reverse("core:project_create_jobs", args=[self.project.pk])

    def test_create_jobs_on_subproject_uses_tree_wide_need(self):
        """Started on a sub-project, the need is scaled up to the root (prints are tree-wide)."""
        sub = Project.objects.create(name="Axle", parent=self.project, quantity=2)
        part = Part.objects.create(
            project=sub, quantity=3, name="Hub", stl_file=SimpleUploadedFile("hub.stl", b"solid hub")
        )
        done = PrintJob.objects.create(status=PrintJob.STATUS_COMPLETED)
        PrintJobPart.objects.create(print_job=done, part=part, quantity=3)
        PrintJobPlate.objects.create(print_job=done, plate_number=1, status=PrintJobPlate.STATUS_COMPLETED)

        self.client.post(reverse("core:project_create_jobs", args=[sub.pk]))

        entry = PrintJobPart.objects.exclude(print_job=done).get(part=part)
        self.assertEqual(entry.quantity, 3)  # needs 2 × 3 = 6, 3 already printed

    def test_creates_single_job_for_compatible_parts(self):
        """All parts with same preset/filament end up in one job."""
        Part.objects.create(
            project=self.project,
            quantity=2,
            name="Part Two",
            stl_file=SimpleUploadedFile("part2.stl", b"solid part2"),
        )
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

        Part.objects.create(
            project=self.project,
            quantity=1,
            name="Part Two",
            spoolman_filament_id=20,
            stl_file=SimpleUploadedFile("part2.stl", b"solid part2"),
        )
        resp = self.client.post(self._url())
        self.assertEqual(resp.status_code, 302)
        jobs = PrintJob.objects.filter(created_by=self.user)
        self.assertEqual(jobs.count(), 2)

    def test_skips_parts_without_stl(self):
        """Parts without an STL file are not added to any job."""
        Part.objects.create(project=self.project, quantity=1, name="No STL Part")
        resp = self.client.post(self._url())
        self.assertEqual(resp.status_code, 302)
        job = PrintJob.objects.filter(created_by=self.user).first()
        # Only the part with STL should be in the job
        self.assertEqual(job.job_parts.count(), 1)
        self.assertEqual(job.job_parts.first().part, self.part)

    def test_skips_fully_printed_parts(self):
        """Parts with a remaining of 0 (needed − printed) are skipped."""
        # Mark part as fully printed via a completed job plate. Part quantity is 3 (TestDataMixin).
        job = PrintJob.objects.create(
            name="Old Job",
            status=PrintJob.STATUS_DRAFT,
            created_by=self.user,
        )
        PrintJobPart.objects.create(print_job=job, part=self.part, quantity=3)
        PrintJobPlate.objects.create(print_job=job, plate_number=1, status="completed")

        resp = self.client.post(self._url())
        self.assertEqual(resp.status_code, 302)
        # No new draft job should be created (only the old one exists)
        new_jobs = PrintJob.objects.filter(created_by=self.user, status=PrintJob.STATUS_DRAFT)
        self.assertEqual(new_jobs.count(), 1)  # only the old one

    def test_uses_remaining_quantity(self):
        """Job quantity equals the remaining (needed − printed)."""
        self.client.post(self._url())
        job = PrintJob.objects.filter(created_by=self.user).first()
        jp = job.job_parts.first()
        # Nothing printed yet; remaining == part quantity (3 from TestDataMixin).
        self.assertEqual(jp.quantity, 3)

    def test_create_jobs_subtracts_global_printed(self):
        """Quantity is needed − printed across all completed jobs (no attribution)."""
        # 3 needed (part quantity from TestDataMixin), 1 already printed in any completed job → 2 remaining.
        done = PrintJob.objects.create(status="completed", created_by=self.user)
        PrintJobPart.objects.create(print_job=done, part=self.part, quantity=1)
        PrintJobPlate.objects.create(print_job=done, plate_number=1, status="completed")

        self.client.post(self._url())
        new_job = (
            PrintJob.objects.filter(created_by=self.user, status=PrintJob.STATUS_DRAFT).order_by("-created_at").first()
        )
        jp = new_job.job_parts.get(part=self.part)
        self.assertEqual(jp.quantity, 2)
        self.assertFalse(hasattr(jp, "target_assembly"))

    def test_create_jobs_groups_by_inherited_preset_and_filament(self):
        """Parts in root R and in sub S (own preset) land in two jobs, each pinned to its preset."""
        preset_r = self.project.default_print_preset
        preset_s = OrcaPrintPreset.objects.create(
            name="Sub", orca_name="Sub", state=OrcaPrintPreset.STATE_RESOLVED, instantiation=True
        )
        sub = Project.objects.create(
            name="S", parent=self.project, quantity=2, default_print_preset=preset_s, created_by=self.user
        )
        sub_part = Part.objects.create(
            project=sub, quantity=2, name="Sub Part", stl_file=SimpleUploadedFile("sub.stl", b"solid sub")
        )

        resp = self.client.post(self._url())
        self.assertRedirects(resp, reverse("core:project_detail", args=[self.project.pk]))
        jobs = PrintJob.objects.filter(created_by=self.user)
        self.assertEqual(jobs.count(), 2)
        by_preset = {j.print_preset_id: j for j in jobs}
        self.assertEqual(set(by_preset), {preset_r.pk, preset_s.pk})
        self.assertEqual(by_preset[preset_r.pk].job_parts.get().part, self.part)
        self.assertEqual(by_preset[preset_r.pk].job_parts.get().quantity, 3)
        # needed = part quantity 2 × sub-project quantity 2.
        self.assertEqual(by_preset[preset_s.pk].job_parts.get().part, sub_part)
        self.assertEqual(by_preset[preset_s.pk].job_parts.get().quantity, 4)

    def test_create_jobs_skips_part_without_resolvable_preset(self):
        """A part with no own preset and no default up the chain is skipped, others still bundle."""
        self.project.default_print_preset = None
        self.project.save(update_fields=["default_print_preset"])
        own = OrcaPrintPreset.objects.create(
            name="Own", orca_name="Own", state=OrcaPrintPreset.STATE_RESOLVED, instantiation=True
        )
        with_preset = Part.objects.create(
            project=self.project,
            quantity=1,
            name="Own Preset",
            print_preset=own,
            stl_file=SimpleUploadedFile("own.stl", b"solid own"),
        )

        resp = self.client.post(self._url())
        self.assertEqual(resp.status_code, 302)
        job = PrintJob.objects.get(created_by=self.user)
        self.assertEqual(job.print_preset_id, own.pk)
        self.assertEqual([jp.part for jp in job.job_parts.all()], [with_preset])
        self.assertFalse(job.job_parts.filter(part=self.part).exists())

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
        Part.objects.create(
            project=self.project,
            quantity=1,
            name="Part Two",
            spoolman_filament_id=20,
            stl_file=SimpleUploadedFile("part2.stl", b"solid part2"),
        )
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
    """Part-path job creation pins the part's bottom-up resolved preset."""

    def setUp(self):
        super().setUp()
        self.client.login(username="testuser", password="testpass123")

    def _preset(self, name):
        return OrcaPrintPreset.objects.create(
            name=name, orca_name=name, state=OrcaPrintPreset.STATE_RESOLVED, instantiation=True
        )

    def test_add_part_to_job_uses_inherited_preset(self):
        """A part without own preset in sub S inherits the root's default preset."""
        preset = self._preset("RootPreset")
        root = Project.objects.create(name="R", default_print_preset=preset, created_by=self.user)
        sub = Project.objects.create(name="S", parent=root, quantity=1, created_by=self.user)
        part = Part.objects.create(project=sub, quantity=1, name="p", stl_file=SimpleUploadedFile("p.stl", b"solid"))

        resp = self.client.post(
            reverse("core:add_part_to_job", kwargs={"part_pk": part.pk}),
            {"job": "", "quantity": 1},
        )
        job = PrintJob.objects.latest("created_at")
        self.assertRedirects(resp, reverse("core:printjob_detail", args=[job.pk]))
        self.assertEqual(job.print_preset_id, preset.pk)
        self.assertTrue(job.job_parts.filter(part=part).exists())

    def test_add_part_to_job_ignores_target_assembly_param(self):
        """A stale ``target_assembly`` POST param is ignored and the add still works."""
        preset = self._preset("P")
        project = Project.objects.create(name="Proj", default_print_preset=preset, created_by=self.user)
        part = Part.objects.create(
            project=project, quantity=1, name="p", stl_file=SimpleUploadedFile("p.stl", b"solid")
        )

        resp = self.client.post(
            reverse("core:add_part_to_job", kwargs={"part_pk": part.pk}),
            {"job": "", "quantity": 2, "target_assembly": 1},
        )
        self.assertEqual(resp.status_code, 302)
        job = PrintJob.objects.latest("created_at")
        jp = job.job_parts.get(part=part)
        self.assertEqual(jp.quantity, 2)
        self.assertEqual(job.print_preset_id, preset.pk)
        self.assertFalse(hasattr(jp, "target_assembly"))


class JobDetailEffectivePresetTests(TestCase):
    def test_detail_uses_job_pinned_preset(self):
        from django.contrib.auth.models import User

        job_preset = OrcaPrintPreset.objects.create(
            name="JobPreset", orca_name="JobPreset", state=OrcaPrintPreset.STATE_RESOLVED, instantiation=True
        )
        part_preset = OrcaPrintPreset.objects.create(
            name="PartPreset", orca_name="PartPreset", state=OrcaPrintPreset.STATE_RESOLVED, instantiation=True
        )
        project = Project.objects.create(name="Proj")
        part = Part.objects.create(project=project, quantity=1, name="p", print_preset=part_preset)
        job = PrintJob.objects.create(name="J", print_preset=job_preset)
        PrintJobPart.objects.create(print_job=job, part=part, quantity=1)

        user = User.objects.create_user("viewer", password="pw")
        self.client.force_login(user)
        resp = self.client.get(reverse("core:printjob_detail", kwargs={"pk": job.pk}))
        self.assertEqual(resp.context["effective_print_preset"], job_preset)


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

    def test_incompatible_with_pinned_job_rejected(self):
        from core.models import Project

        pa = self._preset("PA")
        pb = self._preset("PB")
        proj_a = Project.objects.create(name="A", default_print_preset=pa)
        proj_b = Project.objects.create(name="B", default_print_preset=pb)
        part_a = Part.objects.create(
            project=proj_a, quantity=1, name="pa", stl_file=SimpleUploadedFile("pa.stl", b"solid")
        )
        part_b = Part.objects.create(
            project=proj_b, quantity=1, name="pb", stl_file=SimpleUploadedFile("pb.stl", b"solid")
        )

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
        part = Part.objects.create(project=proj, quantity=1, name="p", stl_file=SimpleUploadedFile("p.stl", b"solid"))
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
        part = Part.objects.create(project=proj, quantity=1, name="p", stl_file=SimpleUploadedFile("p.stl", b"solid"))

        match = PrintJob.objects.create(
            name="Match", status=PrintJob.STATUS_DRAFT, created_by=self.user, print_preset=preset
        )
        mismatch = PrintJob.objects.create(
            name="Mismatch", status=PrintJob.STATUS_DRAFT, created_by=self.user, print_preset=other
        )
        # Give each a part so neither is treated as the always-compatible empty job.
        other_part = Part.objects.create(project=proj, quantity=1, name="x")
        PrintJobPart.objects.create(print_job=match, part=other_part, quantity=1)
        PrintJobPart.objects.create(print_job=mismatch, part=other_part, quantity=1)

        resp = self.client.get(reverse("core:part_detail", kwargs={"pk": part.pk}))
        offered = {j.pk for j in resp.context["draft_jobs"]}
        self.assertIn(match.pk, offered)
        self.assertNotIn(mismatch.pk, offered)


class AddPartToJobNoPresetRejectTests(TestDataMixin, TestCase):
    """Adding a part with no resolvable preset is rejected, not turned into a None-preset job."""

    def setUp(self):
        super().setUp()
        self.client.login(username="testuser", password="testpass123")

    def test_orphan_part_without_preset_rejected(self):
        # Part in a project without default preset (none up the chain) and no override.
        project = Project.objects.create(name="NoPreset", created_by=self.user)
        part = Part.objects.create(
            project=project, quantity=1, name="orphan", stl_file=SimpleUploadedFile("o.stl", b"solid")
        )
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
        Part.objects.create(project=project, quantity=1, name="p", stl_file=SimpleUploadedFile("p.stl", b"solid"))

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
        part = Part.objects.create(project=proj, quantity=1, name="pb", stl_file=SimpleUploadedFile("pb.stl", b"solid"))
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


@override_settings(ALLOWED_HOSTS=["testserver"])
class PrintJobListFilterTests(TestDataMixin, TestCase):
    """Tests for filtering, ordering and pagination of the print job list."""

    def setUp(self):
        super().setUp()
        self.client.login(username="testuser", password="testpass123")
        self.url = reverse("core:printjob_list")
        self.job_a = PrintJob.objects.create(name="Alpha", status=PrintJob.STATUS_DRAFT, printer=self.printer)
        self.job_b = PrintJob.objects.create(name="Beta", status=PrintJob.STATUS_COMPLETED)
        PrintJobPart.objects.create(print_job=self.job_a, part=self.part, quantity=1)
        PrintJobPart.objects.create(print_job=self.job_b, part=self.other_part, quantity=2)

    def _jobs(self, **params):
        resp = self.client.get(self.url, params)
        self.assertEqual(resp.status_code, 200)
        return list(resp.context["print_jobs"])

    def test_newest_first(self):
        self.assertEqual(self._jobs(), [self.job_b, self.job_a])

    def test_filter_status(self):
        self.assertEqual(self._jobs(status="completed"), [self.job_b])

    def test_invalid_status_ignored(self):
        self.assertEqual(len(self._jobs(status="bogus")), 2)

    def test_filter_printer(self):
        self.assertEqual(self._jobs(printer=self.printer.pk), [self.job_a])

    def test_non_numeric_printer_ignored(self):
        resp = self.client.get(self.url, {"printer": "abc"})
        self.assertEqual(len(resp.context["print_jobs"]), 2)
        self.assertFalse(resp.context["filters_active"])

    def test_filter_project(self):
        self.assertEqual(self._jobs(project=self.project.pk), [self.job_a])

    def test_search_job_name(self):
        self.assertEqual(self._jobs(q="alph"), [self.job_a])

    def test_search_part_name_no_duplicates(self):
        PrintJobPart.objects.create(print_job=self.job_a, part=self.other_part, quantity=1)
        self.assertEqual(self._jobs(q="part"), [self.job_b, self.job_a])

    def test_combined_filters(self):
        self.assertEqual(self._jobs(status="draft", printer=self.printer.pk, q="alpha"), [self.job_a])
        self.assertEqual(self._jobs(status="completed", printer=self.printer.pk), [])

    def test_empty_result_message(self):
        resp = self.client.get(self.url, {"q": "nomatch"})
        self.assertContains(resp, "No print jobs match the current filters.")
        self.assertContains(resp, "Reset")

    def test_pagination_keeps_filters(self):
        for i in range(30):
            PrintJob.objects.create(name=f"Bulk {i}", status=PrintJob.STATUS_FAILED)
        resp = self.client.get(self.url, {"status": "failed"})
        self.assertEqual(len(resp.context["print_jobs"]), 25)
        self.assertContains(resp, "page=2&amp;status=failed")
        page2 = self.client.get(self.url, {"status": "failed", "page": 2})
        self.assertEqual(len(page2.context["print_jobs"]), 5)

    def test_multi_status(self):
        self.assertEqual(self._jobs(status=["draft", "completed"]), [self.job_b, self.job_a])
        self.assertEqual(self._jobs(status=["draft", "failed"]), [self.job_a])

    def test_multi_status_ignores_invalid_values(self):
        self.assertEqual(self._jobs(status=["bogus", "draft"]), [self.job_a])
        self.assertEqual(len(self._jobs(status=["bogus", ""])), 2)

    def test_multi_project(self):
        other_project = Project.objects.create(name="Other", created_by=self.user)
        other_project_part = Part.objects.create(project=other_project, quantity=1, name="Gamma Part")
        job_c = PrintJob.objects.create(name="Gamma", status=PrintJob.STATUS_DRAFT)
        PrintJobPart.objects.create(print_job=job_c, part=other_project_part, quantity=1)
        self.assertEqual(self._jobs(project=[self.project.pk, other_project.pk]), [job_c, self.job_a])
        self.assertEqual(self._jobs(project=[other_project.pk, "abc"]), [job_c])

    def test_multi_printer(self):
        other_printer = PrinterProfile.objects.create(name="Second", created_by=self.user)
        job_c = PrintJob.objects.create(name="Gamma", printer=other_printer)
        self.assertEqual(self._jobs(printer=[self.printer.pk, other_printer.pk]), [job_c, self.job_a])

    def test_multi_combined(self):
        self.assertEqual(self._jobs(status=["draft", "completed"], project=[self.project.pk]), [self.job_a])

    def test_multi_project_no_duplicates(self):
        second = Part.objects.create(project=self.project, quantity=1, name="Second Part")
        PrintJobPart.objects.create(print_job=self.job_a, part=second, quantity=1)
        self.assertEqual(self._jobs(project=[self.project.pk]), [self.job_a])

    def test_pagination_keeps_multi_params(self):
        for i in range(30):
            PrintJob.objects.create(name=f"Bulk {i}", status=PrintJob.STATUS_FAILED)
        resp = self.client.get(self.url, {"status": ["failed", "draft"]})
        self.assertContains(resp, "page=2&amp;status=failed&amp;status=draft")

    def test_checked_state_rendered(self):
        resp = self.client.get(self.url, {"status": ["draft", "failed"], "project": [self.project.pk]})
        html = resp.content.decode()
        self.assertRegex(html, r'name="status" value="draft"[^>]*checked')
        self.assertRegex(html, r'name="status" value="failed"[^>]*checked')
        self.assertNotRegex(html, r'name="status" value="completed"[^>]*checked')
        self.assertRegex(html, rf'name="project" value="{self.project.pk}"[^>]*checked')
        self.assertContains(resp, "2 selected")
        self.assertContains(resp, "1 selected")

    def test_nothing_checked_shows_all(self):
        resp = self.client.get(self.url)
        self.assertNotRegex(resp.content.decode(), r'name="status" value="[a-z]+"[^>]*checked')
        self.assertContains(resp, "All statuses")

    def test_hide_completed_shortcut(self):
        resp = self.client.get(self.url)
        self.assertContains(resp, "Hide completed")
        jobs = self._jobs(status=[v for v, _ in PrintJob.STATUS_CHOICES if v != PrintJob.STATUS_COMPLETED])
        self.assertEqual(jobs, [self.job_a])
        self.assertNotContains(self.client.get(self.url, {"status": "draft"}), "Hide completed")

    def test_filters_remembered_across_requests(self):
        self.client.get(self.url, {"status": ["draft", "failed"], "q": "alp"})
        resp = self.client.get(self.url)
        self.assertEqual(resp.status_code, 302)
        followed = self.client.get(self.url, follow=True)
        self.assertEqual(followed.context["status"], ["draft", "failed"])
        self.assertEqual(followed.context["q"], "alp")
        self.assertEqual(list(followed.context["print_jobs"]), [self.job_a])

    def test_redirect_url_carries_params_and_does_not_loop(self):
        self.client.get(self.url, {"status": "draft"})
        resp = self.client.get(self.url)
        self.assertRedirects(resp, f"{self.url}?status=draft", fetch_redirect_response=False)
        self.assertEqual(self.client.get(resp.url).status_code, 200)

    def test_page_alone_keeps_saved_filters_but_drops_stale_page(self):
        self.client.get(self.url, {"status": "draft"})
        resp = self.client.get(self.url, {"page": 2})
        self.assertRedirects(resp, f"{self.url}?status=draft", fetch_redirect_response=False)
        self.assertEqual(self.client.get(resp.url).status_code, 200)

    def test_filter_buttons_expose_selection_to_screen_readers(self):
        html = self.client.get(self.url, {"status": "draft"}).content.decode()
        for key in ("status", "printer", "project"):
            self.assertIn(f'id="filter-{key}-btn"', html)
            self.assertIn(f'aria-labelledby="filter-{key}-label filter-{key}-btn"', html)

    def test_reset_clears_saved_filters(self):
        self.client.get(self.url, {"status": "draft"})
        resp = self.client.get(self.url, {"reset": 1})
        self.assertRedirects(resp, self.url, fetch_redirect_response=False)
        self.assertEqual(self.client.get(self.url).status_code, 200)
        self.assertEqual(len(self._jobs()), 2)

    def test_explicit_empty_submit_clears_saved_filters(self):
        self.client.get(self.url, {"status": "draft"})
        self.client.get(self.url, {"q": ""})
        self.assertEqual(self.client.get(self.url).status_code, 200)

    def test_new_params_override_saved(self):
        self.client.get(self.url, {"status": "draft"})
        self.assertEqual(self._jobs(status="completed"), [self.job_b])
        self.assertRedirects(self.client.get(self.url), f"{self.url}?status=completed", fetch_redirect_response=False)

    def test_saved_deleted_project_and_printer_discarded(self):
        other_printer = PrinterProfile.objects.create(name="Gone", created_by=self.user)
        self.client.get(self.url, {"printer": [self.printer.pk, other_printer.pk]})
        other_printer.delete()
        resp = self.client.get(self.url)
        self.assertRedirects(resp, f"{self.url}?printer={self.printer.pk}", fetch_redirect_response=False)
        self.client.get(self.url, {"printer": self.printer.pk})
        self.printer.delete()
        self.assertEqual(self.client.get(self.url).status_code, 200)

    def test_saved_deleted_project_discarded(self):
        other_project = Project.objects.create(name="Gone", created_by=self.user)
        self.client.get(self.url, {"project": [self.project.pk, other_project.pk]})
        other_project.delete()
        resp = self.client.get(self.url)
        self.assertRedirects(resp, f"{self.url}?project={self.project.pk}", fetch_redirect_response=False)

    def test_filter_project_matches_subtree(self):
        """A job with a part in a grandchild of the root matches the root filter."""
        child = Project.objects.create(name="Child", parent=self.project, quantity=1, created_by=self.user)
        grandchild = Project.objects.create(name="Grandchild", parent=child, quantity=1, created_by=self.user)
        deep_part = Part.objects.create(project=grandchild, quantity=1, name="Deep Part")
        job_deep = PrintJob.objects.create(name="Deep", status=PrintJob.STATUS_DRAFT)
        PrintJobPart.objects.create(print_job=job_deep, part=deep_part, quantity=1)

        self.assertEqual(self._jobs(project=self.project.pk), [job_deep, self.job_a])
        # job_b only holds a part from another root.
        self.assertEqual(self._jobs(project=self.other_project.pk), [self.job_b])

    def test_project_filter_choices_are_roots_only(self):
        """Only top-level projects are offered in the project filter."""
        sub = Project.objects.create(name="Sub", parent=self.project, quantity=1, created_by=self.user)
        resp = self.client.get(self.url)
        choices = list(resp.context["projects"])
        self.assertIn(self.project, choices)
        self.assertIn(self.other_project, choices)
        self.assertNotIn(sub, choices)
        self.assertNotRegex(resp.content.decode(), rf'name="project" value="{sub.pk}"')

    def test_saved_project_filter_dropped_when_not_root_or_missing(self):
        """A remembered project filter for a sub-project or a deleted project is dropped silently."""
        sub = Project.objects.create(name="Sub", parent=self.project, quantity=1, created_by=self.user)
        gone = Project.objects.create(name="Gone", created_by=self.user)
        gone_pk = gone.pk
        gone.delete()
        for pk in (sub.pk, gone_pk):
            session = self.client.session
            session[PrintJobListView.session_key] = {"project": [str(pk)]}
            session.save()
            resp = self.client.get(self.url)
            self.assertEqual(resp.status_code, 200, pk)
            self.assertFalse(resp.context["filters_active"], pk)
            self.assertEqual(len(resp.context["print_jobs"]), 2, pk)

    def test_saved_filters_are_isolated_between_sessions(self):
        self.client.get(self.url, {"status": "draft"})
        other = Client()
        other.login(username="otheruser", password="otherpass123")
        self.assertEqual(other.get(self.url).status_code, 200)
        self.assertEqual(self.client.get(self.url).status_code, 302)

    def test_unicode_digit_and_oversized_ids_ignored(self):
        for value in ("\u00b2", "9" * 30, "-1"):
            for key in ("printer", "project"):
                resp = self.client.get(self.url, {key: value})
                self.assertEqual(resp.status_code, 200, (key, value))
                self.assertEqual(len(resp.context["print_jobs"]), 2)

    def test_saved_oversized_ids_ignored(self):
        session = self.client.session
        session["printjob_list_filters"] = {"printer": ["9" * 30], "project": ["\u00b2"], "status": [], "q": ""}
        session.save()
        self.assertEqual(self.client.get(self.url).status_code, 200)

    def test_reset_link_rendered(self):
        self.assertContains(self.client.get(self.url, {"status": "draft"}), "?reset=1")

    def test_no_table_light_header(self):
        resp = self.client.get(self.url)
        self.assertNotContains(resp, "table-light")

    def test_query_count_constant(self):
        with CaptureQueriesContext(connection) as small:
            self.client.get(self.url)
        for i in range(10):
            job = PrintJob.objects.create(name=f"Extra {i}")
            PrintJobPart.objects.create(print_job=job, part=self.part, quantity=1)
            PrintJobPlate.objects.create(print_job=job, plate_number=1)
        with CaptureQueriesContext(connection) as large:
            self.client.get(self.url)
        self.assertEqual(len(small), len(large))


class JobBedDimensionsTests(TestDataMixin, TestCase):
    """The G-code preview receives the real bed size of the slicing machine profile."""

    def setUp(self):
        super().setUp()
        self.client.login(username="testuser", password="testpass123")

    def _machine(self, area: list[str], height: str = "300") -> OrcaMachineProfile:
        return OrcaMachineProfile.objects.create(
            name="Big",
            orca_name="Big",
            state=OrcaMachineProfile.STATE_RESOLVED,
            instantiation=True,
            settings={"printable_area": area, "printable_height": height},
        )

    def _job_with_plate(self, **kwargs) -> PrintJob:
        job = PrintJob.objects.create(name="J", created_by=self.user, **kwargs)
        PrintJobPlate.objects.create(
            print_job=job,
            plate_number=1,
            gcode_file=SimpleUploadedFile("p.gcode", b"G1 X1 Y1\n"),
        )
        return job

    def test_bed_dimensions_from_machine_profile(self):
        job = self._job_with_plate(machine_profile=self._machine(["0x0", "350x0", "350x300", "0x300"]))
        self.assertEqual(job.bed_dimensions, (350.0, 300.0, 300.0))
        resp = self.client.get(reverse("core:printjob_detail", args=[job.pk]))
        self.assertContains(resp, 'data-bed-x="350"')
        self.assertContains(resp, 'data-bed-y="300"')
        self.assertContains(resp, 'data-bed-z="300"')

    def test_bed_dimensions_fall_back_to_printer(self):
        machine = self._machine(["0x0", "220x0", "220x220", "0x220"], "250")
        printer = PrinterProfile.objects.create(name="P", orca_machine_profile=machine)
        job = self._job_with_plate(printer=printer)
        self.assertEqual(job.bed_dimensions, (220.0, 220.0, 250.0))

    def test_bed_dimensions_without_profile_is_none(self):
        job = self._job_with_plate()
        self.assertIsNone(job.bed_dimensions)
        resp = self.client.get(reverse("core:printjob_detail", args=[job.pk]))
        self.assertEqual(resp.status_code, 200)
        self.assertNotContains(resp, "data-bed-x")

    def test_bed_dimensions_incomplete_profile_is_none(self):
        machine = OrcaMachineProfile.objects.create(
            name="Empty", orca_name="Empty", state=OrcaMachineProfile.STATE_RESOLVED, instantiation=True
        )
        job = self._job_with_plate(machine_profile=machine)
        self.assertIsNone(job.bed_dimensions)

    def test_bed_dimensions_missing_height_defaults_to_256(self):
        machine = OrcaMachineProfile.objects.create(
            name="NoZ",
            orca_name="NoZ",
            state=OrcaMachineProfile.STATE_RESOLVED,
            instantiation=True,
            settings={"printable_area": ["0x0", "200x0", "200x180", "0x180"]},
        )
        job = PrintJob.objects.create(name="J", machine_profile=machine)
        self.assertEqual(job.bed_dimensions, (200.0, 180.0, 256.0))

    def test_bed_dimensions_machine_without_bed_falls_through_to_printer(self):
        empty = OrcaMachineProfile.objects.create(
            name="Empty", orca_name="Empty", state=OrcaMachineProfile.STATE_RESOLVED, instantiation=True
        )
        machine = self._machine(["0x0", "220x0", "220x220", "0x220"], "250")
        printer = PrinterProfile.objects.create(name="P", orca_machine_profile=machine)
        job = PrintJob.objects.create(name="J", machine_profile=empty, printer=printer)
        self.assertEqual(job.bed_dimensions, (220.0, 220.0, 250.0))

    def test_bed_dimensions_malformed_area_is_none(self):
        job = PrintJob.objects.create(name="J", machine_profile=self._machine(["abc", "def"]))
        self.assertIsNone(job.bed_dimensions)
