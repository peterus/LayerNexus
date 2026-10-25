"""Tests for print queue views."""

from unittest import mock

from django.test import Client, TestCase, override_settings
from django.urls import reverse

from core.models import PrinterProfile, PrintJob, PrintJobPlate, PrintQueue
from core.tests.mixins import TestDataMixin, _RBACTestBase
from core.views.queue import PrintQueueDeleteView


@override_settings(ALLOWED_HOSTS=["testserver"])
class PrintQueueViewTests(TestDataMixin, TestCase):
    """Tests for PrintQueue views."""

    def setUp(self):
        super().setUp()
        self.client = Client()
        self.client.login(username="testuser", password="testpass123")
        self.printer = PrinterProfile.objects.create(name="Test Printer", created_by=self.user)

    def test_queue_list_200(self):
        r = self.client.get(reverse("core:printqueue_list"))
        self.assertEqual(r.status_code, 200)

    def test_queue_create_get(self):
        r = self.client.get(reverse("core:printqueue_create"))
        self.assertEqual(r.status_code, 200)


@override_settings(ALLOWED_HOSTS=["testserver"])
class PrintQueueCreatePlateChoiceTests(TestDataMixin, TestCase):
    """Which plates the "Add to Queue" form offers.

    Regression: once plate 1 of a multi-plate job started printing, the job
    switched to ``printing`` and the form (which only accepted plates of
    ``sliced`` jobs) silently dropped plates 2..N, so they could never be queued.
    """

    def setUp(self):
        super().setUp()
        self.client = Client()
        self.client.login(username="testuser", password="testpass123")
        self.printer = PrinterProfile.objects.create(name="Test Printer", created_by=self.user)
        self.job = PrintJob.objects.create(name="Multi", status=PrintJob.STATUS_SLICED, created_by=self.user)
        self.plates = [
            PrintJobPlate.objects.create(
                print_job=self.job,
                plate_number=n,
                gcode_file=f"gcode_jobs/job_{self.job.pk}_plate_{n}.gcode",
            )
            for n in (1, 2, 3)
        ]

    def _offered_plate_pks(self) -> set[int]:
        r = self.client.get(reverse("core:printqueue_create"))
        self.assertEqual(r.status_code, 200)
        return set(r.context["form"].fields["plate"].queryset.values_list("pk", flat=True))

    def _start_plate(self, plate: PrintJobPlate) -> None:
        """Mirror ``start_print_for_queue_entry``: plate, entry and job go to printing."""
        PrintQueue.objects.create(plate=plate, printer=self.printer, status=PrintQueue.STATUS_PRINTING)
        plate.status = PrintJobPlate.STATUS_PRINTING
        plate.save(update_fields=["status"])
        self.job.status = PrintJob.STATUS_PRINTING
        self.job.save(update_fields=["status"])

    def test_all_plates_of_sliced_job_offered(self):
        self.assertEqual(self._offered_plate_pks(), {p.pk for p in self.plates})

    def test_remaining_plates_offered_while_job_printing(self):
        self._start_plate(self.plates[0])
        self.assertEqual(self._offered_plate_pks(), {self.plates[1].pk, self.plates[2].pk})

    def test_plate_2_preselected_while_job_printing(self):
        self._start_plate(self.plates[0])
        r = self.client.get(f"{reverse('core:printqueue_create')}?plate={self.plates[1].pk}")
        self.assertEqual(r.context["form"].fields["plate"].initial, self.plates[1].pk)
        self.assertIn(str(self.plates[1].pk), r.context["job_printer_map_json"])

    def test_queue_plate_2_while_job_printing(self):
        self._start_plate(self.plates[0])
        r = self.client.post(
            reverse("core:printqueue_create"),
            {"plate": self.plates[1].pk, "printer": self.printer.pk, "priority": 3},
        )
        self.assertEqual(r.status_code, 302)
        self.assertTrue(PrintQueue.objects.filter(plate=self.plates[1], status=PrintQueue.STATUS_WAITING).exists())

    def test_already_queued_plate_not_offered(self):
        PrintQueue.objects.create(plate=self.plates[0], printer=self.printer)
        self.assertNotIn(self.plates[0].pk, self._offered_plate_pks())

    def test_completed_plate_not_offered(self):
        self.plates[0].status = PrintJobPlate.STATUS_COMPLETED
        self.plates[0].save(update_fields=["status"])
        self.assertNotIn(self.plates[0].pk, self._offered_plate_pks())

    def test_failed_plate_offered_for_retry(self):
        self._start_plate(self.plates[0])
        PrintQueue.objects.filter(plate=self.plates[0]).delete()
        self.plates[0].status = PrintJobPlate.STATUS_FAILED
        self.plates[0].save(update_fields=["status"])
        self.assertIn(self.plates[0].pk, self._offered_plate_pks())

    def test_plates_of_cancelled_job_not_offered(self):
        self.job.status = PrintJob.STATUS_CANCELLED
        self.job.save(update_fields=["status"])
        self.assertEqual(self._offered_plate_pks(), set())


@override_settings(ALLOWED_HOSTS=["testserver"])
class PrintQueueDeleteStatusGuardTests(_RBACTestBase):
    """A Designer may only dequeue *waiting* entries.

    Both Operator and Designer hold ``can_dequeue_job`` *and*
    ``can_manage_print_queue``; the discriminator is that a Designer
    lacks ``can_control_printer`` (see
    ``core/migrations/0002_create_role_groups.py``).  Deleting a
    ``printing`` or ``awaiting_review`` entry would desync the DB from
    the real printer, so those must be blocked for Designers while
    Operators/Admins may remove entries in any state.
    """

    def setUp(self):
        super().setUp()
        self.job = PrintJob.objects.create(
            name="Guard Job",
            status=PrintJob.STATUS_SLICED,
            created_by=self.admin_user,
        )

        def make_entry(status):
            # A unique plate per entry (unique-when-waiting constraint).
            plate = PrintJobPlate.objects.create(
                print_job=self.job,
                plate_number=PrintJobPlate.objects.count() + 1,
            )
            return PrintQueue.objects.create(
                plate=plate,
                printer=self.printer,
                status=status,
            )

        self.waiting_entry = make_entry(PrintQueue.STATUS_WAITING)
        self.printing_entry = make_entry(PrintQueue.STATUS_PRINTING)
        self.review_entry = make_entry(PrintQueue.STATUS_AWAITING_REVIEW)

    def _delete(self, entry):
        return self.client.post(reverse("core:printqueue_delete", args=[entry.pk]))

    def test_designer_can_delete_waiting_entry(self):
        """Designer may dequeue a waiting entry."""
        self.client.login(username="designer_user", password="testpass123")
        resp = self._delete(self.waiting_entry)
        self.assertEqual(resp.status_code, 302)
        self.assertFalse(PrintQueue.objects.filter(pk=self.waiting_entry.pk).exists())

    def test_designer_cannot_delete_printing_entry(self):
        """Designer must NOT dequeue a printing entry (would desync printer)."""
        self.client.login(username="designer_user", password="testpass123")
        resp = self._delete(self.printing_entry)
        self.assertEqual(resp.status_code, 404)
        self.assertTrue(PrintQueue.objects.filter(pk=self.printing_entry.pk).exists())

    def test_designer_cannot_delete_awaiting_review_entry(self):
        """Designer must NOT dequeue an awaiting_review entry."""
        self.client.login(username="designer_user", password="testpass123")
        resp = self._delete(self.review_entry)
        self.assertEqual(resp.status_code, 404)
        self.assertTrue(PrintQueue.objects.filter(pk=self.review_entry.pk).exists())

    def test_operator_can_delete_printing_entry(self):
        """Operator holds can_control_printer -- may dequeue any state."""
        self.client.login(username="operator_user", password="testpass123")
        resp = self._delete(self.printing_entry)
        self.assertEqual(resp.status_code, 302)
        self.assertFalse(PrintQueue.objects.filter(pk=self.printing_entry.pk).exists())

    def test_operator_can_delete_awaiting_review_entry(self):
        """Operator may dequeue an awaiting_review entry."""
        self.client.login(username="operator_user", password="testpass123")
        resp = self._delete(self.review_entry)
        self.assertEqual(resp.status_code, 302)
        self.assertFalse(PrintQueue.objects.filter(pk=self.review_entry.pk).exists())

    def test_designer_delete_is_atomic_against_status_race(self):
        """A status change between SELECT and DELETE must not let a live entry go.

        Simulates the TOCTOU window: ``get_object`` selects the entry while it
        is still ``waiting``, but a printer worker flips it to ``printing``
        before the row is deleted.  The delete must be conditional on the
        status so the live entry survives and the Designer gets a 404.
        """
        self.client.login(username="designer_user", password="testpass123")
        entry = self.waiting_entry
        original_get_object = PrintQueueDeleteView.get_object

        def racing_get_object(view_self, queryset=None):
            obj = original_get_object(view_self, queryset)
            # Worker transitions the row after it was fetched as "waiting".
            PrintQueue.objects.filter(pk=entry.pk).update(status=PrintQueue.STATUS_PRINTING)
            return obj

        with mock.patch.object(PrintQueueDeleteView, "get_object", racing_get_object):
            resp = self._delete(entry)

        self.assertEqual(resp.status_code, 404)
        self.assertTrue(PrintQueue.objects.filter(pk=entry.pk, status=PrintQueue.STATUS_PRINTING).exists())


@override_settings(ALLOWED_HOSTS=["testserver"])
class QueueEntryReviewRaceTests(TestDataMixin, TestCase):
    """The web review loses a race against a review done on the printer."""

    def test_already_reviewed_concurrently_shows_warning(self):
        from core.services.queue import ReviewStateError

        job = PrintJob.objects.create(name="Race", status=PrintJob.STATUS_PRINTING, created_by=self.user)
        plate = PrintJobPlate.objects.create(print_job=job, plate_number=1, status=PrintJobPlate.STATUS_PRINTING)
        entry = PrintQueue.objects.create(plate=plate, printer=self.printer, status=PrintQueue.STATUS_AWAITING_REVIEW)
        self.client.login(username="testuser", password="testpass123")

        with mock.patch("core.views.queue.review_queue_entry", side_effect=ReviewStateError("gone")):
            resp = self.client.post(reverse("core:printqueue_review", args=[entry.pk]), {"action": "pass"}, follow=True)

        self.assertRedirects(resp, reverse("core:printqueue_list"))
        self.assertIn("This queue entry has already been reviewed.", [str(m) for m in resp.context["messages"]])
