"""Tests for the one-shot scheduled queue start (model, service, views, worker hook)."""

from datetime import timedelta
from unittest import mock

from django.db import IntegrityError, transaction
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from core.models import PrinterProfile, ScheduledQueueStart
from core.services.queue import StartResult
from core.services.scheduled_start import (
    cancel_scheduled_start,
    run_due_scheduled_starts,
    schedule_start,
)
from core.tests.mixins import _RBACTestBase


class ScheduledQueueStartModelTests(TestCase):
    """Constraint and defaults of ScheduledQueueStart."""

    def setUp(self) -> None:
        self.printer = PrinterProfile.objects.create(name="P1")

    def test_only_one_pending_per_printer(self) -> None:
        when = timezone.now() + timedelta(hours=1)
        ScheduledQueueStart.objects.create(printer=self.printer, start_at=when)
        with self.assertRaises(IntegrityError), transaction.atomic():
            ScheduledQueueStart.objects.create(printer=self.printer, start_at=when)

    def test_finished_rows_do_not_block_new_pending(self) -> None:
        when = timezone.now() + timedelta(hours=1)
        ScheduledQueueStart.objects.create(
            printer=self.printer, start_at=when, status=ScheduledQueueStart.STATUS_STARTED
        )
        ScheduledQueueStart.objects.create(printer=self.printer, start_at=when)
        self.assertEqual(ScheduledQueueStart.objects.count(), 2)

    def test_str(self) -> None:
        row = ScheduledQueueStart.objects.create(printer=self.printer, start_at=timezone.now())
        self.assertIn("P1", str(row))


class ScheduleStartServiceTests(TestCase):
    """schedule_start / cancel_scheduled_start."""

    def setUp(self) -> None:
        self.printer = PrinterProfile.objects.create(name="P1")

    def test_creates_pending(self) -> None:
        when = timezone.now() + timedelta(hours=8)
        row = schedule_start(self.printer, when, None)
        self.assertEqual(row.status, ScheduledQueueStart.STATUS_PENDING)
        self.assertEqual(row.start_at, when)

    def test_second_call_updates_start_at(self) -> None:
        first = timezone.now() + timedelta(hours=8)
        second = timezone.now() + timedelta(hours=9)
        a = schedule_start(self.printer, first, None)
        b = schedule_start(self.printer, second, None)
        self.assertEqual(a.pk, b.pk)
        self.assertEqual(ScheduledQueueStart.objects.count(), 1)
        b.refresh_from_db()
        self.assertEqual(b.start_at, second)

    def test_cancel(self) -> None:
        schedule_start(self.printer, timezone.now() + timedelta(hours=1), None)
        self.assertTrue(cancel_scheduled_start(self.printer))
        self.assertEqual(ScheduledQueueStart.objects.get().status, ScheduledQueueStart.STATUS_CANCELLED)
        self.assertFalse(cancel_scheduled_start(self.printer))


@override_settings(SCHEDULED_START_MAX_LATENESS_MINUTES=30)
class RunDueScheduledStartsTests(TestCase):
    """run_due_scheduled_starts with start_next_for_printer mocked."""

    def setUp(self) -> None:
        self.printer = PrinterProfile.objects.create(name="P1")
        self.now = timezone.now()
        patcher = mock.patch("core.services.scheduled_start.start_next_for_printer")
        self.start = patcher.start()
        self.addCleanup(patcher.stop)
        self.start.return_value = StartResult(outcome="started")

    def _row(self, delta: timedelta) -> ScheduledQueueStart:
        return ScheduledQueueStart.objects.create(printer=self.printer, start_at=self.now + delta)

    def test_not_due_is_untouched(self) -> None:
        row = self._row(timedelta(minutes=5))
        self.assertEqual(run_due_scheduled_starts(self.now), [])
        row.refresh_from_db()
        self.assertEqual(row.status, ScheduledQueueStart.STATUS_PENDING)
        self.start.assert_not_called()

    def test_due_starts(self) -> None:
        row = self._row(timedelta(minutes=-1))
        run_due_scheduled_starts(self.now)
        row.refresh_from_db()
        self.assertEqual(row.status, ScheduledQueueStart.STATUS_STARTED)
        self.assertIsNotNone(row.executed_at)
        self.start.assert_called_once_with(self.printer)

    def test_too_late_is_skipped_missed(self) -> None:
        row = self._row(timedelta(minutes=-31))
        run_due_scheduled_starts(self.now)
        row.refresh_from_db()
        self.assertEqual(row.status, ScheduledQueueStart.STATUS_SKIPPED)
        self.assertIn("missed", row.result_message)
        self.start.assert_not_called()

    def test_cancelled_is_not_run(self) -> None:
        row = self._row(timedelta(minutes=-1))
        cancel_scheduled_start(self.printer)
        run_due_scheduled_starts(self.now)
        row.refresh_from_db()
        self.assertEqual(row.status, ScheduledQueueStart.STATUS_CANCELLED)
        self.start.assert_not_called()

    def test_idempotent_second_call(self) -> None:
        self._row(timedelta(minutes=-1))
        run_due_scheduled_starts(self.now)
        run_due_scheduled_starts(self.now)
        self.start.assert_called_once()

    def test_skip_outcomes(self) -> None:
        for outcome in ("busy", "empty", "not_next", "no_gcode"):
            with self.subTest(outcome=outcome):
                ScheduledQueueStart.objects.all().delete()
                self.start.return_value = StartResult(outcome=outcome, error="why")
                row = self._row(timedelta(minutes=-1))
                run_due_scheduled_starts(self.now)
                row.refresh_from_db()
                self.assertEqual(row.status, ScheduledQueueStart.STATUS_SKIPPED)
                self.assertIn(outcome, row.result_message)

    def test_failed_outcomes(self) -> None:
        for outcome in ("error", "not_configured"):
            with self.subTest(outcome=outcome):
                ScheduledQueueStart.objects.all().delete()
                self.start.return_value = StartResult(outcome=outcome, error="boom")
                row = self._row(timedelta(minutes=-1))
                run_due_scheduled_starts(self.now)
                row.refresh_from_db()
                self.assertEqual(row.status, ScheduledQueueStart.STATUS_FAILED)

    def test_exception_marks_failed(self) -> None:
        self.start.side_effect = RuntimeError("kaputt")
        row = self._row(timedelta(minutes=-1))
        run_due_scheduled_starts(self.now)
        row.refresh_from_db()
        self.assertEqual(row.status, ScheduledQueueStart.STATUS_FAILED)
        self.assertIn("kaputt", row.result_message)


@override_settings(ALLOWED_HOSTS=["testserver"], TIME_ZONE="UTC")
class ScheduledStartViewTests(_RBACTestBase):
    """RBAC, create/update/cancel and validation."""

    def setUp(self) -> None:
        super().setUp()
        self.printer = PrinterProfile.objects.create(name="P1")
        self.url = reverse("core:scheduled_start_set", args=[self.printer.pk])
        self.cancel_url = reverse("core:scheduled_start_cancel", args=[self.printer.pk])

    def _value(self, hours: float) -> str:
        return (timezone.localtime() + timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M")

    def test_designer_forbidden(self) -> None:
        self.client.login(username="designer_user", password="testpass123")
        self.assertEqual(self.client.post(self.url, {"start_at": self._value(5)}).status_code, 403)
        self.assertEqual(self.client.post(self.cancel_url).status_code, 403)
        self.assertEqual(ScheduledQueueStart.objects.count(), 0)

    def test_anonymous_forbidden(self) -> None:
        self.assertEqual(self.client.post(self.url, {"start_at": self._value(5)}).status_code, 403)
        self.assertEqual(ScheduledQueueStart.objects.count(), 0)

    def test_operator_creates_and_updates(self) -> None:
        self.client.login(username="operator_user", password="testpass123")
        r = self.client.post(self.url, {"start_at": self._value(5)})
        self.assertRedirects(r, reverse("core:printqueue_list"), fetch_redirect_response=False)
        self.client.post(self.url, {"start_at": self._value(6)})
        row = ScheduledQueueStart.objects.get()
        self.assertEqual(row.created_by, self.operator_user)
        self.assertAlmostEqual((row.start_at - timezone.now()).total_seconds(), 6 * 3600, delta=120)

    def test_past_rejected(self) -> None:
        self.client.login(username="operator_user", password="testpass123")
        self.client.post(self.url, {"start_at": self._value(-1)})
        self.assertEqual(ScheduledQueueStart.objects.count(), 0)

    def test_more_than_seven_days_rejected(self) -> None:
        self.client.login(username="operator_user", password="testpass123")
        self.client.post(self.url, {"start_at": self._value(24 * 8)})
        self.assertEqual(ScheduledQueueStart.objects.count(), 0)

    def test_garbage_rejected(self) -> None:
        self.client.login(username="operator_user", password="testpass123")
        self.client.post(self.url, {"start_at": "nope"})
        self.assertEqual(ScheduledQueueStart.objects.count(), 0)

    @override_settings(TIME_ZONE="Europe/Vienna")
    def test_input_interpreted_in_current_timezone(self) -> None:
        self.client.login(username="operator_user", password="testpass123")
        local = timezone.localtime() + timedelta(hours=5)
        self.client.post(self.url, {"start_at": local.strftime("%Y-%m-%dT%H:%M")})
        row = ScheduledQueueStart.objects.get()
        self.assertAlmostEqual((row.start_at - timezone.now()).total_seconds(), 5 * 3600, delta=120)

    def test_cancel(self) -> None:
        schedule_start(self.printer, timezone.now() + timedelta(hours=2), None)
        self.client.login(username="operator_user", password="testpass123")
        self.client.post(self.cancel_url)
        self.assertEqual(ScheduledQueueStart.objects.get().status, ScheduledQueueStart.STATUS_CANCELLED)

    def test_unknown_printer_404(self) -> None:
        self.client.login(username="operator_user", password="testpass123")
        r = self.client.post(reverse("core:scheduled_start_set", args=[99999]), {"start_at": self._value(5)})
        self.assertEqual(r.status_code, 404)


@override_settings(ALLOWED_HOSTS=["testserver"])
class ScheduledStartListUiTests(_RBACTestBase):
    """The queue list shows the schedule controls only to users who may use them."""

    def setUp(self) -> None:
        super().setUp()
        from core.models import PrintJob, PrintJobPlate, PrintQueue

        printer = PrinterProfile.objects.create(name="UiPrinter")
        job = PrintJob.objects.create(name="J", status=PrintJob.STATUS_SLICED, created_by=self.admin_user)
        plate = PrintJobPlate.objects.create(print_job=job, plate_number=1)
        PrintQueue.objects.create(plate=plate, printer=printer)
        self.printer = printer

    def test_operator_sees_controls_and_pending(self) -> None:
        schedule_start(self.printer, timezone.now() + timedelta(hours=3), None)
        self.client.login(username="operator_user", password="testpass123")
        html = self.client.get(reverse("core:printqueue_list")).content.decode()
        self.assertIn("Scheduled:", html)
        self.assertIn(reverse("core:scheduled_start_cancel", args=[self.printer.pk]), html)

    def test_designer_has_no_controls(self) -> None:
        self.client.login(username="designer_user", password="testpass123")
        html = self.client.get(reverse("core:printqueue_list")).content.decode()
        self.assertNotIn(reverse("core:scheduled_start_set", args=[self.printer.pk]), html)

    def test_last_result_visible(self) -> None:
        ScheduledQueueStart.objects.create(
            printer=self.printer,
            start_at=timezone.now() - timedelta(hours=1),
            status=ScheduledQueueStart.STATUS_SKIPPED,
            result_message="Skipped: printer busy",
        )
        self.client.login(username="operator_user", password="testpass123")
        html = self.client.get(reverse("core:printqueue_list")).content.decode()
        self.assertIn("Skipped: printer busy", html)
