"""Tests for manual print-queue reordering (service, view, RBAC, run-next)."""

from unittest import mock

from django.contrib.auth.models import User
from django.test import override_settings
from django.urls import reverse

from core.models import PrinterProfile, PrintJob, PrintJobPlate, PrintQueue
from core.services.queue import (
    QueueReorderError,
    move_queue_entry,
    next_waiting_entry,
    ordered_waiting_entries,
)
from core.tests.mixins import _RBACTestBase


class _QueueFixtureMixin:
    """Helpers to build queue entries with unique plates."""

    def _make_entry(
        self,
        printer: PrinterProfile,
        *,
        priority: int = 2,
        position: int = 0,
        status: str = PrintQueue.STATUS_WAITING,
    ) -> PrintQueue:
        plate = PrintJobPlate.objects.create(
            print_job=self.job,
            plate_number=PrintJobPlate.objects.count() + 1,
        )
        return PrintQueue.objects.create(
            plate=plate,
            printer=printer,
            priority=priority,
            position=position,
            status=status,
        )

    def _order(self, printer: PrinterProfile) -> list[int]:
        return [e.pk for e in ordered_waiting_entries(printer)]


class MoveQueueEntryServiceTests(_QueueFixtureMixin, _RBACTestBase):
    """Tests for ``move_queue_entry()``."""

    def setUp(self):
        super().setUp()
        self.job = PrintJob.objects.create(
            name="Reorder Job", status=PrintJob.STATUS_SLICED, created_by=self.admin_user
        )
        self.a = self._make_entry(self.printer, position=1)
        self.b = self._make_entry(self.printer, position=2)
        self.c = self._make_entry(self.printer, position=3)

    def test_move_up_swaps_with_previous(self):
        move_queue_entry(self.c.pk, direction="up")
        self.assertEqual(self._order(self.printer), [self.a.pk, self.c.pk, self.b.pk])

    def test_move_down_swaps_with_next(self):
        move_queue_entry(self.a.pk, direction="down")
        self.assertEqual(self._order(self.printer), [self.b.pk, self.a.pk, self.c.pk])

    def test_move_to_target_position(self):
        move_queue_entry(self.c.pk, target_index=0)
        self.assertEqual(self._order(self.printer), [self.c.pk, self.a.pk, self.b.pk])

    def test_target_index_is_clamped(self):
        move_queue_entry(self.a.pk, target_index=99)
        self.assertEqual(self._order(self.printer), [self.b.pk, self.c.pk, self.a.pk])

    def test_move_up_at_top_is_noop(self):
        move_queue_entry(self.a.pk, direction="up")
        self.assertEqual(self._order(self.printer), [self.a.pk, self.b.pk, self.c.pk])

    def test_positions_renumbered_gapless(self):
        PrintQueue.objects.filter(pk=self.b.pk).update(position=40)
        PrintQueue.objects.filter(pk=self.c.pk).update(position=90)
        move_queue_entry(self.c.pk, direction="up")
        positions = list(ordered_waiting_entries(self.printer).values_list("position", flat=True))
        self.assertEqual(positions, [1, 2, 3])

    def test_moving_above_higher_priority_adopts_priority(self):
        self.a.priority = 4
        self.a.save()
        move_queue_entry(self.b.pk, direction="up")
        self.b.refresh_from_db()
        self.assertEqual(self.b.priority, 4)
        self.assertEqual(self._order(self.printer), [self.b.pk, self.a.pk, self.c.pk])

    def test_moving_below_lower_priority_adopts_priority(self):
        self.c.priority = 1
        self.c.save()
        move_queue_entry(self.b.pk, direction="down")
        self.b.refresh_from_db()
        self.assertEqual(self.b.priority, 1)
        self.assertEqual(self._order(self.printer), [self.a.pk, self.c.pk, self.b.pk])

    def test_move_within_same_priority_keeps_priority(self):
        move_queue_entry(self.c.pk, direction="up")
        self.c.refresh_from_db()
        self.assertEqual(self.c.priority, 2)

    def test_non_waiting_entry_cannot_be_moved(self):
        for status in (PrintQueue.STATUS_PRINTING, PrintQueue.STATUS_AWAITING_REVIEW):
            entry = self._make_entry(self.printer, status=status, position=5)
            with self.assertRaises(QueueReorderError):
                move_queue_entry(entry.pk, direction="up")

    def test_non_waiting_entries_untouched(self):
        printing = self._make_entry(self.printer, status=PrintQueue.STATUS_PRINTING, position=7)
        move_queue_entry(self.c.pk, target_index=0)
        printing.refresh_from_db()
        self.assertEqual(printing.position, 7)
        self.assertEqual(printing.status, PrintQueue.STATUS_PRINTING)

    def test_other_printer_queue_untouched(self):
        other = PrinterProfile.objects.create(name="Other Printer")
        x = self._make_entry(other, position=1)
        y = self._make_entry(other, position=2)
        move_queue_entry(self.c.pk, target_index=0)
        self.assertEqual(self._order(other), [x.pk, y.pk])

    def test_invalid_direction_raises(self):
        with self.assertRaises(QueueReorderError):
            move_queue_entry(self.a.pk, direction="sideways")

    def test_requires_direction_or_target(self):
        with self.assertRaises(QueueReorderError):
            move_queue_entry(self.a.pk)

    def test_missing_entry_raises_does_not_exist(self):
        with self.assertRaises(PrintQueue.DoesNotExist):
            move_queue_entry(999999, direction="up")

    def test_move_top_and_bottom(self):
        move_queue_entry(self.c.pk, direction="top")
        self.assertEqual(self._order(self.printer), [self.c.pk, self.a.pk, self.b.pk])
        move_queue_entry(self.c.pk, direction="bottom")
        self.assertEqual(self._order(self.printer), [self.a.pk, self.b.pk, self.c.pk])

    def test_move_top_across_priority_groups_adopts_highest(self):
        PrintQueue.objects.filter(pk=self.a.pk).update(priority=4)
        PrintQueue.objects.filter(pk=self.b.pk).update(priority=3)
        result = move_queue_entry(self.c.pk, direction="top")
        self.assertEqual(result.entry.priority, 4)
        self.assertEqual(result.previous_priority, 2)
        self.assertTrue(result.priority_changed)
        self.assertEqual(self._order(self.printer), [self.c.pk, self.a.pk, self.b.pk])

    def test_move_bottom_across_priority_groups_adopts_lowest(self):
        PrintQueue.objects.filter(pk=self.b.pk).update(priority=1)
        PrintQueue.objects.filter(pk=self.c.pk).update(priority=1)
        result = move_queue_entry(self.a.pk, direction="bottom")
        self.assertEqual(result.entry.priority, 1)
        self.assertEqual(self._order(self.printer), [self.b.pk, self.c.pk, self.a.pk])

    def test_result_reports_unchanged_priority(self):
        result = move_queue_entry(self.c.pk, direction="up")
        self.assertFalse(result.priority_changed)

    def test_next_waiting_entry_respects_manual_order(self):
        move_queue_entry(self.c.pk, target_index=0)
        self.assertEqual(next_waiting_entry(self.printer).pk, self.c.pk)


@override_settings(ALLOWED_HOSTS=["testserver"])
class PrintQueueMoveViewTests(_QueueFixtureMixin, _RBACTestBase):
    """Tests for the reorder endpoint (RBAC, validation, redirect)."""

    def setUp(self):
        super().setUp()
        self.job = PrintJob.objects.create(
            name="Reorder Job", status=PrintJob.STATUS_SLICED, created_by=self.admin_user
        )
        self.a = self._make_entry(self.printer, position=1)
        self.b = self._make_entry(self.printer, position=2)

    def _move(self, entry: PrintQueue, **data: str):
        return self.client.post(reverse("core:printqueue_move", args=[entry.pk]), data)

    def test_designer_can_reorder(self):
        self.client.login(username="designer_user", password="testpass123")
        resp = self._move(self.b, direction="up")
        self.assertRedirects(resp, reverse("core:printqueue_list"), fetch_redirect_response=False)
        self.assertEqual(self._order(self.printer), [self.b.pk, self.a.pk])

    def test_operator_can_reorder_to_position(self):
        self.client.login(username="operator_user", password="testpass123")
        resp = self._move(self.a, position="2")
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(self._order(self.printer), [self.b.pk, self.a.pk])

    def test_user_without_role_gets_403(self):
        User.objects.create_user(username="norole", password="testpass123")
        self.client.login(username="norole", password="testpass123")
        resp = self._move(self.b, direction="up")
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(self._order(self.printer), [self.a.pk, self.b.pk])

    def test_anonymous_rejected(self):
        resp = self._move(self.b, direction="up")
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(self._order(self.printer), [self.a.pk, self.b.pk])

    def test_get_not_allowed(self):
        self.client.login(username="designer_user", password="testpass123")
        resp = self.client.get(reverse("core:printqueue_move", args=[self.b.pk]))
        self.assertEqual(resp.status_code, 405)

    def test_printing_entry_rejected_with_400(self):
        printing = self._make_entry(self.printer, status=PrintQueue.STATUS_PRINTING, position=3)
        self.client.login(username="admin_user", password="testpass123")
        resp = self._move(printing, direction="up")
        self.assertEqual(resp.status_code, 400)
        printing.refresh_from_db()
        self.assertEqual(printing.position, 3)

    def test_invalid_direction_400(self):
        self.client.login(username="designer_user", password="testpass123")
        self.assertEqual(self._move(self.b, direction="left").status_code, 400)

    def test_invalid_position_400(self):
        self.client.login(username="designer_user", password="testpass123")
        self.assertEqual(self._move(self.b, position="abc").status_code, 400)
        self.assertEqual(self._move(self.b, position="0").status_code, 400)

    def test_missing_params_400(self):
        self.client.login(username="designer_user", password="testpass123")
        self.assertEqual(self._move(self.b).status_code, 400)

    def test_unknown_entry_404(self):
        self.client.login(username="designer_user", password="testpass123")
        resp = self.client.post(reverse("core:printqueue_move", args=[999999]), {"direction": "up"})
        self.assertEqual(resp.status_code, 404)

    def test_success_message_and_priority_change_message(self):
        PrintQueue.objects.filter(pk=self.a.pk).update(priority=4)
        self.client.login(username="designer_user", password="testpass123")
        self._move(self.b, direction="up")
        msgs = [str(m) for m in self.client.get(reverse("core:printqueue_list")).context["messages"]]
        self.assertTrue(any(m.startswith("Moved Plate") for m in msgs), msgs)
        self.assertTrue(any("Priority changed from Normal to Urgent" in m for m in msgs), msgs)

    def test_no_priority_message_within_same_group(self):
        self.client.login(username="designer_user", password="testpass123")
        self._move(self.b, direction="up")
        msgs = [str(m) for m in self.client.get(reverse("core:printqueue_list")).context["messages"]]
        self.assertFalse(any("Priority changed" in m for m in msgs), msgs)

    @mock.patch("core.views.queue.start_print_for_queue_entry")
    def test_run_all_starts_manually_reordered_entry(self, mock_start):
        self.printer.moonraker_url = "http://printer:7125"
        self.printer.save()
        low = self._make_entry(self.printer, position=3, priority=1)
        low.plate.gcode_file = "gcode/test.gcode"
        low.plate.save()
        self.client.login(username="admin_user", password="testpass123")
        self._move(low, position="1")
        self.client.post(reverse("core:run_all_queues"))
        mock_start.assert_called_once()
        self.assertEqual(mock_start.call_args.args[0].pk, low.pk)

    def test_failed_retry_requeues_first_in_priority_group(self):
        """A failed print goes back to the front of its priority group, not an arbitrary rank."""
        c = self._make_entry(self.printer, position=3)
        self.client.login(username="admin_user", password="testpass123")
        self._move(c, position="1")  # order: c, a, b -> positions 1, 2, 3
        PrintQueue.objects.filter(pk=self.b.pk).update(status=PrintQueue.STATUS_AWAITING_REVIEW)
        self.client.post(reverse("core:printqueue_review", args=[self.b.pk]), {"action": "fail"})
        self.b.refresh_from_db()
        self.assertEqual(self.b.status, PrintQueue.STATUS_WAITING)
        self.assertEqual(self._order(self.printer), [self.b.pk, c.pk, self.a.pk])

    @mock.patch("core.views.queue.start_print_for_queue_entry")
    def test_run_next_starts_manually_reordered_entry(self, mock_start):
        self.printer.moonraker_url = "http://printer:7125"
        self.printer.save()
        low_first = self._make_entry(self.printer, position=3, priority=1)
        self.client.login(username="admin_user", password="testpass123")
        self._move(low_first, position="1")
        self.client.post(reverse("core:run_queue", args=[self.printer.pk]))
        mock_start.assert_called_once()
        self.assertEqual(mock_start.call_args.args[0].pk, low_first.pk)


@override_settings(ALLOWED_HOSTS=["testserver"])
class PrintQueueListReorderUITests(_QueueFixtureMixin, _RBACTestBase):
    """The list shows execution order and reorder controls only where allowed."""

    def setUp(self):
        super().setUp()
        self.job = PrintJob.objects.create(
            name="Reorder Job", status=PrintJob.STATUS_SLICED, created_by=self.admin_user
        )
        self.printing = self._make_entry(self.printer, status=PrintQueue.STATUS_PRINTING, priority=1, position=9)
        self.a = self._make_entry(self.printer, position=1)
        self.b = self._make_entry(self.printer, position=2)
        self.move_url_a = reverse("core:printqueue_move", args=[self.a.pk])
        self.move_url_printing = reverse("core:printqueue_move", args=[self.printing.pk])

    def test_active_entries_listed_first_then_waiting_in_execution_order(self):
        self.client.login(username="designer_user", password="testpass123")
        resp = self.client.get(reverse("core:printqueue_list"))
        pks = [e.pk for e in resp.context["queue_entries"]]
        self.assertEqual(pks, [self.printing.pk, self.a.pk, self.b.pk])

    def test_queue_rank_and_move_flags(self):
        self.client.login(username="designer_user", password="testpass123")
        entries = {e.pk: e for e in self.client.get(reverse("core:printqueue_list")).context["queue_entries"]}
        self.assertIsNone(entries[self.printing.pk].queue_rank)
        self.assertEqual(entries[self.a.pk].queue_rank, 1)
        self.assertEqual(entries[self.b.pk].queue_rank, 2)
        self.assertFalse(entries[self.a.pk].can_move_up)
        self.assertTrue(entries[self.a.pk].can_move_down)
        self.assertTrue(entries[self.b.pk].can_move_up)
        self.assertFalse(entries[self.b.pk].can_move_down)

    def test_reorder_controls_shown_for_queue_manager(self):
        self.client.login(username="designer_user", password="testpass123")
        content = self.client.get(reverse("core:printqueue_list")).content.decode()
        self.assertIn(self.move_url_a, content)
        self.assertNotIn(self.move_url_printing, content)

    def test_reorder_controls_hidden_without_permission(self):
        User.objects.create_user(username="viewer", password="testpass123")
        self.client.login(username="viewer", password="testpass123")
        content = self.client.get(reverse("core:printqueue_list")).content.decode()
        self.assertNotIn(self.move_url_a, content)
