"""Tests for marking part copies of a print attempt as not ok in the web UI.

The review page offers the plate's copies as checkboxes (pre-ticked where the
printer excluded them); after the review, passed attempts are corrected on
``printqueue_exclusions``.  Both need ``can_control_printer`` like the review.
"""

from __future__ import annotations

from django.urls import reverse

from core.models import Part, PrintAttemptExclusion, PrintJobPart, PrintJobPlatePart, PrintQueue
from core.tests.mixins import _RBACTestBase, add_attempt, complete_plate, make_sliced_job


class ExclusionViewTestBase(_RBACTestBase):
    """One plate with 3 copies of a part (object IDs 1-3)."""

    def setUp(self) -> None:
        super().setUp()
        self.part.quantity = 10
        self.part.save()
        self.job = make_sliced_job(plates=1)
        PrintJobPart.objects.create(print_job=self.job, part=self.part, quantity=3)
        self.plate = self.job.plates.get()
        self.copies = [
            PrintJobPlatePart.objects.create(plate=self.plate, part=self.part, copy_index=i, object_ids=[i + 1])
            for i in range(3)
        ]

    def printed(self) -> int:
        return Part.objects.get(pk=self.part.pk).printed_quantity


class ReviewPageExclusionTests(ExclusionViewTestBase):
    def setUp(self) -> None:
        super().setUp()
        self.entry = add_attempt(self.plate, self.printer, "printing")
        self.entry.record_excluded_objects(["OBJECT_2_ID_1_COPY_0"])
        PrintQueue.objects.filter(pk=self.entry.pk).update(completed_at=self.entry.started_at)
        self.url = reverse("core:printqueue_review", args=[self.entry.pk])

    def test_page_lists_copies_with_printer_exclusion_ticked(self) -> None:
        self.client.force_login(self.operator_user)
        response = self.client.get(self.url)
        self.assertContains(response, "1 part(s) excluded")
        self.assertContains(response, f'value="{self.copies[1].pk}" id="excluded-{self.copies[1].pk}" checked')
        self.assertNotContains(response, f'id="excluded-{self.copies[0].pk}" checked')

    def test_pass_stores_the_ticked_copies(self) -> None:
        self.client.force_login(self.operator_user)
        response = self.client.post(
            self.url,
            {"action": "pass", "exclusions_submitted": "1", "excluded": [self.copies[0].pk, self.copies[1].pk]},
        )
        self.assertRedirects(response, reverse("core:printqueue_list"))
        self.entry.refresh_from_db()
        self.assertEqual(self.entry.result, PrintQueue.RESULT_PASSED)
        self.assertEqual(
            set(self.entry.exclusions.values_list("plate_part", flat=True)), {self.copies[0].pk, self.copies[1].pk}
        )
        self.assertEqual(self.printed(), 1)

    def test_bare_pass_keeps_printer_exclusions(self) -> None:
        self.client.force_login(self.operator_user)
        self.client.post(self.url, {"action": "pass"})
        self.assertEqual(self.entry.exclusions.count(), 1)
        self.assertEqual(self.printed(), 2)

    def test_fail_ignores_the_ticks(self) -> None:
        self.client.force_login(self.operator_user)
        self.client.post(self.url, {"action": "fail", "exclusions_submitted": "1"})
        self.assertEqual(self.entry.exclusions.count(), 1)

    def test_foreign_plate_part_is_rejected_without_review(self) -> None:
        other_job = make_sliced_job(name="Other")
        PrintJobPart.objects.create(print_job=other_job, part=self.part, quantity=1)
        foreign = PrintJobPlatePart.objects.create(plate=other_job.plates.get(), part=self.part, copy_index=0)
        self.client.force_login(self.operator_user)
        for value in (foreign.pk, "abc"):
            with self.subTest(value=value):
                response = self.client.post(
                    self.url, {"action": "pass", "exclusions_submitted": "1", "excluded": [value]}
                )
                self.assertRedirects(response, self.url, fetch_redirect_response=False)
        self.entry.refresh_from_db()
        self.assertEqual(self.entry.result, "")


class AttemptExclusionsViewTests(ExclusionViewTestBase):
    def setUp(self) -> None:
        super().setUp()
        self.entry = complete_plate(self.plate, self.printer)
        self.url = reverse("core:printqueue_exclusions", args=[self.entry.pk])

    def test_operator_marks_a_copy_after_the_review(self) -> None:
        self.client.force_login(self.operator_user)
        self.assertContains(self.client.get(self.url), f'name="excluded" value="{self.copies[2].pk}"')
        response = self.client.post(self.url, {"excluded": [self.copies[2].pk]})
        self.assertRedirects(response, reverse("core:printjob_detail", args=[self.job.pk]))
        exclusion = PrintAttemptExclusion.objects.get(attempt=self.entry)
        self.assertEqual(
            (exclusion.plate_part, exclusion.source), (self.copies[2], PrintAttemptExclusion.SOURCE_MANUAL)
        )
        self.assertEqual(exclusion.created_by, self.operator_user)
        self.assertEqual(self.printed(), 2)

        self.client.post(self.url, {})
        self.assertFalse(PrintAttemptExclusion.objects.exists())
        self.assertEqual(self.printed(), 3)

    def test_designer_is_forbidden(self) -> None:
        self.client.force_login(self.designer_user)
        self.assertEqual(self.client.get(self.url).status_code, 403)
        self.assertEqual(self.client.post(self.url, {"excluded": [self.copies[0].pk]}).status_code, 403)
        self.assertFalse(PrintAttemptExclusion.objects.exists())

    def test_anonymous_is_refused(self) -> None:
        """Role mixins raise (403) instead of redirecting, also for anonymous users."""
        self.assertEqual(self.client.get(self.url).status_code, 403)
        self.assertEqual(self.client.post(self.url, {"excluded": [self.copies[0].pk]}).status_code, 403)
        self.assertFalse(PrintAttemptExclusion.objects.exists())

    def test_not_editable_attempts_are_404(self) -> None:
        self.client.force_login(self.operator_user)
        for state in ("printing", "failed", "cancelled"):
            with self.subTest(state=state):
                other = make_sliced_job(name=state)
                attempt = add_attempt(other.plates.get(), self.printer, state)
                url = reverse("core:printqueue_exclusions", args=[attempt.pk])
                self.assertEqual(self.client.get(url).status_code, 404)
                self.assertEqual(self.client.post(url, {}).status_code, 404)

    def test_job_detail_shows_the_exclusion_and_the_link(self) -> None:
        self.entry.set_excluded_plate_parts([self.copies[0].pk])
        self.client.force_login(self.operator_user)
        response = self.client.get(reverse("core:printjob_detail", args=[self.job.pk]))
        self.assertContains(response, "1 part(s) excluded")
        self.assertContains(response, self.url)

    def test_job_detail_hides_the_link_without_permission(self) -> None:
        self.client.force_login(self.designer_user)
        response = self.client.get(reverse("core:printjob_detail", args=[self.job.pk]))
        self.assertNotContains(response, self.url)


class QueueListExclusionTests(ExclusionViewTestBase):
    def test_printing_entry_shows_excluded_badge(self) -> None:
        entry = add_attempt(self.plate, self.printer, "printing")
        entry.record_excluded_objects(["OBJECT_1_ID_0_COPY_0", "OBJECT_3_ID_2_COPY_0"])
        self.client.force_login(self.operator_user)
        response = self.client.get(reverse("core:printqueue_list"))
        self.assertContains(response, "2 part(s) excluded")
