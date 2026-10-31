"""Tests for deriving the plate / job status (``0036_backfill_derived_plate_job_status`` + ``0037``).

The tests build prod-like legacy data at ``0035`` (plates and jobs still store
their print status), run the real migrations with
:class:`~django.db.migrations.executor.MigrationExecutor` and check that the
derived states match, and that no printed quantity or project progress drops.
"""

from __future__ import annotations

import importlib
from datetime import timedelta

from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase
from django.utils import timezone

BEFORE = [("core", "0035_print_attempts_irreversible")]
BACKFILL = [("core", "0036_backfill_derived_plate_job_status")]


def _leaf() -> list[tuple[str, str]]:
    """Return the leaf migration nodes (the current schema)."""
    return MigrationExecutor(connection).loader.graph.leaf_nodes()


class DerivedStatusMigrationTests(TransactionTestCase):
    """Run ``0036`` / ``0037`` on legacy data built at ``0035``."""

    def setUp(self) -> None:
        """Migrate back to ``0035`` and expose the historical models."""
        super().setUp()
        executor = MigrationExecutor(connection)
        executor.migrate(BEFORE)
        apps = executor.loader.project_state(BEFORE).apps
        self.PrintQueue = apps.get_model("core", "PrintQueue")
        self.PrintJob = apps.get_model("core", "PrintJob")
        self.PrintJobPlate = apps.get_model("core", "PrintJobPlate")
        self.PrintJobPart = apps.get_model("core", "PrintJobPart")
        self.PrinterProfile = apps.get_model("core", "PrinterProfile")
        self.Project = apps.get_model("core", "Project")
        self.Part = apps.get_model("core", "Part")
        self.printer = self.PrinterProfile.objects.create(name="Farm 1")
        self.t0 = timezone.now() - timedelta(days=2)
        self.project = self.Project.objects.create(name="Legacy Project")

    def tearDown(self) -> None:
        """Migrate forward to the leaf nodes so later tests see the current schema."""
        executor = MigrationExecutor(connection)
        executor.loader.build_graph()
        executor.migrate(executor.loader.graph.leaf_nodes())
        super().tearDown()

    def _migrate(self, target: list[tuple[str, str]]) -> None:
        executor = MigrationExecutor(connection)
        executor.loader.build_graph()
        executor.migrate(target)

    def _job(self, status: str, plates: list[str], *, attempts: bool = True, printer=None):
        """Create a legacy job; ``attempts`` backs completed/failed plates with attempts (as after 0033)."""
        job = self.PrintJob.objects.create(
            name=f"Legacy {status}", status=status, printer=printer, started_at=self.t0 if plates else None
        )
        for number, plate_status in enumerate(plates, start=1):
            plate = self.PrintJobPlate.objects.create(
                print_job=job,
                plate_number=number,
                status=plate_status,
                started_at=self.t0 if plate_status != "waiting" else None,
                completed_at=self.t0 + timedelta(hours=number) if plate_status == "completed" else None,
            )
            if not attempts:
                continue
            if plate_status == "completed":
                self._attempt(plate, "passed", resolved_at=plate.completed_at)
            elif plate_status == "failed":
                for index in range(3):
                    self._attempt(plate, "failed", is_retry=index > 0)
            elif plate_status == "printing":
                self._attempt(plate, "", started_at=self.t0, completed_at=None)
        return job

    def _attempt(self, plate, result: str, **kwargs):
        values = {
            "plate": plate,
            "printer": self.printer,
            "result": result,
            "started_at": self.t0,
            "completed_at": self.t0 + timedelta(minutes=30),
            "resolved_at": self.t0 + timedelta(hours=1) if result else None,
            **kwargs,
        }
        return self.PrintQueue.objects.create(**values)

    def _part(self, name: str, job, quantity: int, need: int = 10):
        part = self.Part.objects.create(name=name, project=self.project, quantity=need)
        self.PrintJobPart.objects.create(print_job=job, part=part, quantity=quantity)
        return part

    def _legacy_printed(self) -> dict[int, int]:
        """Printed quantity per part by the legacy rule (all plates stored ``completed``)."""
        printed: dict[int, int] = {}
        for entry in self.PrintJobPart.objects.all():
            plates = list(self.PrintJobPlate.objects.filter(print_job_id=entry.print_job_id))
            done = bool(plates) and all(plate.status == "completed" for plate in plates)
            printed[entry.part_id] = printed.get(entry.part_id, 0) + (entry.quantity if done else 0)
        return printed

    def _build_prod_like_data(self) -> dict[str, object]:
        """Legacy jobs in every print status, incl. an inconsistent (admin-edited) plate."""
        jobs = {
            "done": self._job("completed", ["completed", "completed"], printer=self.printer),
            "partial": self._job("printing", ["completed", "waiting"]),
            "printing": self._job("printing", ["completed", "printing"]),
            "failed_plate": self._job("printing", ["completed", "failed"]),
            "edited": self._job("completed", ["completed"], attempts=False, printer=self.printer),
            "edited_failed": self._job("printing", ["failed"], attempts=False),
            "ready": self._job("sliced", ["waiting"]),
            "uploaded": self._job("uploaded", ["waiting"]),
            "cancelled": self._job("cancelled", ["waiting"]),
            "uploading": self._job("uploading", ["waiting"]),
            "draft": self._job("draft", []),
            "slicing_failed": self._job("failed", []),
        }
        for index, (key, job) in enumerate(jobs.items()):
            self._part(f"Part {key}", job, quantity=index + 1)
        return jobs

    def test_job_print_statuses_become_sliced(self) -> None:
        jobs = self._build_prod_like_data()

        self._migrate(_leaf())

        from core.models import PrintJob

        expected_slicing = {
            "done": "sliced",
            "partial": "sliced",
            "printing": "sliced",
            "failed_plate": "sliced",
            "edited": "sliced",
            "edited_failed": "sliced",
            "ready": "sliced",
            "uploaded": "sliced",
            "cancelled": "sliced",
            "uploading": "sliced",
            "draft": "draft",
            "slicing_failed": "failed",
        }
        for key, status in expected_slicing.items():
            with self.subTest(job=key):
                self.assertEqual(PrintJob.objects.get(pk=jobs[key].pk).status, status)

    def test_derived_display_status_after_migration(self) -> None:
        jobs = self._build_prod_like_data()

        self._migrate(_leaf())

        from core.models import PrintJob

        expected = {
            "done": "completed",
            "partial": "in_progress",
            "printing": "printing",
            "failed_plate": "partially_failed",
            "edited": "completed",
            "edited_failed": "partially_failed",
            "ready": "ready",
            "uploaded": "ready",
            "cancelled": "ready",
            "uploading": "ready",
            "draft": "draft",
            "slicing_failed": "slicing_failed",
        }
        annotated = {job.pk: job.annotated_display_status for job in PrintJob.objects.with_display_status()}
        for key, display in expected.items():
            with self.subTest(job=key):
                self.assertEqual(PrintJob.objects.get(pk=jobs[key].pk).display_status, display)
                self.assertEqual(annotated[jobs[key].pk], display)

    def test_plate_states_match_stored_status(self) -> None:
        self._build_prod_like_data()
        stored = dict(self.PrintJobPlate.objects.values_list("pk", "status"))

        self._migrate(_leaf())

        from core.models import PrintJobPlate

        mapping = {"completed": "completed", "failed": "failed", "printing": "printing", "waiting": "not_queued"}
        for plate in PrintJobPlate.objects.all():
            with self.subTest(plate=plate.pk):
                self.assertEqual(plate.state, mapping[stored[plate.pk]])

    def test_inconsistent_completed_plate_gets_passed_attempt(self) -> None:
        job = self._job("completed", ["completed"], attempts=False, printer=self.printer)
        plate = job.plates.get()

        self._migrate(BACKFILL)

        (attempt,) = self.PrintQueue.objects.filter(plate=plate)
        self.assertEqual(attempt.result, "passed")
        self.assertEqual(attempt.printer_id, self.printer.pk)
        self.assertEqual(attempt.resolved_at, plate.completed_at)
        self.assertFalse(attempt.is_retry)

    def test_inconsistent_failed_plate_gets_missing_failures(self) -> None:
        job = self._job("printing", ["failed"], attempts=False)
        plate = job.plates.get()
        self._attempt(plate, "failed", max_retries=3)  # one manual failure only

        self._migrate(BACKFILL)

        results = list(self.PrintQueue.objects.filter(plate=plate).order_by("pk").values_list("result", "is_retry"))
        self.assertEqual(results, [("failed", False), ("failed", True), ("failed", True)])

    def test_printed_quantity_and_progress_do_not_drop(self) -> None:
        """Review Focus 5: no printed quantity or project progress drops through the migration."""
        self._build_prod_like_data()
        legacy = self._legacy_printed()

        self._migrate(_leaf())

        from core.models import Part, Project

        for part in Part.objects.filter(project_id=self.project.pk):
            with self.subTest(part=part.name):
                self.assertGreaterEqual(part.printed_quantity, legacy[part.pk])
                self.assertEqual(part.printed_quantity, legacy[part.pk])
        project = Project.objects.prefetch_related(*Project.aggregate_prefetch_lookups()).get(pk=self.project.pk)
        self.assertEqual(project.build_progress()["printed"], sum(min(v, 10) for v in legacy.values()))

    def test_backfill_is_idempotent(self) -> None:
        self._build_prod_like_data()
        module = importlib.import_module("core.migrations.0036_backfill_derived_plate_job_status")

        self._migrate(BACKFILL)
        executor = MigrationExecutor(connection)
        apps = executor.loader.project_state(BACKFILL).apps
        count = self.PrintQueue.objects.count()
        module.backfill(apps, None)
        self.assertEqual(self.PrintQueue.objects.count(), count)

    def test_rollback_restores_stored_status_from_attempts(self) -> None:
        jobs = self._build_prod_like_data()
        stored_plates = dict(self.PrintJobPlate.objects.values_list("pk", "status"))

        self._migrate(_leaf())
        self._migrate(BEFORE)

        executor = MigrationExecutor(connection)
        apps = executor.loader.project_state(BEFORE).apps
        PrintJob = apps.get_model("core", "PrintJob")
        PrintJobPlate = apps.get_model("core", "PrintJobPlate")
        self.assertEqual(dict(PrintJobPlate.objects.values_list("pk", "status")), stored_plates)
        self.assertEqual(PrintJob.objects.get(pk=jobs["done"].pk).status, "completed")
        self.assertEqual(PrintJob.objects.get(pk=jobs["done"].pk).printer_id, self.printer.pk)
        self.assertEqual(PrintJob.objects.get(pk=jobs["printing"].pk).status, "printing")
        self.assertEqual(PrintJob.objects.get(pk=jobs["ready"].pk).status, "sliced")
        self.assertEqual(PrintJob.objects.get(pk=jobs["draft"].pk).status, "draft")
        done_plate = PrintJobPlate.objects.filter(print_job_id=jobs["done"].pk).order_by("plate_number").last()
        self.assertIsNotNone(done_plate.completed_at)
