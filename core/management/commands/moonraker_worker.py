"""Long-running Moonraker WebSocket worker.

Run with ``python manage.py moonraker_worker``.  The worker:

1. Enables SQLite WAL mode and a write busy-timeout so it can coexist
   with Gunicorn.
2. Starts a failure-isolating task supervisor that holds one task per
   configured printer and a reconciler task that refreshes the printer
   list periodically (live reload — no container restart on printer
   config changes). A crash in one printer's task neither cancels its
   siblings nor kills the worker; the reconciler respawns dead tasks.
3. For each printer, opens a persistent WebSocket to Moonraker,
   subscribes to print status objects, and feeds incoming
   notifications into :func:`core.services.printer_status_sync.apply_status_event`.
4. For printers with ``operator_prompts_enabled``, shows operator dialogs
   on the printer (review a finished plate, start the next job) and handles
   the button presses (see :mod:`core.services.moonraker_operator_ui`).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
from dataclasses import dataclass
from typing import Any

from asgiref.sync import sync_to_async
from django.core.management.base import BaseCommand

from core.models import PrinterProfile, PrintQueue
from core.services.moonraker import MoonrakerClient
from core.services.moonraker_operator_ui import MoonrakerOperatorUI
from core.services.moonraker_ws import MoonrakerWebSocketClient
from core.services.printer_status_sync import apply_status_event

logger = logging.getLogger(__name__)

DEFAULT_RELOAD_INTERVAL = 10.0


@dataclass(frozen=True)
class PrinterSnapshot:
    """Minimal view of a PrinterProfile for diffing."""

    pk: int
    moonraker_url: str
    moonraker_api_key: str
    updated_at: Any  # datetime


@sync_to_async
def _load_active_printer_snapshots() -> dict[int, PrinterSnapshot]:
    rows = PrinterProfile.objects.exclude(moonraker_url="").values(
        "pk", "moonraker_url", "moonraker_api_key", "updated_at"
    )
    return {
        row["pk"]: PrinterSnapshot(
            pk=row["pk"],
            moonraker_url=row["moonraker_url"],
            moonraker_api_key=row["moonraker_api_key"],
            updated_at=row["updated_at"],
        )
        for row in rows
    }


@sync_to_async
def _load_printer(pk: int) -> PrinterProfile | None:
    return PrinterProfile.objects.filter(pk=pk).first()


@sync_to_async
def _load_active_queue_entry(printer_pk: int) -> PrintQueue | None:
    return (
        PrintQueue.objects.filter(
            printer_id=printer_pk,
            status=PrintQueue.STATUS_PRINTING,
        )
        .select_related("plate__print_job")
        .order_by("started_at")
        .first()
    )


@sync_to_async
def _apply_event_sync(entry: PrintQueue, event: dict[str, Any]) -> None:
    apply_status_event(entry, event)


async def _handle_event_for_printer(
    printer: PrinterProfile,
    event: dict[str, Any],
    ui: MoonrakerOperatorUI | None = None,
) -> None:
    """Route one Moonraker notification of ``printer``.

    G-code responses go to the operator UI (if enabled) and never touch the
    status sync; everything else updates the printing queue entry.  When an
    entry transitions to ``awaiting_review`` the review dialog is shown.
    """
    if event.get("method") == "notify_gcode_response":
        if ui is not None:
            await ui.on_gcode_response(event.get("params") or [])
        return

    entry = await _load_active_queue_entry(printer.pk)
    if entry is None:
        return
    await _apply_event_sync(entry, event)
    if ui is not None and entry.status == PrintQueue.STATUS_AWAITING_REVIEW:
        await ui.on_review_pending(entry.pk)


async def _close_stale_dialogs_loop(ui: MoonrakerOperatorUI, interval: float) -> None:
    """Periodically close review dialogs whose entry was reviewed elsewhere."""
    while True:
        await asyncio.sleep(interval)
        try:
            await ui.close_if_stale()
        except Exception:  # noqa: BLE001
            logger.exception("Stale dialog check failed for printer %s", ui.printer.name)


async def _run_printer(printer: PrinterProfile, stale_check_interval: float = DEFAULT_RELOAD_INTERVAL) -> None:
    ui: MoonrakerOperatorUI | None = None
    stale_task: asyncio.Task | None = None
    if printer.operator_prompts_enabled:
        ui = MoonrakerOperatorUI(printer, MoonrakerClient(printer.moonraker_url, printer.moonraker_api_key))
        stale_task = asyncio.create_task(
            _close_stale_dialogs_loop(ui, stale_check_interval), name=f"operator_stale_{printer.pk}"
        )

    async def on_event(event: dict[str, Any]) -> None:
        await _handle_event_for_printer(printer, event, ui)

    client = MoonrakerWebSocketClient(printer, on_event)
    try:
        await client.run()
    except asyncio.CancelledError:
        logger.info("Worker task for printer %s cancelled", printer.name)
        raise
    finally:
        if stale_task is not None:
            stale_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await stale_task
        if ui is not None:
            await ui.cancel_pending()


class TaskGroupLike:
    """Minimal task-group interface we use, for testability."""

    def create_task(self, coro: Any, *, name: str | None = None) -> asyncio.Task:  # pragma: no cover
        raise NotImplementedError


class _Supervisor:
    """Spawns independent, failure-isolated tasks.

    Unlike :class:`asyncio.TaskGroup`, an unhandled exception in one
    spawned task does **not** cancel its siblings nor propagate out of
    the supervisor and tear down the whole worker.  Each per-printer task
    lives and dies on its own; the reconcile loop is responsible for
    noticing a dead task (via :meth:`asyncio.Task.done`) and respawning
    it.  We attach a done-callback that retrieves the exception so the
    event loop does not log a spurious "Task exception was never
    retrieved" warning — the reconciler logs it deliberately instead.
    """

    def __init__(self) -> None:
        self._tasks: set[asyncio.Task] = set()

    def create_task(self, coro: Any, *, name: str | None = None) -> asyncio.Task:
        task = asyncio.ensure_future(coro)
        if name is not None:
            task.set_name(name)
        self._tasks.add(task)
        task.add_done_callback(self._on_task_done)
        return task

    def _on_task_done(self, task: asyncio.Task) -> None:
        self._tasks.discard(task)
        # Retrieve the exception so asyncio does not emit a
        # "Task exception was never retrieved" warning. The reconciler
        # inspects and logs it separately when pruning ``active``.
        if not task.cancelled():
            task.exception()

    async def cancel_all(self) -> None:
        """Cancel all outstanding tasks and wait for them to unwind."""
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)


async def _reconcile_printers(
    active: dict[int, tuple[asyncio.Task, PrinterSnapshot]],
    task_group: Any,
    stale_check_interval: float = DEFAULT_RELOAD_INTERVAL,
) -> dict[int, tuple[asyncio.Task, PrinterSnapshot]]:
    """Reconcile active WebSocket tasks against the current DB state.

    Spawns new tasks for newly added printers, cancels tasks for
    removed printers, and restarts tasks whose configuration changed.

    Returns the updated ``active`` map.  The function mutates the
    given map in-place **and** returns it for convenience.
    """
    snapshots = await _load_active_printer_snapshots()

    # Prune dead per-printer tasks. A task that crashed (or exited on its
    # own) must not linger in ``active`` — otherwise the spawn loop below
    # skips it (``pk in active``) and the printer is never watched again.
    # Removing it here lets the spawn loop restart it on this same pass.
    for pk in list(active.keys()):
        task, _prev = active[pk]
        if not task.done():
            continue
        try:
            exc = task.exception()
        except asyncio.CancelledError:
            # A cancellation we initiated (removed/changed printer); the
            # spawn loop handles re-adding if it is still configured.
            logger.debug("Printer %s task was cancelled, dropping", pk)
        else:
            if exc is not None:
                logger.error("Printer %s task crashed, respawning: %r", pk, exc)
            else:
                logger.warning("Printer %s task exited unexpectedly, respawning", pk)
        del active[pk]

    # Cancel removed / changed printers.
    for pk in list(active.keys()):
        task, prev = active[pk]
        snap = snapshots.get(pk)
        if snap is None:
            logger.info("Printer %s removed, cancelling task", pk)
            task.cancel()
            del active[pk]
            continue
        if (
            snap.moonraker_url != prev.moonraker_url
            or snap.moonraker_api_key != prev.moonraker_api_key
            or snap.updated_at != prev.updated_at
        ):
            logger.info("Printer %s changed, restarting task", pk)
            task.cancel()
            del active[pk]

    # Spawn missing printers.
    for pk, snap in snapshots.items():
        if pk in active:
            continue
        printer = await _load_printer(pk)
        if printer is None:
            continue
        logger.info("Starting worker task for printer %s (%s)", printer.name, snap.moonraker_url)
        task = task_group.create_task(_run_printer(printer, stale_check_interval), name=f"moonraker_ws_{pk}")
        active[pk] = (task, snap)

    return active


async def _reconcile_loop(
    task_group: Any,
    active: dict[int, tuple[asyncio.Task, PrinterSnapshot]],
    interval: float,
    stop_event: asyncio.Event,
) -> None:
    while not stop_event.is_set():
        try:
            await _reconcile_printers(active, task_group, interval)
        except Exception:  # noqa: BLE001
            logger.exception("Reconcile loop iteration failed")
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(stop_event.wait(), timeout=interval)


async def _main(reload_interval: float) -> None:
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop_event.set)

    # Reconciler state: the map of currently-running per-printer tasks,
    # keyed by printer pk. Shared with the reconcile loop, which spawns,
    # cancels and respawns entries. On shutdown the supervisor cancels all
    # tasks via cancel_all(); this map lets the reconciler track them
    # meanwhile.
    active: dict[int, tuple[asyncio.Task, PrinterSnapshot]] = {}

    # Supervisor instead of asyncio.TaskGroup: a crash in one printer's
    # task must not cancel the reconciler or the other printers. The
    # reconcile loop watches for dead tasks and respawns them.
    supervisor = _Supervisor()
    reconciler = supervisor.create_task(
        _reconcile_loop(supervisor, active, reload_interval, stop_event),
        name="moonraker_reconciler",
    )
    try:
        await stop_event.wait()
    finally:
        logger.info("Shutdown signal received, stopping worker")
        reconciler.cancel()
        await supervisor.cancel_all()


class Command(BaseCommand):
    help = "Run the long-running Moonraker WebSocket status worker."

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument(
            "--reload-interval",
            type=float,
            default=float(os.environ.get("WORKER_RELOAD_INTERVAL", DEFAULT_RELOAD_INTERVAL)),
            help="Seconds between printer-list reconciliations (default: 10).",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        reload_interval = options["reload_interval"]
        logger.info("Starting Moonraker worker (reload_interval=%.1fs)", reload_interval)
        with contextlib.suppress(KeyboardInterrupt):
            asyncio.run(_main(reload_interval))
