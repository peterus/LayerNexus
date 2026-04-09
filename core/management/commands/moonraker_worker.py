"""Long-running Moonraker WebSocket worker.

Run with ``python manage.py moonraker_worker``.  The worker:

1. Enables SQLite WAL mode and a write busy-timeout so it can coexist
   with Gunicorn.
2. Starts an asyncio ``TaskGroup`` that holds one task per configured
   printer and a reconciler task that refreshes the printer list
   periodically (live reload — no container restart on printer
   config changes).
3. For each printer, opens a persistent WebSocket to Moonraker,
   subscribes to print status objects, and feeds incoming
   notifications into :func:`core.services.printer_status_sync.apply_status_event`.
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
from django.db import connection

from core.models import PrinterProfile, PrintQueue
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
def _enable_sqlite_wal() -> None:
    if connection.vendor != "sqlite":
        return
    with connection.cursor() as cursor:
        cursor.execute("PRAGMA journal_mode=WAL;")
        cursor.execute("PRAGMA busy_timeout=5000;")
    logger.info("SQLite WAL mode + busy_timeout enabled")


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


async def _handle_event_for_printer(printer: PrinterProfile, event: dict[str, Any]) -> None:
    entry = await _load_active_queue_entry(printer.pk)
    if entry is None:
        return
    await _apply_event_sync(entry, event)


async def _run_printer(printer: PrinterProfile) -> None:
    async def on_event(event: dict[str, Any]) -> None:
        await _handle_event_for_printer(printer, event)

    client = MoonrakerWebSocketClient(printer, on_event)
    try:
        await client.run()
    except asyncio.CancelledError:
        logger.info("Worker task for printer %s cancelled", printer.name)
        raise


class TaskGroupLike:
    """Minimal task-group interface we use, for testability."""

    def create_task(self, coro: Any, *, name: str | None = None) -> asyncio.Task:  # pragma: no cover
        raise NotImplementedError


async def _reconcile_printers(
    active: dict[int, tuple[asyncio.Task, PrinterSnapshot]],
    task_group: Any,
) -> dict[int, tuple[asyncio.Task, PrinterSnapshot]]:
    """Reconcile active WebSocket tasks against the current DB state.

    Spawns new tasks for newly added printers, cancels tasks for
    removed printers, and restarts tasks whose configuration changed.

    Returns the updated ``active`` map.  The function mutates the
    given map in-place **and** returns it for convenience.
    """
    snapshots = await _load_active_printer_snapshots()

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
        task = task_group.create_task(_run_printer(printer), name=f"moonraker_ws_{pk}")
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
            await _reconcile_printers(active, task_group)
        except Exception:  # noqa: BLE001
            logger.exception("Reconcile loop iteration failed")
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(stop_event.wait(), timeout=interval)


async def _main(reload_interval: float) -> None:
    await _enable_sqlite_wal()

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop_event.set)

    # Shared between _main and the reconciler so we can cancel all
    # per-printer tasks on shutdown. Without this, exiting the
    # TaskGroup context would hang waiting for long-running printer
    # tasks that never observe stop_event.
    active: dict[int, tuple[asyncio.Task, PrinterSnapshot]] = {}

    async with asyncio.TaskGroup() as tg:
        reconciler = tg.create_task(
            _reconcile_loop(tg, active, reload_interval, stop_event),
            name="moonraker_reconciler",
        )
        await stop_event.wait()
        logger.info("Shutdown signal received, stopping worker")
        reconciler.cancel()
        for pk, (task, _snap) in list(active.items()):
            logger.debug("Cancelling worker task for printer %s", pk)
            task.cancel()


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
