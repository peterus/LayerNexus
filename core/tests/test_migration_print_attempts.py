"""Tests for the print-attempt backfill migration (``0033_backfill_print_attempts``).

The tests build historical data at ``0032`` (queue entries still have ``status`` /
``retry_count``), run the real migrations with
:class:`~django.db.migrations.executor.MigrationExecutor` and check the attempts.
"""

from __future__ import annotations

import importlib
from datetime import timedelta

from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase
from django.utils import timezone

BEFORE = [("core", "0032_print_attempt_fields")]
BACKFILL = [("core", "0033_backfill_print_attempts")]


class PrintAttemptBackfillTests(TransactionTestCase):
    """Run ``0033_backfill_print_attempts`` on historical data built at ``0032``."""

    def setUp(self) -> None:
        """Migrate back to ``0032`` and expose the historical models."""
        super().setUp()
        executor = MigrationExecutor(connection)
        executor.migrate(BEFORE)
        self.apps = executor.loader.project_state(BEFORE).apps
        self.PrintQueue = self.apps.get_model("core", "PrintQueue")
        self.PrintJob = self.apps.get_model("core", "PrintJob")
        self.PrintJobPlate = self.apps.get_model("core", "PrintJobPlate")
        self.PrintJobPart = self.apps.get_model("core", "PrintJobPart")
        self.PrinterProfile = self.apps.get_model("core", "PrinterProfile")
        self.Project = self.apps.get_model("core", "Project")
        self.Part = self.apps.get_model("core", "Part")
        self.printer = self.PrinterProfile.objects.create(name="Farm 1")
        self.t0 = timezone.now() - timedelta(hours=5)

    def tearDown(self) -> None:
        """Migrate forward to the leaf nodes so later tests see the current schema."""
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())
        super().tearDown()

    def _migrate(self, target: list[tuple[str, str]] = BACKFILL) -> None:
        """Run the migrations up to ``target`` on a fresh executor."""
        executor = MigrationExecutor(connection)
        executor.loader.build_graph()
        executor.migrate(target)

    def _job(self, plates: list[str], printer=None):
        """Create a job with plates in the given stored statuses."""
        job = self.PrintJob.objects.create(name="Legacy", status="printing", printer=printer)
        for number, status in enumerate(plates, start=1):
            self.PrintJobPlate.objects.create(
                print_job=job,
                plate_number=number,
                status=status,
                started_at=self.t0 if status != "waiting" else None,
                completed_at=self.t0 + timedelta(hours=1) if status == "completed" else None,
            )
        return job

    def _attempts(self, plate):
        return list(self.PrintQueue.objects.filter(plate=plate).order_by("pk"))

    def test_backfill_creates_passed_attempt_for_completed_plate(self) -> None:
        job = self._job(["completed"], printer=self.printer)
        plate = job.plates.get()

        self._migrate()

        (attempt,) = self._attempts(plate)
        self.assertEqual(attempt.result, "passed")
        self.assertEqual(attempt.printer_id, self.printer.pk)
        self.assertEqual(attempt.started_at, plate.started_at)
        self.assertEqual(attempt.completed_at, plate.completed_at)
        self.assertEqual(attempt.resolved_at, plate.completed_at)

    def test_backfill_passed_attempt_without_known_printer(self) -> None:
        plate = self._job(["completed"]).plates.get()
        self._migrate()
        (attempt,) = self._attempts(plate)
        self.assertIsNone(attempt.printer_id)

    def test_backfill_failed_plate_derives_failed(self) -> None:
        plate = self._job(["failed"]).plates.get()

        self._migrate()
        self._migrate(executor_leaf())

        from core.models import PrintJobPlate as CurrentPlate

        current = CurrentPlate.objects.get(pk=plate.pk)
        attempts = list(current.queue_entries.order_by("pk"))
        self.assertEqual([a.result for a in attempts], ["failed"] * 3)
        self.assertEqual([a.is_retry for a in attempts], [False, True, True])
        self.assertEqual(current.failed_attempts_since_manual_enqueue(), 3)
        current.refresh_stored_status()
        current.refresh_from_db()
        self.assertEqual(current.status, "failed")

    def test_backfill_translates_open_statuses(self) -> None:
        job = self._job(["waiting", "printing", "printing"], printer=self.printer)
        p1, p2, p3 = job.plates.order_by("plate_number")
        updated = self.t0 + timedelta(minutes=30)
        waiting = self.PrintQueue.objects.create(
            plate=p1, printer=self.printer, status="waiting", started_at=self.t0, completed_at=self.t0
        )
        printing = self.PrintQueue.objects.create(
            plate=p2, printer=self.printer, status="printing", status_updated_at=updated
        )
        review = self.PrintQueue.objects.create(
            plate=p3, printer=self.printer, status="awaiting_review", started_at=self.t0, retry_count=1
        )

        self._migrate()
        self._migrate(executor_leaf())

        from core.models import PrintQueue as CurrentQueue

        self.assertEqual(CurrentQueue.objects.get(pk=waiting.pk).state, "waiting")
        current_printing = CurrentQueue.objects.get(pk=printing.pk)
        self.assertEqual(current_printing.state, "printing")
        self.assertEqual(current_printing.started_at, updated)
        current_review = CurrentQueue.objects.get(pk=review.pk)
        self.assertEqual(current_review.state, "awaiting_review")
        self.assertIsNotNone(current_review.completed_at)
        # retry_count=1 survives as one failed attempt in the current retry cycle.
        annotated = CurrentQueue.objects.with_retry_count().get(pk=review.pk)
        self.assertEqual(annotated.retry_count, 1)

    def test_backfill_resolves_duplicate_open_entries(self) -> None:
        plate = self._job(["printing"], printer=self.printer).plates.get()
        printing = self.PrintQueue.objects.create(
            plate=plate, printer=self.printer, status="printing", started_at=self.t0
        )
        # Allowed by the old constraint (only one *waiting* entry per plate).
        waiting = self.PrintQueue.objects.create(plate=plate, printer=self.printer, status="waiting")

        self._migrate(executor_leaf())

        from core.models import PrintQueue as CurrentQueue

        self.assertEqual(CurrentQueue.objects.get(pk=printing.pk).state, "printing")
        # Never started: removed like a manual dequeue (deleted), not kept as "cancelled".
        self.assertFalse(CurrentQueue.objects.filter(pk=waiting.pk).exists())

    def test_backfill_cancels_started_duplicate(self) -> None:
        plate = self._job(["printing"], printer=self.printer).plates.get()
        review = self.PrintQueue.objects.create(
            plate=plate, printer=self.printer, status="awaiting_review", completed_at=self.t0
        )
        printing = self.PrintQueue.objects.create(
            plate=plate, printer=self.printer, status="printing", started_at=self.t0
        )

        self._migrate(executor_leaf())

        from core.models import PrintQueue as CurrentQueue

        self.assertEqual(CurrentQueue.objects.get(pk=review.pk).state, "awaiting_review")
        self.assertEqual(CurrentQueue.objects.get(pk=printing.pk).state, "cancelled")

    def test_backfill_is_idempotent(self) -> None:
        job = self._job(["completed", "failed", "waiting"], printer=self.printer)
        p3 = job.plates.get(plate_number=3)
        self.PrintQueue.objects.create(plate=p3, printer=self.printer, status="awaiting_review", retry_count=2)
        self._migrate()
        module = importlib.import_module("core.migrations.0033_backfill_print_attempts")
        apps = MigrationExecutor(connection).loader.project_state(BACKFILL).apps
        before = sorted(apps.get_model("core", "PrintQueue").objects.values_list("plate_id", "result", "is_retry"))
        module.backfill_print_attempts(apps, None)
        after = sorted(apps.get_model("core", "PrintQueue").objects.values_list("plate_id", "result", "is_retry"))
        self.assertEqual(before, after)
        self.assertEqual(len(after), 1 + 3 + 3)

    def test_backfill_resets_stuck_printing_plate(self) -> None:
        job = self._job(["printing", "printing"], printer=self.printer)
        stuck, active = job.plates.order_by("plate_number")
        self.PrintQueue.objects.create(plate=active, printer=self.printer, status="printing", started_at=self.t0)

        self._migrate()

        stuck.refresh_from_db()
        active.refresh_from_db()
        self.assertEqual(stuck.status, "waiting")
        self.assertEqual(active.status, "printing")

    def test_printed_quantity_unchanged_after_backfill(self) -> None:
        project = self.Project.objects.create(name="Legacy Project")
        part_full = self.Part.objects.create(name="Full", project=project, quantity=4)
        part_partial = self.Part.objects.create(name="Partial", project=project, quantity=4)
        full = self._job(["completed", "completed"], printer=self.printer)
        partial = self._job(["completed", "waiting"], printer=self.printer)
        self.PrintJobPart.objects.create(print_job=full, part=part_full, quantity=2)
        self.PrintJobPart.objects.create(print_job=partial, part=part_partial, quantity=3)
        plate_statuses = dict(self.PrintJobPlate.objects.values_list("pk", "status"))

        self._migrate(executor_leaf())

        from core.models import Part as CurrentPart
        from core.models import PrintJobPlate as CurrentPlate

        self.assertEqual(dict(CurrentPlate.objects.values_list("pk", "status")), plate_statuses)
        self.assertEqual(CurrentPart.objects.get(pk=part_full.pk).printed_quantity, 2)
        self.assertEqual(CurrentPart.objects.get(pk=part_partial.pk).printed_quantity, 0)


def executor_leaf() -> list[tuple[str, str]]:
    """Return the leaf migration nodes (the current schema)."""
    return MigrationExecutor(connection).loader.graph.leaf_nodes()


class PrintAttemptRollbackTests(TransactionTestCase):
    """Rolling back below the print-attempt migrations is refused while history exists."""

    def tearDown(self) -> None:
        """Make sure later tests see the current schema."""
        executor = MigrationExecutor(connection)
        executor.loader.build_graph()
        executor.migrate(executor.loader.graph.leaf_nodes())
        super().tearDown()

    def test_rollback_with_history_is_refused(self) -> None:
        from django.db.migrations.exceptions import IrreversibleError

        from core.models import PrinterProfile, PrintJob, PrintJobPlate, PrintQueue

        printer = PrinterProfile.objects.create(name="Rollback Printer")
        plate = PrintJobPlate.objects.create(print_job=PrintJob.objects.create(name="Rollback"), plate_number=1)
        PrintQueue.objects.create(plate=plate, printer=printer, result="passed", resolved_at=timezone.now())

        executor = MigrationExecutor(connection)
        with self.assertRaises(IrreversibleError):
            executor.migrate(BEFORE)
        PrintQueue.objects.all().delete()
