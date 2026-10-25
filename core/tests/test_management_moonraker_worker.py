"""Tests for the moonraker_worker management command reconciler."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest import IsolatedAsyncioTestCase
from unittest.mock import MagicMock, patch

from asgiref.sync import sync_to_async
from django.contrib.auth.models import User
from django.test import SimpleTestCase, TransactionTestCase
from django.utils import timezone

from core.management.commands.moonraker_worker import (
    _handle_event_for_printer,
    _reconcile_printers,
    _run_printer,
    _Supervisor,
)
from core.models import PrinterProfile


class FakeTaskGroup:
    """Collects create_task calls without actually running coroutines."""

    def __init__(self):
        self.created: list[tuple[str, object]] = []

    def create_task(self, coro, *, name=None):
        self.created.append((name or "", coro))
        # Close the coroutine to avoid "never awaited" warnings.
        coro.close()
        task = asyncio.get_event_loop().create_future()
        task.set_result(None)
        # We need something with .cancel() for the reconciler map.
        return _FakeTask(name)


class _FakeTask:
    def __init__(self, name: str | None):
        self.name = name
        self.cancelled = False
        self._done = False
        self._exc: BaseException | None = None

    def cancel(self) -> None:
        self.cancelled = True

    def done(self) -> bool:
        return self._done

    def exception(self) -> BaseException | None:
        if self.cancelled:
            raise asyncio.CancelledError
        return self._exc

    def set_crashed(self, exc: BaseException) -> None:
        """Mark this fake task as finished with an unhandled exception."""
        self._done = True
        self._exc = exc

    def set_finished(self) -> None:
        """Mark this fake task as finished without an exception."""
        self._done = True


class ReconcilePrintersTests(IsolatedAsyncioTestCase, TransactionTestCase):
    """Async tests for the reconciler. Use TransactionTestCase so that
    ``sync_to_async`` ORM calls see committed data."""

    def setUp(self):
        super().setUp()
        self.user = User.objects.create_user(username="wt", password="x")

    async def _create_printer(self, name: str, url: str, key: str = "") -> PrinterProfile:
        from asgiref.sync import sync_to_async

        return await sync_to_async(PrinterProfile.objects.create)(
            name=name,
            moonraker_url=url,
            moonraker_api_key=key,
            created_by=self.user,
        )

    async def test_spawns_task_for_new_printer(self):
        await self._create_printer("P1", "http://p1:7125")
        tg = FakeTaskGroup()
        active: dict = {}

        await _reconcile_printers(active, tg)

        self.assertEqual(len(tg.created), 1)
        self.assertEqual(len(active), 1)

    async def test_ignores_printer_without_url(self):
        await self._create_printer("P1", "")
        tg = FakeTaskGroup()
        active: dict = {}

        await _reconcile_printers(active, tg)

        self.assertEqual(len(tg.created), 0)
        self.assertEqual(active, {})

    async def test_removed_printer_is_cancelled(self):
        from asgiref.sync import sync_to_async

        p1 = await self._create_printer("P1", "http://p1:7125")
        tg = FakeTaskGroup()
        active: dict = {}
        await _reconcile_printers(active, tg)
        (task, _snap) = active[p1.pk]

        # Clear moonraker_url (= effectively removed from active set).
        await sync_to_async(PrinterProfile.objects.filter(pk=p1.pk).update)(moonraker_url="")

        await _reconcile_printers(active, tg)

        self.assertTrue(task.cancelled)
        self.assertNotIn(p1.pk, active)

    async def test_changed_printer_restarts_task(self):
        from asgiref.sync import sync_to_async

        p1 = await self._create_printer("P1", "http://p1:7125")
        tg = FakeTaskGroup()
        active: dict = {}
        await _reconcile_printers(active, tg)
        (old_task, _snap) = active[p1.pk]
        self.assertEqual(len(tg.created), 1)

        # Touch updated_at by saving a new URL.
        await sync_to_async(PrinterProfile.objects.filter(pk=p1.pk).update)(
            moonraker_url="http://p1-new:7125",
            updated_at=timezone.now(),
        )

        await _reconcile_printers(active, tg)

        self.assertTrue(old_task.cancelled)
        self.assertEqual(len(tg.created), 2)  # one new spawn
        self.assertIn(p1.pk, active)
        (new_task, new_snap) = active[p1.pk]
        self.assertIsNot(new_task, old_task)
        self.assertEqual(new_snap.moonraker_url, "http://p1-new:7125")

    async def test_crashed_task_is_pruned_and_respawned(self):
        """A per-printer task that dies must not stay stuck in ``active``.

        The reconciler should detect the finished (crashed) task, drop it,
        and spawn a fresh task for the same printer so it recovers."""
        p1 = await self._create_printer("P1", "http://p1:7125")
        tg = FakeTaskGroup()
        active: dict = {}
        await _reconcile_printers(active, tg)
        (task, _snap) = active[p1.pk]
        self.assertEqual(len(tg.created), 1)

        # Simulate the printer's WebSocket task crashing.
        task.set_crashed(RuntimeError("boom"))

        await _reconcile_printers(active, tg)

        self.assertEqual(len(tg.created), 2)  # respawned
        self.assertIn(p1.pk, active)
        (new_task, _snap) = active[p1.pk]
        self.assertIsNot(new_task, task)
        self.assertFalse(new_task.done())

    async def test_finished_task_without_exception_is_respawned(self):
        """A task that returns cleanly (e.g. run() exited) is also respawned."""
        p1 = await self._create_printer("P1", "http://p1:7125")
        tg = FakeTaskGroup()
        active: dict = {}
        await _reconcile_printers(active, tg)
        (task, _snap) = active[p1.pk]

        task.set_finished()

        await _reconcile_printers(active, tg)

        self.assertEqual(len(tg.created), 2)
        (new_task, _snap) = active[p1.pk]
        self.assertIsNot(new_task, task)


class SupervisorTests(IsolatedAsyncioTestCase):
    """Tests for the failure-isolating task supervisor."""

    async def test_task_failure_does_not_cancel_siblings(self):
        """Unlike asyncio.TaskGroup, one crashing task must not tear down
        its siblings or propagate out of the supervisor."""
        sup = _Supervisor()
        survivor_finished = asyncio.Event()

        async def crasher() -> None:
            raise RuntimeError("boom")

        async def survivor() -> None:
            await asyncio.sleep(0.05)
            survivor_finished.set()

        sup.create_task(crasher(), name="crasher")
        sup.create_task(survivor(), name="survivor")

        await asyncio.sleep(0.15)

        self.assertTrue(survivor_finished.is_set())

    async def test_cancel_all_cancels_running_tasks(self):
        sup = _Supervisor()

        async def long_running() -> None:
            await asyncio.sleep(10)

        task = sup.create_task(long_running(), name="long")
        await asyncio.sleep(0)  # let the task start

        await sup.cancel_all()

        self.assertTrue(task.cancelled())


class OperatorDialogWorkerTests(IsolatedAsyncioTestCase, TransactionTestCase):
    """End-to-end worker handling of operator dialogs (Moonraker mocked)."""

    def setUp(self):
        super().setUp()
        from core.models import PrintJob, PrintJobPlate, PrintQueue

        self.user = User.objects.create_user(username="wt", password="x")
        self.printer = PrinterProfile.objects.create(
            name="P1",
            moonraker_url="http://p1:7125",
            operator_prompts_enabled=True,
            created_by=self.user,
        )
        self.job = PrintJob.objects.create(name="Bracket", status=PrintJob.STATUS_PRINTING, created_by=self.user)
        self.plate = PrintJobPlate.objects.create(
            print_job=self.job,
            plate_number=1,
            status=PrintJobPlate.STATUS_PRINTING,
            gcode_file="gcode_jobs/p1.gcode",
        )
        self.entry = PrintQueue.objects.create(
            plate=self.plate,
            printer=self.printer,
            status=PrintQueue.STATUS_PRINTING,
            started_at=timezone.now(),
        )
        self.client_mock = MagicMock()

    def _ui(self, printer=None):
        from core.services.moonraker_operator_ui import MoonrakerOperatorUI

        return MoonrakerOperatorUI(printer or self.printer, self.client_mock)

    @staticmethod
    def _finished_event(status: str = "completed") -> dict:
        return {
            "method": "notify_history_changed",
            "params": [{"action": "finished", "job": {"status": status}}],
        }

    def _scripts(self) -> list[str]:
        return [c.args[0] for c in self.client_mock.run_gcode.call_args_list]

    async def _refresh(self, obj):
        await sync_to_async(obj.refresh_from_db)()
        return obj

    async def test_transition_shows_review_dialog(self):
        await _handle_event_for_printer(self.printer, self._finished_event(), self._ui())
        scripts = self._scripts()
        self.assertEqual(len(scripts), 1)
        self.assertIn(f"LN_REVIEW ENTRY={self.entry.pk} RESULT=PASS", scripts[0])
        self.assertIn(f"LN_REVIEW ENTRY={self.entry.pk} RESULT=FAIL", scripts[0])

    async def test_cancelled_print_shows_end_state(self):
        await _handle_event_for_printer(self.printer, self._finished_event("cancelled"), self._ui())
        self.assertIn("Ended: Printer reported: cancelled", self._scripts()[0])

    async def test_no_prompt_when_flag_off(self):
        # The worker builds no UI for printers without the flag.
        await _handle_event_for_printer(self.printer, self._finished_event(), None)
        entry = await self._refresh(self.entry)
        self.assertEqual(entry.status, "awaiting_review")
        self.client_mock.run_gcode.assert_not_called()

    async def test_no_dialog_without_queue_entry(self):
        await sync_to_async(self.entry.delete)()
        await _handle_event_for_printer(self.printer, self._finished_event(), self._ui())
        self.client_mock.run_gcode.assert_not_called()

    async def test_claimed_entry_during_upload_is_ignored(self):
        """A claimed entry whose plate is not printing yet is not ended by the previous print's state."""
        from core.models import PrintJobPlate

        await sync_to_async(PrintJobPlate.objects.filter(pk=self.plate.pk).update)(status="waiting")
        event = {"method": "notify_status_update", "params": [{"print_stats": {"state": "standby"}}]}
        await _handle_event_for_printer(self.printer, event, self._ui())
        entry = await self._refresh(self.entry)
        self.assertEqual(entry.status, "printing")
        self.client_mock.run_gcode.assert_not_called()

    async def test_progress_update_shows_no_dialog(self):
        event = {"method": "notify_status_update", "params": [{"virtual_sdcard": {"progress": 0.5}}]}
        await _handle_event_for_printer(self.printer, event, self._ui())
        self.client_mock.run_gcode.assert_not_called()

    @patch("core.services.queue.get_printer_backend")
    async def test_gcode_response_pass_reviews_and_shows_next(self, mock_get_backend):
        from core.models import PrintJobPlate

        ui = self._ui()
        await _handle_event_for_printer(self.printer, self._finished_event(), ui)
        event = {"method": "notify_gcode_response", "params": [f"LN REVIEW ENTRY={self.entry.pk} RESULT=PASS"]}
        await _handle_event_for_printer(self.printer, event, ui)
        await ui.wait_idle()

        plate = await self._refresh(self.plate)
        self.assertEqual(plate.status, PrintJobPlate.STATUS_COMPLETED)
        self.assertIn("Plate passed (1/1 plates done)", self._scripts()[-1])
        self.assertIn("Queue empty", self._scripts()[-1])
        mock_get_backend.assert_not_called()

    @patch("core.services.queue.get_printer_backend")
    async def test_gcode_response_start_next_starts_print(self, mock_get_backend):
        from core.models import PrintQueue

        await sync_to_async(self.entry.delete)()
        entry = await sync_to_async(PrintQueue.objects.create)(plate=self.plate, printer=self.printer)
        ui = self._ui()
        event = {"method": "notify_gcode_response", "params": [f"LN NEXT ENTRY={entry.pk}"]}
        await _handle_event_for_printer(self.printer, event, ui)
        await ui.wait_idle()

        entry = await self._refresh(entry)
        self.assertEqual(entry.status, PrintQueue.STATUS_PRINTING)
        mock_get_backend.return_value.start_print.assert_called_once()
        self.assertIn("Started: Bracket, plate 1", self._scripts()[-1])

    @patch("core.services.queue.get_printer_backend")
    async def test_double_start_press_starts_once(self, mock_get_backend):
        from core.models import PrintQueue

        await sync_to_async(self.entry.delete)()
        entry = await sync_to_async(PrintQueue.objects.create)(plate=self.plate, printer=self.printer)
        ui = self._ui()
        line = f"LN NEXT ENTRY={entry.pk}"
        await ui.on_gcode_response([line])
        await ui.on_gcode_response([line])
        await ui.wait_idle()

        mock_get_backend.return_value.start_print.assert_called_once()
        self.assertIn("Already handled", self._scripts()[-1])

    async def test_gcode_response_ignored_when_flag_off(self):
        from core.models import PrintQueue

        await sync_to_async(PrintQueue.objects.filter(pk=self.entry.pk).update)(status="awaiting_review")
        event = {"method": "notify_gcode_response", "params": [f"LN REVIEW ENTRY={self.entry.pk} RESULT=PASS"]}
        await _handle_event_for_printer(self.printer, event, None)
        entry = await self._refresh(self.entry)
        self.assertEqual(entry.status, "awaiting_review")

    async def test_gcode_response_from_other_printer_is_rejected(self):
        from core.models import PrintQueue

        await sync_to_async(PrintQueue.objects.filter(pk=self.entry.pk).update)(status="awaiting_review")
        other = await sync_to_async(PrinterProfile.objects.create)(
            name="P2", moonraker_url="http://p2:7125", operator_prompts_enabled=True
        )
        ui = self._ui(other)
        await ui.on_gcode_response([f"LN REVIEW ENTRY={self.entry.pk} RESULT=PASS"])
        await ui.wait_idle()
        entry = await self._refresh(self.entry)
        self.assertEqual(entry.status, "awaiting_review")
        self.assertIn("Already handled", self._scripts()[-1])

    async def test_non_ln_gcode_response_is_ignored(self):
        ui = self._ui()
        handled = await ui.on_gcode_response(["// action:prompt_show", "echo: hello"])
        self.assertFalse(handled)
        self.client_mock.run_gcode.assert_not_called()

    async def test_menu_shows_pending_review(self):
        from core.models import PrintQueue

        await sync_to_async(PrintQueue.objects.filter(pk=self.entry.pk).update)(status="awaiting_review")
        ui = self._ui()
        await ui.on_gcode_response(["LN MENU"])
        await ui.wait_idle()
        self.assertIn(f"LN_REVIEW ENTRY={self.entry.pk} RESULT=PASS", self._scripts()[-1])
        self.assertEqual(ui.open_review_pk, self.entry.pk)

    async def test_prompt_send_failure_is_logged(self):
        from core.services.moonraker import MoonrakerError

        self.client_mock.run_gcode.side_effect = MoonrakerError("Unknown command: RESPOND")
        with self.assertLogs("core.services.moonraker_operator_ui", level="WARNING"):
            await _handle_event_for_printer(self.printer, self._finished_event(), self._ui())
        entry = await self._refresh(self.entry)
        self.assertEqual(entry.status, "awaiting_review")

    async def test_failing_action_is_logged_not_raised(self):
        ui = self._ui()
        with (
            patch("core.services.moonraker_operator_ui.handle_action", side_effect=RuntimeError("boom")),
            self.assertLogs("core.services.moonraker_operator_ui", level="ERROR"),
        ):
            await ui.on_gcode_response(["LN MENU"])
            await ui.wait_idle()
        self.client_mock.run_gcode.assert_not_called()

    async def test_close_if_stale_closes_after_web_review(self):
        from core.services.moonraker_operator_ui import END_PROMPT_SCRIPT
        from core.services.queue import review_queue_entry

        ui = self._ui()
        await _handle_event_for_printer(self.printer, self._finished_event(), ui)
        await ui.close_if_stale()
        self.assertEqual(len(self._scripts()), 1)  # still awaiting review -> left open

        await sync_to_async(review_queue_entry)(self.entry.pk, True)
        await ui.close_if_stale()
        self.assertEqual(self._scripts()[-1], END_PROMPT_SCRIPT)
        self.assertIsNone(ui.open_review_pk)

        await ui.close_if_stale()
        self.assertEqual(len(self._scripts()), 2)  # closed only once

    async def test_run_printer_builds_ui_only_when_enabled(self):
        captured: dict = {}

        class FakeWS:
            def __init__(self, printer, on_event):
                captured["on_event"] = on_event

            async def run(self):
                await captured["on_event"](OperatorDialogWorkerTests._finished_event())

        for enabled, expected_calls in ((False, 0), (True, 1)):
            with self.subTest(enabled=enabled):
                from core.models import PrintQueue

                await sync_to_async(PrintQueue.objects.filter(pk=self.entry.pk).update)(status="printing")
                self.printer.operator_prompts_enabled = enabled
                with (
                    patch("core.management.commands.moonraker_worker.MoonrakerWebSocketClient", FakeWS),
                    patch("core.management.commands.moonraker_worker.MoonrakerClient") as client_cls,
                ):
                    await _run_printer(self.printer, stale_check_interval=3600)
                self.assertEqual(client_cls.return_value.run_gcode.call_count, expected_calls)


class OperatorUIImportBoundaryTests(SimpleTestCase):
    """Moonraker/Klipper-specific UI code must only be imported by the worker."""

    def test_only_worker_imports_operator_ui(self):
        core_dir = Path(__file__).resolve().parent.parent
        importers = {
            path.relative_to(core_dir).as_posix()
            for path in core_dir.rglob("*.py")
            if "tests" not in path.parts
            and path.name != "moonraker_operator_ui.py"
            and "moonraker_operator_ui" in path.read_text()
        }
        self.assertEqual(importers, {"management/commands/moonraker_worker.py"})
