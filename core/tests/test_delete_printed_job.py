"""Tests for deleting print jobs that have print history.

A job may be deleted with its attempts (waiting or resolved); an attempt that is
printing, starting or awaiting review (parts may still be on the bed) blocks the
delete.  The copies the job printed stop counting afterwards.
"""

import shutil
import tempfile
from datetime import timedelta
from pathlib import Path

from django.contrib.auth.models import Group, User
from django.core.files.base import ContentFile
from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from core.models import Part, PrintJob, PrintJobPart, PrintJobPlate, PrintJobPlatePart, PrintQueue
from core.tests.mixins import TestDataMixin, add_attempt, complete_plate, make_sliced_job


class _JobWithHistoryBase(TestDataMixin, TestCase):
    """A sliced two-plate job with three copies of the test part placed on its plates."""

    def setUp(self) -> None:
        super().setUp()
        self.client.login(username="testuser", password="testpass123")
        self.job = make_sliced_job("Old Settings", plates=2, created_by=self.user)
        self.plate1, self.plate2 = list(self.job.plates.order_by("plate_number"))
        PrintJobPart.objects.create(print_job=self.job, part=self.part, quantity=3)
        for copy_index, plate in ((0, self.plate1), (1, self.plate1), (2, self.plate2)):
            PrintJobPlatePart.objects.create(plate=plate, part=self.part, copy_index=copy_index)
        self.url = reverse("core:printjob_delete", args=[self.job.pk])

    def messages_of(self, response) -> str:
        return " ".join(str(message) for message in response.context["messages"])


class CanDeleteTests(_JobWithHistoryBase):
    """``PrintJob.can_delete`` blocks only a printing attempt or a print start in progress."""

    def assert_can_delete(self, expected: bool) -> None:
        self.assertIs(PrintJob.objects.get(pk=self.job.pk).can_delete, expected)
        self.assertIs(PrintJob.objects.with_display_status().get(pk=self.job.pk).can_delete, expected)
        prefetched = PrintJob.objects.prefetch_related("plates__queue_entries").get(pk=self.job.pk)
        with self.assertNumQueries(0):
            self.assertIs(prefetched.can_delete, expected)

    def test_no_attempts(self) -> None:
        self.assert_can_delete(True)

    def test_resolved_attempts_do_not_block(self) -> None:
        complete_plate(self.plate1, self.printer)
        add_attempt(self.plate2, self.printer, PrintQueue.RESULT_FAILED)
        self.assert_can_delete(True)

    def test_waiting_attempt_does_not_block(self) -> None:
        add_attempt(self.plate1, self.printer, "waiting")
        self.assert_can_delete(True)

    def test_awaiting_review_attempt_blocks(self) -> None:
        add_attempt(self.plate1, self.printer, "awaiting_review")
        self.assert_can_delete(False)

    def test_resolved_review_unblocks(self) -> None:
        attempt = add_attempt(self.plate1, self.printer, "awaiting_review")
        attempt.resolve(passed=True)
        self.assert_can_delete(True)

    def test_expired_start_claim_does_not_block(self) -> None:
        add_attempt(self.plate1, self.printer, "waiting", start_claimed_at=timezone.now() - timedelta(hours=1))
        self.assert_can_delete(True)

    def test_resolved_attempt_with_leftover_claim_does_not_block(self) -> None:
        add_attempt(self.plate1, self.printer, PrintQueue.RESULT_PASSED, start_claimed_at=timezone.now())
        self.assert_can_delete(True)

    def test_printing_attempt_blocks(self) -> None:
        complete_plate(self.plate1, self.printer)
        add_attempt(self.plate2, self.printer, "printing")
        self.assert_can_delete(False)

    def test_active_start_claim_blocks(self) -> None:
        add_attempt(self.plate1, self.printer, "waiting", start_claimed_at=timezone.now())
        self.assert_can_delete(False)


class HistorySummaryTests(_JobWithHistoryBase):
    """The confirmation page counts the attempts and the printed copies that stop counting."""

    def test_counts_attempts_and_printed_copies(self) -> None:
        complete_plate(self.plate1, self.printer)
        add_attempt(self.plate2, self.printer, PrintQueue.RESULT_FAILED)
        add_attempt(self.plate2, self.printer, "waiting")
        job = PrintJob.objects.get(pk=self.job.pk)
        self.assertEqual(job.attempt_count(), 3)
        self.assertEqual(job.printed_copies(), 2)

    def test_printed_copies_legacy_rule_without_plate_parts(self) -> None:
        """Unknown plate assignment: the job counts its full quantity only once every plate passed."""
        PrintJob.objects.filter(pk=self.job.pk).update(plate_parts_known=False)
        complete_plate(self.plate1, self.printer)
        self.assertEqual(PrintJob.objects.get(pk=self.job.pk).printed_copies(), 0)
        complete_plate(self.plate2, self.printer)
        self.assertEqual(PrintJob.objects.get(pk=self.job.pk).printed_copies(), 3)
        self.assertEqual(Part.objects.get(pk=self.part.pk).printed_quantity, 3)

    def test_confirmation_page_explains_awaiting_review_block(self) -> None:
        complete_plate(self.plate1, self.printer)
        add_attempt(self.plate2, self.printer, "awaiting_review")
        response = self.client.get(self.url)
        self.assertContains(response, "Cannot delete this print job")
        self.assertContains(response, "Plate 2 is waiting for review on Test Printer")
        self.assertContains(response, "Confirm the print result first (ok/failed), then the job can be deleted.")
        self.assertNotContains(response, '<button type="submit" class="btn btn-danger">')

    def test_confirmation_page_explains_printing_and_starting_block(self) -> None:
        add_attempt(self.plate1, self.printer, "printing")
        add_attempt(self.plate2, self.printer, "waiting", start_claimed_at=timezone.now())
        response = self.client.get(self.url)
        self.assertContains(response, "Plate 1 is printing on Test Printer")
        self.assertContains(response, "Plate 2 is starting on Test Printer")

    def test_no_history(self) -> None:
        job = PrintJob.objects.get(pk=self.job.pk)
        self.assertEqual(job.attempt_count(), 0)
        self.assertEqual(job.printed_copies(), 0)

    def test_confirmation_page_warns_about_history(self) -> None:
        complete_plate(self.plate1, self.printer)
        add_attempt(self.plate2, self.printer, PrintQueue.RESULT_FAILED)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "Cannot delete this print job")
        self.assertContains(response, "2 print attempts")
        self.assertContains(response, "2 printed parts")
        self.assertContains(response, '<button type="submit" class="btn btn-danger">')

    def test_confirmation_page_with_only_waiting_attempts_has_no_history_warning(self) -> None:
        add_attempt(self.plate1, self.printer, "waiting")
        response = self.client.get(self.url)
        self.assertNotContains(response, "This job has print history")
        self.assertContains(response, "1 waiting queue entry")
        self.assertEqual(PrintJob.objects.get(pk=self.job.pk).history_attempt_count(), 0)

    def test_confirmation_page_without_history_has_no_history_warning(self) -> None:
        response = self.client.get(self.url)
        self.assertNotContains(response, "print attempt")
        self.assertContains(response, '<button type="submit" class="btn btn-danger">')

    def test_confirmation_page_query_count_independent_of_plates(self) -> None:
        def count(job: PrintJob) -> int:
            with CaptureQueriesContext(connection) as ctx:
                self.client.get(reverse("core:printjob_delete", args=[job.pk]))
            return len(ctx.captured_queries)

        def job_with_history(plates: int) -> PrintJob:
            job = make_sliced_job(f"History {plates}", plates=plates)
            PrintJobPart.objects.create(print_job=job, part=self.part, quantity=plates)
            for index, plate in enumerate(job.plates.all()):
                PrintJobPlatePart.objects.create(plate=plate, part=self.part, copy_index=index)
                complete_plate(plate, self.printer)
            return job

        self.assertEqual(count(job_with_history(1)), count(job_with_history(8)))


class DeleteJobWithHistoryTests(_JobWithHistoryBase):
    """The delete view removes jobs with print history, unless a print is running or starting."""

    def test_delete_with_completed_and_failed_history(self) -> None:
        passed = complete_plate(self.plate1, self.printer)
        failed = add_attempt(self.plate2, self.printer, PrintQueue.RESULT_FAILED)
        self.assertEqual(Part.objects.get(pk=self.part.pk).printed_quantity, 2)

        response = self.client.post(self.url, follow=True)

        self.assertRedirects(response, reverse("core:printjob_list"))
        self.assertIn("deleted", self.messages_of(response))
        self.assertFalse(PrintJob.objects.filter(pk=self.job.pk).exists())
        self.assertFalse(PrintJobPlate.objects.filter(print_job_id=self.job.pk).exists())
        self.assertFalse(PrintJobPart.objects.filter(print_job_id=self.job.pk).exists())
        self.assertFalse(PrintJobPlatePart.objects.filter(plate_id__in=[self.plate1.pk, self.plate2.pk]).exists())
        self.assertFalse(PrintQueue.objects.filter(pk__in=[passed.pk, failed.pk]).exists())
        self.assertTrue(Part.objects.filter(pk=self.part.pk).exists())
        self.assertEqual(Part.objects.get(pk=self.part.pk).printed_quantity, 0)

    def test_delete_with_waiting_attempts(self) -> None:
        waiting = add_attempt(self.plate2, self.printer, "waiting")
        response = self.client.post(self.url)
        self.assertEqual(response.status_code, 302)
        self.assertFalse(PrintJob.objects.filter(pk=self.job.pk).exists())
        self.assertFalse(PrintQueue.objects.filter(pk=waiting.pk).exists())

    def test_awaiting_review_attempt_blocks_delete(self) -> None:
        review = add_attempt(self.plate1, self.printer, "awaiting_review")
        response = self.client.post(self.url, follow=True)
        self.assertRedirects(response, reverse("core:printjob_detail", args=[self.job.pk]))
        self.assertIn("awaiting review", self.messages_of(response))
        self.assertTrue(PrintJob.objects.filter(pk=self.job.pk).exists())
        self.assertTrue(PrintQueue.objects.filter(pk=review.pk).exists())

    def test_deletable_after_review_resolved(self) -> None:
        review = add_attempt(self.plate1, self.printer, "awaiting_review")
        review.resolve(passed=True)
        response = self.client.post(self.url)
        self.assertRedirects(response, reverse("core:printjob_list"), fetch_redirect_response=False)
        self.assertFalse(PrintJob.objects.filter(pk=self.job.pk).exists())

    def test_print_finished_after_confirmation_page_blocks_delete(self) -> None:
        """The POST re-checks the live attempts: a plate that finished (awaiting review) since the page wins."""
        waiting = add_attempt(self.plate1, self.printer, "waiting")
        self.assertNotContains(self.client.get(self.url), "Cannot delete this print job")
        now = timezone.now()
        PrintQueue.objects.filter(pk=waiting.pk).update(started_at=now, completed_at=now)
        response = self.client.post(self.url, follow=True)
        self.assertRedirects(response, reverse("core:printjob_detail", args=[self.job.pk]))
        self.assertIn("awaiting review", self.messages_of(response))
        self.assertTrue(PrintJob.objects.filter(pk=self.job.pk).exists())
        self.assertTrue(PrintQueue.objects.filter(pk=waiting.pk).exists())

    def test_printing_attempt_blocks_delete(self) -> None:
        complete_plate(self.plate1, self.printer)
        printing = add_attempt(self.plate2, self.printer, "printing")
        page = self.client.get(self.url)
        self.assertContains(page, "Cannot delete this print job")
        self.assertNotContains(page, '<button type="submit" class="btn btn-danger">')

        response = self.client.post(self.url, follow=True)

        self.assertRedirects(response, reverse("core:printjob_detail", args=[self.job.pk]))
        self.assertIn("is printing, starting or awaiting review", self.messages_of(response))
        self.assertTrue(PrintJob.objects.filter(pk=self.job.pk).exists())
        self.assertTrue(PrintQueue.objects.filter(pk=printing.pk).exists())

    def test_active_start_claim_blocks_delete(self) -> None:
        claimed = add_attempt(self.plate1, self.printer, "waiting", start_claimed_at=timezone.now())
        self.assertContains(self.client.get(self.url), "Cannot delete this print job")
        response = self.client.post(self.url, follow=True)
        self.assertIn("is printing, starting or awaiting review", self.messages_of(response))
        self.assertTrue(PrintJob.objects.filter(pk=self.job.pk).exists())
        self.assertTrue(PrintQueue.objects.filter(pk=claimed.pk).exists())

    def test_print_started_after_confirmation_page_blocks_delete(self) -> None:
        """The POST re-checks the live attempts: a print started since the page was shown wins."""
        waiting = add_attempt(self.plate1, self.printer, "waiting")
        self.assertNotContains(self.client.get(self.url), "Cannot delete this print job")
        PrintQueue.objects.filter(pk=waiting.pk).update(started_at=timezone.now())
        response = self.client.post(self.url, follow=True)
        self.assertRedirects(response, reverse("core:printjob_detail", args=[self.job.pk]))
        self.assertIn("is printing, starting or awaiting review", self.messages_of(response))
        self.assertTrue(PrintJob.objects.filter(pk=self.job.pk).exists())
        self.assertTrue(PrintQueue.objects.filter(pk=waiting.pk).exists())

    def test_user_without_permission_gets_403(self) -> None:
        complete_plate(self.plate1, self.printer)
        self.client.login(username="otheruser", password="otherpass123")
        self.assertEqual(self.client.get(self.url).status_code, 403)
        self.assertEqual(self.client.post(self.url).status_code, 403)
        self.assertTrue(PrintJob.objects.filter(pk=self.job.pk).exists())

    def test_designer_and_operator_may_delete_job_with_history(self) -> None:
        for group in ("Designer", "Operator"):
            with self.subTest(group=group):
                user = User.objects.create_user(username=f"{group.lower()}_user", password="pw-123456")
                user.groups.add(Group.objects.get(name=group))
                self.client.force_login(user)
                job = make_sliced_job(f"{group} job", plates=1)
                complete_plate(job.plates.get(), self.printer)
                response = self.client.post(reverse("core:printjob_delete", args=[job.pk]))
                self.assertEqual(response.status_code, 302)
                self.assertFalse(PrintJob.objects.filter(pk=job.pk).exists())


class DeleteButtonVisibilityTests(_JobWithHistoryBase):
    """Detail and list show the delete button for jobs with history, not while a print is running."""

    def delete_link(self) -> str:
        return f'href="{self.url}"'

    def test_buttons_shown_for_completed_job(self) -> None:
        complete_plate(self.plate1, self.printer)
        complete_plate(self.plate2, self.printer)
        detail = self.client.get(reverse("core:printjob_detail", args=[self.job.pk]))
        self.assertContains(detail, self.delete_link())
        self.assertContains(self.client.get(reverse("core:printjob_list")), self.delete_link())

    def test_buttons_hidden_while_printing_or_awaiting_review(self) -> None:
        for state in ("printing", "awaiting_review"):
            with self.subTest(state=state):
                attempt = add_attempt(self.plate1, self.printer, state)
                detail = self.client.get(reverse("core:printjob_detail", args=[self.job.pk]))
                self.assertNotContains(detail, self.delete_link())
                self.assertContains(detail, "Confirm the print result first (ok/failed)")
                self.assertNotContains(self.client.get(reverse("core:printjob_list")), self.delete_link())
                attempt.delete()


class DeleteJobWithHistoryFilesTests(_JobWithHistoryBase):
    """Deleting a job with history removes its plate files like any job delete."""

    def setUp(self) -> None:
        super().setUp()
        self.media_root = tempfile.mkdtemp(prefix="layernexus-media-")
        override = override_settings(MEDIA_ROOT=self.media_root)
        override.enable()
        self.addCleanup(override.disable)
        self.addCleanup(shutil.rmtree, self.media_root, ignore_errors=True)

    def test_plate_files_removed(self) -> None:
        self.plate1.gcode_file.save("plate.gcode", ContentFile(b"G28"), save=False)
        self.plate1.thumbnail.save("thumb.png", ContentFile(b"png"), save=False)
        self.plate1.save()
        names = [self.plate1.gcode_file.name, self.plate1.thumbnail.name]
        complete_plate(self.plate1, self.printer)
        with self.captureOnCommitCallbacks(execute=True):
            self.client.post(self.url)
        self.assertFalse(PrintJob.objects.filter(pk=self.job.pk).exists())
        self.assertEqual([name for name in names if (Path(self.media_root) / name).is_file()], [])
