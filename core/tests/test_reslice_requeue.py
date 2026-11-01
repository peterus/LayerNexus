"""Tests for "Add all plates to queue" and "Re-slice & re-queue" on the print job page."""

import tempfile
from datetime import timedelta
from unittest import mock

from django.contrib.auth.models import Group, User
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from core.models import (
    OrcaMachineProfile,
    Part,
    PrinterProfile,
    PrintJob,
    PrintJobPart,
    PrintJobPlate,
    PrintQueue,
    Project,
)
from core.services.orcaslicer import MultiPlateSliceResult, OrcaSlicerError, PlateResult
from core.tests.mixins import add_attempt, complete_plate, fail_plate


class _JobFixture(TestCase):
    """A sliced three-plate job with a machine profile and two compatible printers."""

    def setUp(self) -> None:
        super().setUp()
        self.machine = OrcaMachineProfile.objects.create(
            name="M", orca_name="M", state=OrcaMachineProfile.STATE_RESOLVED, instantiation=True
        )
        self.other_machine = OrcaMachineProfile.objects.create(
            name="M2", orca_name="M2", state=OrcaMachineProfile.STATE_RESOLVED, instantiation=True
        )
        self.printer = PrinterProfile.objects.create(name="P1", orca_machine_profile=self.machine)
        self.printer2 = PrinterProfile.objects.create(name="P2", orca_machine_profile=self.machine)
        self.incompatible = PrinterProfile.objects.create(name="PX", orca_machine_profile=self.other_machine)
        self.inactive = PrinterProfile.objects.create(name="Off", orca_machine_profile=self.machine, is_active=False)
        project = Project.objects.create(name="Proj")
        self.part = Part.objects.create(name="Part", project=project)
        self.part.stl_file.name = "stl_files/part.stl"
        self.part.save(update_fields=["stl_file"])
        self.job = PrintJob.objects.create(name="Job", status=PrintJob.STATUS_SLICED, machine_profile=self.machine)
        PrintJobPart.objects.create(print_job=self.job, part=self.part, quantity=1)
        self.plates = [self._plate(number) for number in (1, 2, 3)]

    def _plate(self, number: int, job: PrintJob | None = None) -> PrintJobPlate:
        job = job or self.job
        return PrintJobPlate.objects.create(
            print_job=job, plate_number=number, gcode_file=f"gcode_jobs/job_{job.pk}_plate_{number}.gcode"
        )

    def _user(self, group: str | None) -> User:
        user = User.objects.create_user(username=f"u_{group or 'none'}_{User.objects.count()}", password="pw")
        if group:
            user.groups.add(Group.objects.get(name=group))
        return user

    def _client(self, group: str | None = "Designer") -> Client:
        client = Client()
        client.force_login(self._user(group))
        return client


# ── Model ──────────────────────────────────────────────────────────────────


class CompatiblePrintersTests(_JobFixture):
    """``PrintJob.compatible_printers()`` offers active printers with the job's machine profile."""

    def test_only_active_matching_printers(self) -> None:
        self.assertEqual(set(self.job.compatible_printers()), {self.printer, self.printer2})

    def test_job_without_machine_profile_allows_all_active(self) -> None:
        self.job.machine_profile = None
        self.job.save(update_fields=["machine_profile"])
        self.assertEqual(set(self.job.compatible_printers()), {self.printer, self.printer2, self.incompatible})


class EnqueuePlatesTests(_JobFixture):
    """``PrintJob.enqueue_plates()`` queues every queueable plate in plate order."""

    def test_enqueues_all_plates_in_plate_order(self) -> None:
        entries = self.job.enqueue_plates(printer=self.printer, priority=3)
        self.assertEqual([entry.plate.plate_number for entry in entries], [1, 2, 3])
        self.assertTrue(all(entry.priority == 3 and entry.printer == self.printer for entry in entries))
        positions = [entry.position for entry in entries]
        self.assertEqual(positions, sorted(positions))

    def test_appends_behind_existing_entries(self) -> None:
        other = PrintJob.objects.create(name="Other", status=PrintJob.STATUS_SLICED)
        existing = PrintQueue.enqueue(plate=self._plate(1, other), printer=self.printer)
        entries = self.job.enqueue_plates(printer=self.printer)
        self.assertTrue(all(entry.position > existing.position for entry in entries))

    def test_skips_waiting_printing_and_completed_plates(self) -> None:
        add_attempt(self.plates[0], self.printer2, "waiting")
        complete_plate(self.plates[1], self.printer2)
        entries = self.job.enqueue_plates(printer=self.printer)
        self.assertEqual([entry.plate for entry in entries], [self.plates[2]])

    def test_failed_plate_is_queued_again(self) -> None:
        fail_plate(self.plates[0], self.printer2)
        entries = self.job.enqueue_plates(printer=self.printer)
        self.assertIn(self.plates[0], [entry.plate for entry in entries])

    def test_unsliced_job_enqueues_nothing(self) -> None:
        self.job.status = PrintJob.STATUS_PENDING
        self.job.save(update_fields=["status"])
        self.assertEqual(self.job.enqueue_plates(printer=self.printer), [])

    def test_incompatible_printer_raises(self) -> None:
        with self.assertRaises(ValueError):
            self.job.enqueue_plates(printer=self.incompatible)
        self.assertFalse(PrintQueue.objects.exists())

    def test_inactive_printer_raises(self) -> None:
        with self.assertRaises(ValueError):
            self.job.enqueue_plates(printer=self.inactive)


class CanResliceAndRequeueTests(_JobFixture):
    """``can_reslice_and_requeue``: queued, and every attempt is a never-started waiting one."""

    def _fresh(self) -> PrintJob:
        return PrintJob.objects.get(pk=self.job.pk)

    def test_no_attempts_is_false(self) -> None:
        """Without queue entries the plain re-slice applies instead."""
        self.assertFalse(self._fresh().can_reslice_and_requeue)

    def test_only_waiting_attempts_is_true(self) -> None:
        add_attempt(self.plates[0], self.printer)
        add_attempt(self.plates[1], self.printer)
        self.assertTrue(self._fresh().can_reslice_and_requeue)

    def test_printing_attempt_is_false(self) -> None:
        add_attempt(self.plates[0], self.printer)
        add_attempt(self.plates[1], self.printer, "printing")
        self.assertFalse(self._fresh().can_reslice_and_requeue)

    def test_history_is_false(self) -> None:
        add_attempt(self.plates[0], self.printer)
        add_attempt(self.plates[1], self.printer, PrintQueue.RESULT_CANCELLED)
        self.assertFalse(self._fresh().can_reslice_and_requeue)

    def test_active_start_claim_is_false(self) -> None:
        add_attempt(self.plates[0], self.printer, start_claimed_at=timezone.now())
        self.assertFalse(self._fresh().can_reslice_and_requeue)

    def test_expired_start_claim_is_true(self) -> None:
        add_attempt(self.plates[0], self.printer, start_claimed_at=timezone.now() - timedelta(hours=1))
        self.assertTrue(self._fresh().can_reslice_and_requeue)

    def test_slicing_job_is_false(self) -> None:
        add_attempt(self.plates[0], self.printer)
        self.job.status = PrintJob.STATUS_SLICING
        self.job.save(update_fields=["status"])
        self.assertFalse(self._fresh().can_reslice_and_requeue)


class StartResliceAndRequeueTests(_JobFixture):
    """``start_reslice_and_requeue()`` removes the waiting entries and remembers the printer."""

    def test_removes_entries_deletes_plates_and_remembers_printer(self) -> None:
        add_attempt(self.plates[0], self.printer, priority=4)
        add_attempt(self.plates[1], self.printer, priority=4)
        printer = self.job.start_reslice_and_requeue()

        self.assertEqual(printer, self.printer)
        self.job.refresh_from_db()
        self.assertEqual(self.job.status, PrintJob.STATUS_PENDING)
        self.assertEqual(self.job.requeue_printer, self.printer)
        self.assertEqual(self.job.requeue_priority, 4)
        self.assertEqual(self.job.requeue_error, "")
        self.assertFalse(PrintQueue.objects.exists())
        self.assertFalse(self.job.plates.exists())

    def test_printer_with_most_entries_wins(self) -> None:
        add_attempt(self.plates[0], self.printer2, priority=1)
        add_attempt(self.plates[1], self.printer, priority=2)
        add_attempt(self.plates[2], self.printer, priority=3)
        self.assertEqual(self.job.start_reslice_and_requeue(), self.printer)
        self.job.refresh_from_db()
        self.assertEqual(self.job.requeue_priority, 3)

    def test_tie_goes_to_the_earliest_entry(self) -> None:
        add_attempt(self.plates[0], self.printer2)
        add_attempt(self.plates[1], self.printer)
        self.assertEqual(self.job.start_reslice_and_requeue(), self.printer2)

    def test_refuses_with_printing_attempt(self) -> None:
        add_attempt(self.plates[0], self.printer)
        add_attempt(self.plates[1], self.printer, "printing")
        with self.assertRaises(ValueError):
            self.job.start_reslice_and_requeue()
        self.assertEqual(PrintQueue.objects.count(), 2)
        self.assertEqual(self.job.plates.count(), 3)

    def test_refuses_with_active_start_claim(self) -> None:
        add_attempt(self.plates[0], self.printer, start_claimed_at=timezone.now())
        with self.assertRaises(ValueError):
            self.job.start_reslice_and_requeue()
        self.assertEqual(PrintQueue.objects.count(), 1)

    def test_refuses_without_entries(self) -> None:
        with self.assertRaises(ValueError):
            self.job.start_reslice_and_requeue()


class RequeueAfterSlicingTests(_JobFixture):
    """``requeue_after_slicing()`` queues the new plates on the remembered printer."""

    def _remember(self, printer: PrinterProfile, priority: int = 2) -> None:
        self.job.requeue_printer = printer
        self.job.requeue_priority = priority
        self.job.save(update_fields=["requeue_printer", "requeue_priority"])

    def test_enqueues_all_plates_and_clears_wish(self) -> None:
        self._remember(self.printer, 3)
        self.assertEqual(self.job.requeue_after_slicing(), 3)
        self.job.refresh_from_db()
        self.assertIsNone(self.job.requeue_printer)
        self.assertEqual(self.job.requeue_error, "")
        entries = PrintQueue.objects.filter(plate__print_job=self.job).order_by("position")
        self.assertEqual([entry.plate.plate_number for entry in entries], [1, 2, 3])
        self.assertTrue(all(entry.printer == self.printer and entry.priority == 3 for entry in entries))

    def test_no_wish_does_nothing(self) -> None:
        self.assertEqual(self.job.requeue_after_slicing(), 0)
        self.assertFalse(PrintQueue.objects.exists())

    def test_incompatible_printer_is_not_used(self) -> None:
        self._remember(self.incompatible)
        self.assertEqual(self.job.requeue_after_slicing(), 0)
        self.job.refresh_from_db()
        self.assertIsNone(self.job.requeue_printer)
        self.assertIn("PX", self.job.requeue_error)
        self.assertFalse(PrintQueue.objects.exists())

    def test_deactivated_printer_is_not_used(self) -> None:
        self._remember(self.inactive)
        self.assertEqual(self.job.requeue_after_slicing(), 0)
        self.job.refresh_from_db()
        self.assertIn("Off", self.job.requeue_error)

    def test_abandon_requeue_keeps_a_hint(self) -> None:
        self._remember(self.printer)
        self.job.abandon_requeue()
        self.job.refresh_from_db()
        self.assertIsNone(self.job.requeue_printer)
        self.assertIn("P1", self.job.requeue_error)


@override_settings(MEDIA_ROOT=tempfile.mkdtemp())
class SlicingWorkerRequeueTests(_JobFixture):
    """The slicing worker re-queues the new plates (or drops the wish on failure)."""

    def setUp(self) -> None:
        super().setUp()
        add_attempt(self.plates[0], self.printer, priority=3)
        add_attempt(self.plates[1], self.printer, priority=3)
        self.job.start_reslice_and_requeue()
        PrintJob.objects.filter(pk=self.job.pk).update(status=PrintJob.STATUS_SLICING)

    def _slice(self, *, error: Exception | None = None, plates: int = 2) -> None:
        from core.services import slicing_worker

        result = MultiPlateSliceResult(
            plates=[PlateResult(plate_number=n, gcode_content=b"G1 X0\n") for n in range(1, plates + 1)]
        )
        with (
            mock.patch.object(slicing_worker, "_build_slicer_kwargs", return_value={}),
            mock.patch.object(slicing_worker, "create_3mf_bundle", return_value=(b"3mf", [])),
            mock.patch.object(slicing_worker, "OrcaSlicerAPIClient") as client_cls,
        ):
            if error is not None:
                client_cls.return_value.slice_bundle.side_effect = error
            else:
                client_cls.return_value.slice_bundle.return_value = result
            slicing_worker._slice_job_in_background(self.job.pk)
        self.job.refresh_from_db()

    def test_success_requeues_every_new_plate(self) -> None:
        self._slice(plates=2)
        self.assertEqual(self.job.status, PrintJob.STATUS_SLICED)
        self.assertIsNone(self.job.requeue_printer)
        entries = PrintQueue.objects.filter(plate__print_job=self.job)
        self.assertEqual(sorted(entry.plate.plate_number for entry in entries), [1, 2])
        self.assertTrue(all(entry.printer == self.printer and entry.priority == 3 for entry in entries))

    def test_failure_does_not_requeue_and_keeps_hint(self) -> None:
        self._slice(error=OrcaSlicerError("boom"))
        self.assertEqual(self.job.status, PrintJob.STATUS_FAILED)
        self.assertIsNone(self.job.requeue_printer)
        self.assertNotEqual(self.job.requeue_error, "")
        self.assertFalse(PrintQueue.objects.exists())


# ── Views ──────────────────────────────────────────────────────────────────


@override_settings(ALLOWED_HOSTS=["testserver"])
class EnqueueAllPlatesViewTests(_JobFixture):
    """``printjob_enqueue_all``: pick a printer + priority, queue every remaining plate."""

    def _url(self) -> str:
        return reverse("core:printjob_enqueue_all", kwargs={"pk": self.job.pk})

    def test_get_offers_compatible_printers_and_lists_plates(self) -> None:
        add_attempt(self.plates[0], self.printer2)
        response = self._client().get(self._url())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(set(response.context["form"].fields["printer"].queryset), {self.printer, self.printer2})
        self.assertEqual(response.context["plates"], self.plates[1:])

    def test_post_enqueues_remaining_plates(self) -> None:
        add_attempt(self.plates[0], self.printer2)
        response = self._client().post(self._url(), {"printer": self.printer.pk, "priority": 4})
        self.assertRedirects(response, reverse("core:printjob_detail", kwargs={"pk": self.job.pk}))
        entries = PrintQueue.objects.filter(printer=self.printer)
        self.assertEqual(sorted(entry.plate.plate_number for entry in entries), [2, 3])
        self.assertTrue(all(entry.priority == 4 for entry in entries))

    def test_post_incompatible_printer_is_rejected(self) -> None:
        response = self._client().post(self._url(), {"printer": self.incompatible.pk, "priority": 2})
        self.assertEqual(response.status_code, 200)
        self.assertIn("printer", response.context["form"].errors)
        self.assertFalse(PrintQueue.objects.exists())

    def test_nothing_queueable_redirects(self) -> None:
        for plate in self.plates:
            add_attempt(plate, self.printer)
        response = self._client().get(self._url())
        self.assertRedirects(response, reverse("core:printjob_detail", kwargs={"pk": self.job.pk}))

    def test_requires_queue_permission(self) -> None:
        self.assertEqual(self._client(None).get(self._url()).status_code, 403)
        self.assertEqual(self._client(None).post(self._url(), {"printer": self.printer.pk}).status_code, 403)
        self.assertFalse(PrintQueue.objects.exists())

    def test_operator_allowed(self) -> None:
        response = self._client("Operator").post(self._url(), {"printer": self.printer.pk, "priority": 2})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(PrintQueue.objects.count(), 3)

    def test_detail_page_shows_add_all_button(self) -> None:
        response = self._client().get(reverse("core:printjob_detail", kwargs={"pk": self.job.pk}))
        self.assertContains(response, self._url())
        self.assertContains(response, "Add all plates to queue")


@override_settings(ALLOWED_HOSTS=["testserver"])
class ResliceRequeueViewTests(_JobFixture):
    """``printjob_reslice_requeue``: remove the waiting entries, slice, re-queue afterwards."""

    def _url(self) -> str:
        return reverse("core:printjob_reslice_requeue", kwargs={"pk": self.job.pk})

    def _detail(self) -> str:
        return reverse("core:printjob_detail", kwargs={"pk": self.job.pk})

    def setUp(self) -> None:
        super().setUp()
        add_attempt(self.plates[0], self.printer)
        add_attempt(self.plates[1], self.printer)
        patcher = mock.patch("core.views.print_jobs._start_orcaslicer_worker")
        self.start_worker = patcher.start()
        self.addCleanup(patcher.stop)

    def test_post_starts_reslice_with_requeue(self) -> None:
        response = self._client().post(self._url())
        self.assertRedirects(response, self._detail())
        self.job.refresh_from_db()
        self.assertEqual(self.job.status, PrintJob.STATUS_PENDING)
        self.assertEqual(self.job.requeue_printer, self.printer)
        self.assertFalse(PrintQueue.objects.exists())
        self.start_worker.assert_called_once()

    def test_refused_once_a_plate_prints(self) -> None:
        PrintQueue.objects.filter(plate=self.plates[0]).update(started_at=timezone.now())
        response = self._client("Operator").post(self._url())
        self.assertRedirects(response, self._detail())
        self.job.refresh_from_db()
        self.assertEqual(self.job.status, PrintJob.STATUS_SLICED)
        self.assertEqual(PrintQueue.objects.count(), 2)
        self.start_worker.assert_not_called()

    def test_refused_while_slicing(self) -> None:
        PrintJob.objects.filter(pk=self.job.pk).update(status=PrintJob.STATUS_SLICING)
        self._client().post(self._url())
        self.assertEqual(PrintQueue.objects.count(), 2)
        self.start_worker.assert_not_called()

    def test_refused_without_part_files(self) -> None:
        Part.objects.filter(pk=self.part.pk).update(stl_file="")
        self._client().post(self._url())
        self.assertEqual(PrintQueue.objects.count(), 2)
        self.start_worker.assert_not_called()

    def test_get_not_allowed(self) -> None:
        self.assertEqual(self._client().get(self._url()).status_code, 405)

    def test_requires_change_printjob_and_queue_permissions(self) -> None:
        user = self._user(None)
        client = Client()
        client.force_login(user)
        self.assertEqual(client.post(self._url()).status_code, 403)
        # change_printjob alone is not enough: the attempts are dequeued and re-queued.
        from django.contrib.auth.models import Permission

        user.user_permissions.add(Permission.objects.get(codename="change_printjob"))
        user = User.objects.get(pk=user.pk)
        client.force_login(user)
        self.assertEqual(client.post(self._url()).status_code, 403)
        self.assertEqual(PrintQueue.objects.count(), 2)

    def test_detail_page_shows_button_only_when_allowed(self) -> None:
        client = self._client()
        response = client.get(self._detail())
        self.assertTrue(response.context["can_reslice_requeue"])
        self.assertContains(response, self._url())
        self.assertFalse(response.context["can_reslice"])

        PrintQueue.objects.filter(plate=self.plates[0]).update(started_at=timezone.now())
        response = client.get(self._detail())
        self.assertFalse(response.context["can_reslice_requeue"])
        self.assertNotContains(response, self._url())

    def test_detail_page_shows_requeue_hints(self) -> None:
        client = self._client()
        client.post(self._url())
        self.assertContains(client.get(self._detail()), "re-queued on P1")
        PrintJob.objects.filter(pk=self.job.pk).update(
            status=PrintJob.STATUS_FAILED, requeue_printer=None, requeue_error="Plates were not re-queued."
        )
        self.assertContains(client.get(self._detail()), "Plates were not re-queued.")

    def test_plain_slice_clears_requeue_wish(self) -> None:
        PrintQueue.objects.all().delete()
        PrintJob.objects.filter(pk=self.job.pk).update(
            status=PrintJob.STATUS_FAILED, requeue_error="old hint", requeue_printer=self.printer
        )
        self._client().post(reverse("core:printjob_slice", kwargs={"pk": self.job.pk}))
        self.job.refresh_from_db()
        self.assertEqual(self.job.status, PrintJob.STATUS_PENDING)
        self.assertIsNone(self.job.requeue_printer)
        self.assertEqual(self.job.requeue_error, "")


# ── Review round 1 ─────────────────────────────────────────────────────────


class ResliceRequeueEdgeCaseTests(_JobFixture):
    """Edge cases: hints, printer deletion, entries without printer, concurrent queueing."""

    def test_enqueue_plates_clears_old_requeue_hint(self) -> None:
        PrintJob.objects.filter(pk=self.job.pk).update(requeue_error="not re-queued")
        self.job.refresh_from_db()
        self.job.enqueue_plates(printer=self.printer)
        self.job.refresh_from_db()
        self.assertEqual(self.job.requeue_error, "")

    def test_requeue_printer_cannot_be_deleted_while_pending(self) -> None:
        from django.db.models import ProtectedError

        add_attempt(self.plates[0], self.printer2)
        self.job.start_reslice_and_requeue()
        with self.assertRaises(ProtectedError):
            self.printer2.delete()

    def test_waiting_entry_without_printer_blocks_reslice_and_requeue(self) -> None:
        add_attempt(self.plates[0], self.printer)
        add_attempt(self.plates[1], None)
        self.assertFalse(PrintJob.objects.get(pk=self.job.pk).can_reslice_and_requeue)
        with self.assertRaises(ValueError):
            self.job.start_reslice_and_requeue()
        self.assertEqual(PrintQueue.objects.count(), 2)

    def test_can_reslice_and_requeue_uses_prefetched_attempts(self) -> None:
        from django.db.models import Prefetch

        add_attempt(self.plates[0], self.printer)
        job = PrintJob.objects.prefetch_related(
            Prefetch("plates", queryset=PrintJobPlate.objects.prefetch_related("queue_entries"))
        ).get(pk=self.job.pk)
        with self.assertNumQueries(0):
            self.assertTrue(job.can_reslice_and_requeue)

    def test_enqueue_plates_skips_plate_queued_concurrently(self) -> None:
        from django.db import IntegrityError

        real_enqueue = PrintQueue.enqueue

        def flaky(**kwargs):
            if kwargs["plate"] == self.plates[1]:
                raise IntegrityError("one open attempt per plate")
            return real_enqueue(**kwargs)

        with mock.patch.object(PrintQueue, "enqueue", side_effect=flaky):
            entries = self.job.enqueue_plates(printer=self.printer)
        self.assertEqual([entry.plate for entry in entries], [self.plates[0], self.plates[2]])


@override_settings(MEDIA_ROOT=tempfile.mkdtemp())
class SlicingWorkerUnexpectedErrorTests(SlicingWorkerRequeueTests):
    """An unexpected slicer error also drops the wish and leaves a hint."""

    def test_unexpected_error_does_not_requeue(self) -> None:
        self._slice(error=RuntimeError("kaput"))
        self.assertEqual(self.job.status, PrintJob.STATUS_FAILED)
        self.assertIsNone(self.job.requeue_printer)
        self.assertIn("P1", self.job.requeue_error)
        self.assertFalse(PrintQueue.objects.exists())


@override_settings(ALLOWED_HOSTS=["testserver"])
class ResliceRequeueViewEdgeCaseTests(_JobFixture):
    """View-level edge cases from review round 1."""

    def test_anonymous_enqueue_all_is_refused_before_the_job_lookup(self) -> None:
        """A missing job is not revealed (404) before the permission check."""
        response = Client().get(reverse("core:printjob_enqueue_all", kwargs={"pk": 999999}))
        self.assertEqual(response.status_code, 403)

    def test_requeue_hint_hidden_once_plates_are_queued(self) -> None:
        PrintJob.objects.filter(pk=self.job.pk).update(requeue_error="Plates were not re-queued.")
        client = self._client()
        detail = reverse("core:printjob_detail", kwargs={"pk": self.job.pk})
        self.assertContains(client.get(detail), "Plates were not re-queued.")
        add_attempt(self.plates[0], self.printer)
        self.assertNotContains(client.get(detail), "Plates were not re-queued.")
