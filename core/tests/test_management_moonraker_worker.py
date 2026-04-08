"""Tests for the moonraker_worker management command reconciler."""

from __future__ import annotations

import asyncio
from unittest import IsolatedAsyncioTestCase

from django.contrib.auth.models import User
from django.test import TransactionTestCase
from django.utils import timezone

from core.management.commands.moonraker_worker import (
    _reconcile_printers,
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

    def cancel(self) -> None:
        self.cancelled = True


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
