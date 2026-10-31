"""Tests for the job's slicing status (stored) and its derived display status."""

from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from core.models import OrcaMachineProfile, OrcaPrintPreset, Part, PrintJob, PrintJobPart, PrintJobPlate, PrintQueue
from core.tests.mixins import TestDataMixin, add_attempt, complete_plate, fail_plate, make_sliced_job


class _JobStatusBase(TestDataMixin, TestCase):
    def _display(self, job: PrintJob) -> str:
        """Return the display status via the property and assert the annotation agrees."""
        prop = PrintJob.objects.get(pk=job.pk).display_status
        annotated = PrintJob.objects.with_display_status().get(pk=job.pk).annotated_display_status
        self.assertEqual(prop, annotated)
        return prop


class DisplayStatusTableTests(_JobStatusBase):
    """One test per row of the spec table (§3)."""

    def test_slicing_statuses_without_attempts(self):
        expected = {
            PrintJob.STATUS_DRAFT: PrintJob.DISPLAY_DRAFT,
            PrintJob.STATUS_PENDING: PrintJob.DISPLAY_PENDING,
            PrintJob.STATUS_SLICING: PrintJob.DISPLAY_SLICING,
            PrintJob.STATUS_FAILED: PrintJob.DISPLAY_SLICING_FAILED,
            PrintJob.STATUS_SLICED: PrintJob.DISPLAY_READY,
        }
        for status, display in expected.items():
            with self.subTest(status=status):
                job = PrintJob.objects.create(name=status, status=status)
                self.assertEqual(self._display(job), display)

    def test_ready_when_sliced_without_attempt(self):
        self.assertEqual(self._display(make_sliced_job(plates=2)), PrintJob.DISPLAY_READY)

    def test_printing_when_a_plate_prints(self):
        job = make_sliced_job(plates=2)
        add_attempt(job.plates.first(), self.printer, "printing")
        self.assertEqual(self._display(job), PrintJob.DISPLAY_PRINTING)

    def test_printing_while_a_plate_awaits_review(self):
        job = make_sliced_job(plates=2)
        complete_plate(job.plates.first(), self.printer)
        add_attempt(job.plates.last(), self.printer, "awaiting_review")
        self.assertEqual(self._display(job), PrintJob.DISPLAY_PRINTING)

    def test_in_progress_when_only_waiting(self):
        job = make_sliced_job(plates=1)
        add_attempt(job.plates.first(), self.printer, "waiting")
        self.assertEqual(self._display(job), PrintJob.DISPLAY_IN_PROGRESS)

    def test_in_progress_when_plates_still_to_queue(self):
        job = make_sliced_job(plates=2)
        complete_plate(job.plates.first(), self.printer)
        self.assertEqual(self._display(job), PrintJob.DISPLAY_IN_PROGRESS)

    def test_job_with_failed_plate_and_unqueued_plate_is_in_progress(self):
        job = make_sliced_job(plates=2)
        fail_plate(job.plates.first(), self.printer)
        self.assertEqual(self._display(job), PrintJob.DISPLAY_IN_PROGRESS)

    def test_completed_when_all_plates_passed(self):
        job = make_sliced_job(plates=2)
        for plate in job.plates.all():
            complete_plate(plate, self.printer)
        self.assertEqual(self._display(job), PrintJob.DISPLAY_COMPLETED)
        self.assertTrue(PrintJob.objects.get(pk=job.pk).is_finished)

    def test_partially_failed_when_every_plate_done_or_failed(self):
        job = make_sliced_job(plates=2)
        complete_plate(job.plates.first(), self.printer)
        fail_plate(job.plates.last(), self.printer)
        self.assertEqual(self._display(job), PrintJob.DISPLAY_PARTIALLY_FAILED)

    def test_partially_failed_when_all_plates_failed(self):
        job = make_sliced_job(plates=1)
        fail_plate(job.plates.first(), self.printer)
        self.assertEqual(self._display(job), PrintJob.DISPLAY_PARTIALLY_FAILED)

    def test_cancelled_attempt_alone_is_in_progress(self):
        job = make_sliced_job(plates=1)
        add_attempt(job.plates.first(), self.printer, PrintQueue.RESULT_CANCELLED)
        self.assertEqual(self._display(job), PrintJob.DISPLAY_IN_PROGRESS)

    def test_derived_timestamps(self):
        job = make_sliced_job(plates=2)
        first, second = list(job.plates.order_by("plate_number"))
        a1 = complete_plate(first, self.printer, when=timezone.now() - timezone.timedelta(hours=3))
        self.assertEqual(PrintJob.objects.get(pk=job.pk).started_at, a1.started_at)
        self.assertIsNone(PrintJob.objects.get(pk=job.pk).completed_at)
        a2 = complete_plate(second, self.printer)
        self.assertEqual(PrintJob.objects.get(pk=job.pk).completed_at, a2.resolved_at)

    def test_print_started_counts_any_attempt(self):
        job = make_sliced_job(plates=1)
        self.assertFalse(PrintJob.objects.get(pk=job.pk).print_started)
        attempt = add_attempt(job.plates.first(), self.printer, "waiting")
        self.assertTrue(PrintJob.objects.get(pk=job.pk).print_started)
        self.assertTrue(PrintJob.objects.with_display_status().get(pk=job.pk).print_started)
        attempt.remove()
        self.assertFalse(PrintJob.objects.get(pk=job.pk).print_started)

    def test_str_uses_slicing_status(self):
        job = make_sliced_job("Named", plates=1)
        self.assertEqual(str(job), "Named (Sliced)")


class JobListDisplayStatusTests(_JobStatusBase):
    """The job list filters, hides and counts by the derived display status."""

    def setUp(self):
        super().setUp()
        self.client.login(username="testuser", password="testpass123")
        self.url = reverse("core:printjob_list")
        self.draft = PrintJob.objects.create(name="Draft", status=PrintJob.STATUS_DRAFT)
        self.ready = make_sliced_job("Ready")
        self.printing = make_sliced_job("Printing")
        add_attempt(self.printing.plates.get(), self.printer, "printing")
        self.done = make_sliced_job("Done")
        complete_plate(self.done.plates.get(), self.other_printer)
        self.partial = make_sliced_job("Partial", plates=2)
        complete_plate(self.partial.plates.first(), self.printer)
        fail_plate(self.partial.plates.last(), self.printer)

    def _names(self, **params) -> set[str]:
        response = self.client.get(self.url, params)
        self.assertEqual(response.status_code, 200)
        return {job.name for job in response.context["print_jobs"]}

    def test_status_filter_multi_select_uses_display_status(self):
        self.assertEqual(self._names(status=["printing", "ready"]), {"Printing", "Ready"})
        self.assertEqual(self._names(status="partially_failed"), {"Partial"})

    def test_old_print_statuses_are_ignored(self):
        self.assertEqual(len(self._names(status="sliced")), 5)

    def test_hide_completed_hides_completed_and_partially_failed(self):
        response = self.client.get(self.url)
        self.assertTrue(response.context["show_hide_completed"])
        self.assertContains(response, "Hide finished")
        response = self.client.get(f"{self.url}?{response.context['hide_completed_querystring']}")
        self.assertEqual({job.name for job in response.context["print_jobs"]}, {"Draft", "Ready", "Printing"})
        self.assertFalse(response.context["show_hide_completed"])

    def test_hide_link_keeps_unfinished_selection(self):
        response = self.client.get(self.url, {"status": ["completed", "printing"]})
        self.assertEqual(response.context["hide_completed_querystring"], "status=printing")

    def test_printer_filter_matches_jobs_with_attempt_on_printer(self):
        self.assertEqual(self._names(printer=self.printer.pk), {"Printing", "Partial"})
        self.assertEqual(self._names(printer=self.other_printer.pk), {"Done"})

    def test_badge_shows_display_status(self):
        response = self.client.get(self.url)
        self.assertContains(response, '<span class="badge bg-warning text-dark">Partially failed</span>', html=True)

    def test_job_list_query_count_constant(self):
        def count() -> int:
            with CaptureQueriesContext(connection) as ctx:
                self.client.get(self.url, {"status": ["printing", "completed", "partially_failed", "ready"]})
            return len(ctx.captured_queries)

        baseline = count()
        for index in range(6):
            job = make_sliced_job(f"More {index}", plates=2)
            complete_plate(job.plates.first(), self.printer)
            add_attempt(job.plates.last(), self.printer, "printing" if index % 2 else PrintQueue.RESULT_PASSED)
        self.assertEqual(count(), baseline)


class DashboardDisplayStatusTests(_JobStatusBase):
    """Dashboard and statistics count jobs by their derived display status."""

    def setUp(self):
        super().setUp()
        self.client.login(username="testuser", password="testpass123")
        PrintJob.objects.create(name="Slicing", status=PrintJob.STATUS_SLICING)
        PrintJob.objects.create(name="Broken", status=PrintJob.STATUS_FAILED)
        printing = make_sliced_job("Printing")
        add_attempt(printing.plates.get(), self.printer, "printing")
        done = make_sliced_job("Done")
        complete_plate(done.plates.get(), self.printer)
        partial = make_sliced_job("Partial")
        fail_plate(partial.plates.get(), self.printer)

    def test_dashboard_counts_by_display_status(self):
        context = self.client.get(reverse("core:dashboard")).context
        self.assertEqual(context["completed_jobs"], 1)
        self.assertEqual(context["active_jobs"], 2)  # slicing + printing
        self.assertEqual(context["failed_jobs"], 2)  # slicing failed + partially failed
        self.assertEqual(context["completed_today"], 1)
        self.assertEqual(context["total_quantity_completed"], 1)

    def test_statistics_counts_and_success_rate(self):
        context = self.client.get(reverse("core:statistics")).context
        self.assertEqual(context["success_rate"], 50.0)
        by_status = {row["status"]: row["count"] for row in context["jobs_by_status"]}
        self.assertEqual(
            by_status,
            {"slicing": 1, "slicing_failed": 1, "printing": 1, "completed": 1, "partially_failed": 1},
        )
        self.assertEqual(context["total_plates_completed"], 1)

    def test_farm_completed_today_counts_plates_passed_today(self):
        old = make_sliced_job("Old")
        complete_plate(old.plates.get(), self.printer, when=timezone.now() - timezone.timedelta(days=2))
        context = self.client.get(reverse("core:farm_dashboard")).context
        self.assertEqual(context["completed_today"], 1)


class JobEditRuleTests(TestDataMixin, TestCase):
    """Edit, re-slice and delete rules follow ``print_started`` (Spec §3, Task B3)."""

    def setUp(self):
        super().setUp()
        self.client.login(username="testuser", password="testpass123")
        self.machine = OrcaMachineProfile.objects.create(
            name="M1", state=OrcaMachineProfile.STATE_RESOLVED, instantiation=True
        )
        self.machine2 = OrcaMachineProfile.objects.create(
            name="M2", state=OrcaMachineProfile.STATE_RESOLVED, instantiation=True
        )
        self.preset = OrcaPrintPreset.objects.create(
            name="P1", state=OrcaPrintPreset.STATE_RESOLVED, instantiation=True
        )
        self.project.default_print_preset = self.preset
        self.project.save()
        self.job = make_sliced_job(
            "Sliced Job", plates=2, created_by=self.user, machine_profile=self.machine, print_preset=self.preset
        )
        PrintJob.objects.filter(pk=self.job.pk).update(filament_used_grams=12.0)
        PrintJobPart.objects.create(print_job=self.job, part=self.part, quantity=1)
        self.part.stl_file = "stl_files/a.stl"
        self.part.save()
        self.new_part = Part.objects.create(name="New Part", project=self.project, stl_file="stl_files/b.stl")

    def _start(self) -> PrintQueue:
        return add_attempt(self.job.plates.first(), self.printer, "printing")

    def _edit(self, **data):
        payload = {"name": self.job.name, "machine_profile": self.machine.pk, "notes": "", **data}
        return self.client.post(reverse("core:printjob_update", args=[self.job.pk]), payload)

    def test_name_and_notes_editable_while_printing(self):
        self._start()
        response = self._edit(name="Renamed", notes="Hello", machine_profile=self.machine2.pk)
        self.assertEqual(response.status_code, 302)
        job = PrintJob.objects.get(pk=self.job.pk)
        self.assertEqual(job.name, "Renamed")
        self.assertEqual(job.notes, "Hello")
        self.assertEqual(job.machine_profile, self.machine)  # disabled field: unchanged
        self.assertEqual(job.plates.count(), 2)

    def test_edit_form_disables_machine_profile_after_first_attempt(self):
        url = reverse("core:printjob_update", args=[self.job.pk])
        self.assertFalse(self.client.get(url).context["form"].fields["machine_profile"].disabled)
        self._start()
        self.assertTrue(self.client.get(url).context["form"].fields["machine_profile"].disabled)

    def test_preset_change_after_slicing_resets_to_draft(self):
        """The machine profile decides the slicing; changing it discards the plates."""
        response = self._edit(machine_profile=self.machine2.pk)
        self.assertEqual(response.status_code, 302)
        job = PrintJob.objects.get(pk=self.job.pk)
        self.assertEqual(job.status, PrintJob.STATUS_DRAFT)
        self.assertEqual(job.machine_profile, self.machine2)
        self.assertFalse(job.plates.exists())
        self.assertIsNone(job.filament_used_grams)

    def test_name_change_after_slicing_keeps_plates(self):
        self._edit(name="Only name")
        job = PrintJob.objects.get(pk=self.job.pk)
        self.assertEqual(job.status, PrintJob.STATUS_SLICED)
        self.assertEqual(job.plates.count(), 2)

    def test_add_part_after_slicing_resets_to_draft_and_deletes_plates(self):
        response = self.client.post(
            reverse("core:add_part_to_job", args=[self.new_part.pk]), {"job": self.job.pk, "quantity": 2}
        )
        self.assertRedirects(
            response, reverse("core:printjob_detail", args=[self.job.pk]), fetch_redirect_response=False
        )
        job = PrintJob.objects.get(pk=self.job.pk)
        self.assertEqual(job.status, PrintJob.STATUS_DRAFT)
        self.assertFalse(job.plates.exists())
        self.assertTrue(job.job_parts.filter(part=self.new_part, quantity=2).exists())

    def test_add_part_refused_after_first_attempt(self):
        self._start()
        response = self.client.post(
            reverse("core:add_part_to_job", args=[self.new_part.pk]), {"job": self.job.pk, "quantity": 1}
        )
        self.assertEqual(response.status_code, 302)
        job = PrintJob.objects.get(pk=self.job.pk)
        self.assertEqual(job.status, PrintJob.STATUS_SLICED)
        self.assertEqual(job.plates.count(), 2)
        self.assertFalse(job.job_parts.filter(part=self.new_part).exists())

    def test_add_part_refused_while_slicing(self):
        PrintJob.objects.filter(pk=self.job.pk).update(status=PrintJob.STATUS_SLICING)
        self.client.post(reverse("core:add_part_to_job", args=[self.new_part.pk]), {"job": self.job.pk, "quantity": 1})
        self.assertFalse(PrintJob.objects.get(pk=self.job.pk).job_parts.filter(part=self.new_part).exists())

    def test_remove_part_after_slicing_resets_to_draft(self):
        job_part = self.job.job_parts.get()
        self.client.post(reverse("core:printjob_remove_part", args=[self.job.pk, job_part.pk]))
        job = PrintJob.objects.get(pk=self.job.pk)
        self.assertEqual(job.status, PrintJob.STATUS_DRAFT)
        self.assertFalse(job.plates.exists())
        self.assertFalse(job.job_parts.exists())

    def test_remove_part_refused_after_first_attempt(self):
        self._start()
        job_part = self.job.job_parts.get()
        self.client.post(reverse("core:printjob_remove_part", args=[self.job.pk, job_part.pk]))
        self.assertTrue(PrintJobPart.objects.filter(pk=job_part.pk).exists())
        self.assertEqual(PrintJobPlate.objects.filter(print_job=self.job).count(), 2)

    def test_reslice_refused_after_first_attempt(self):
        add_attempt(self.job.plates.first(), self.printer, "waiting")
        response = self.client.post(reverse("core:printjob_slice", args=[self.job.pk]))
        self.assertEqual(response.status_code, 302)
        job = PrintJob.objects.get(pk=self.job.pk)
        self.assertEqual(job.status, PrintJob.STATUS_SLICED)
        self.assertEqual(job.plates.count(), 2)
        self.assertFalse(self.client.get(reverse("core:printjob_detail", args=[self.job.pk])).context["can_reslice"])

    def test_delete_refused_after_first_attempt(self):
        add_attempt(self.job.plates.first(), self.printer, "waiting")
        self.client.post(reverse("core:printjob_delete", args=[self.job.pk]))
        self.assertTrue(PrintJob.objects.filter(pk=self.job.pk).exists())

    def test_delete_allowed_after_unstarted_attempt_removed(self):
        attempt = add_attempt(self.job.plates.first(), self.printer, "waiting")
        attempt.remove()
        PrintJobPart.objects.filter(print_job=self.job).delete()
        self.client.post(reverse("core:printjob_delete", args=[self.job.pk]))
        self.assertFalse(PrintJob.objects.filter(pk=self.job.pk).exists())

    def test_discard_slicing_refuses_started_job(self):
        self._start()
        with self.assertRaises(ValueError):
            PrintJob.objects.get(pk=self.job.pk).discard_slicing()
        self.assertEqual(PrintJob.objects.get(pk=self.job.pk).plates.count(), 2)

    def test_detail_hides_part_actions_after_first_attempt(self):
        url = reverse("core:printjob_detail", args=[self.job.pk])
        self.assertTrue(self.client.get(url).context["can_change_contents"])
        self._start()
        self.assertFalse(self.client.get(url).context["can_change_contents"])
