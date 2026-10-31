"""Tests for ``Part.printed_quantity``: copies on passed plates plus manual records.

A copy counts once its meshes all lie on plates with a passed attempt; jobs whose
plate assignment is unknown keep the legacy all-plates rule; manual
``PrintedPartRecord`` bookings add up.  The prefetched and the database path
must agree, and no count may be lower than under the legacy rule.
"""

from __future__ import annotations

from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TestCase, TransactionTestCase

from core.models import Part, PrintedPartRecord, PrintJob, PrintJobPart, PrintJobPlatePart, Project
from core.tests.mixins import add_attempt, complete_plate, fail_plate, make_sliced_job


def _place(plate, part: Part, *copies: int, meshes_on_plate: int = 1, mesh_count: int = 1) -> None:
    """Record copies of ``part`` on ``plate``."""
    for copy_index in copies:
        PrintJobPlatePart.objects.create(
            plate=plate, part=part, copy_index=copy_index, meshes_on_plate=meshes_on_plate, mesh_count=mesh_count
        )


def _legacy_printed(part: Part) -> int:
    """The pre-PR-E rule: a job counts its full quantity once every plate is completed."""
    total = 0
    for entry in PrintJobPart.objects.filter(part=part):
        plates = list(entry.print_job.plates.all())
        if plates and all(plate.queue_entries.filter(result="passed").exists() for plate in plates):
            total += entry.quantity
    return total


def _prefetched(part: Part) -> Part:
    """Reload ``part`` with the full printed-quantity prefetch."""
    return Part.objects.prefetch_related(*Part.printed_quantity_prefetch_lookups()).get(pk=part.pk)


class PrintedQuantityTests(TestCase):
    """The counting rule on both evaluation paths."""

    def setUp(self) -> None:
        self.project = Project.objects.create(name="P")
        self.part = Part.objects.create(name="R02", project=self.project, quantity=10)
        self.other = Part.objects.create(name="R05", project=self.project, quantity=10)

    def assertPrinted(self, part: Part, expected: int) -> None:  # noqa: N802 — unittest naming
        """Both paths give ``expected``; the prefetched one without any query."""
        self.assertEqual(Part.objects.get(pk=part.pk).printed_quantity, expected, "database path")
        prefetched = _prefetched(part)
        with self.assertNumQueries(0):
            self.assertEqual(prefetched.printed_quantity, expected, "prefetched path")

    def test_partially_printed_job_counts_passed_plates(self) -> None:
        """Job-33 case: plates 1+2 passed, plate 3 still open → their copies count."""
        job = make_sliced_job(plates=3)
        PrintJobPart.objects.create(print_job=job, part=self.part, quantity=4)
        PrintJobPart.objects.create(print_job=job, part=self.other, quantity=1)
        plate1, plate2, plate3 = job.plates.order_by("plate_number")
        _place(plate1, self.part, 0, 1)
        _place(plate2, self.part, 2)
        _place(plate3, self.part, 3)
        _place(plate3, self.other, 0)
        complete_plate(plate1)
        complete_plate(plate2)
        add_attempt(plate3, None, "printing")

        self.assertPrinted(self.part, 3)
        self.assertPrinted(self.other, 0)
        self.assertEqual(_legacy_printed(self.part), 0)

    def test_failed_plate_does_not_count(self) -> None:
        job = make_sliced_job(plates=2)
        PrintJobPart.objects.create(print_job=job, part=self.part, quantity=2)
        plate1, plate2 = job.plates.order_by("plate_number")
        _place(plate1, self.part, 0)
        _place(plate2, self.part, 1)
        complete_plate(plate1)
        fail_plate(plate2)
        self.assertPrinted(self.part, 1)

    def test_multi_mesh_copy_counts_only_when_all_meshes_passed(self) -> None:
        job = make_sliced_job(plates=2)
        PrintJobPart.objects.create(print_job=job, part=self.part, quantity=1)
        plate1, plate2 = job.plates.order_by("plate_number")
        _place(plate1, self.part, 0, meshes_on_plate=1, mesh_count=2)
        _place(plate2, self.part, 0, meshes_on_plate=1, mesh_count=2)
        complete_plate(plate1)
        self.assertPrinted(self.part, 0)

        complete_plate(plate2)
        self.assertPrinted(self.part, 1)

    def test_unknown_mapping_job_uses_legacy_rule(self) -> None:
        """``plate_parts_known=False``: only a fully completed job counts, then with its full quantity."""
        job = make_sliced_job(plates=2, plate_parts_known=False)
        PrintJobPart.objects.create(print_job=job, part=self.part, quantity=5)
        plate1, plate2 = job.plates.order_by("plate_number")
        # Plate parts kept for the warning (e.g. 2 copies on no plate) — not authoritative.
        _place(plate1, self.part, 0, 1)
        _place(plate2, self.part, 2)
        complete_plate(plate1)
        self.assertPrinted(self.part, 0)

        complete_plate(plate2)
        self.assertPrinted(self.part, 5)

    def test_job_without_plate_parts_uses_legacy_rule(self) -> None:
        """No plate parts recorded at all (nothing to count by) → legacy rule, never zero for a done job."""
        job = make_sliced_job(plates=2)
        PrintJobPart.objects.create(print_job=job, part=self.part, quantity=3)
        plate1, plate2 = job.plates.order_by("plate_number")
        complete_plate(plate1)
        self.assertPrinted(self.part, 0)
        complete_plate(plate2)
        self.assertPrinted(self.part, 3)

    def test_unplaced_copies_of_known_job_do_not_count(self) -> None:
        """A newly sliced job counts honestly: copies on no plate were never printed."""
        job = make_sliced_job(plates=1)
        PrintJobPart.objects.create(print_job=job, part=self.part, quantity=3)
        (plate,) = job.plates.all()
        _place(plate, self.part, 0)
        complete_plate(plate)
        self.assertPrinted(self.part, 1)

    def test_part_absent_from_plates_of_known_job_counts_zero(self) -> None:
        job = make_sliced_job(plates=1)
        PrintJobPart.objects.create(print_job=job, part=self.part, quantity=2)
        PrintJobPart.objects.create(print_job=job, part=self.other, quantity=1)
        (plate,) = job.plates.all()
        _place(plate, self.other, 0)
        complete_plate(plate)
        self.assertPrinted(self.part, 0)
        self.assertPrinted(self.other, 1)

    def test_manual_records_add_up(self) -> None:
        PrintedPartRecord.objects.create(part=self.part, quantity=2)
        PrintedPartRecord.objects.create(part=self.part, quantity=3, note="spares")
        PrintedPartRecord.objects.create(part=self.other, quantity=7)
        job = make_sliced_job(plates=1)
        PrintJobPart.objects.create(print_job=job, part=self.part, quantity=1)
        _place(job.plates.get(), self.part, 0)
        complete_plate(job.plates.get())
        self.assertPrinted(self.part, 6)

    def test_prefetched_equals_db_path(self) -> None:
        """A mix of every case gives the same count on both paths (and over a project prefetch)."""
        partial = make_sliced_job(plates=2)
        PrintJobPart.objects.create(print_job=partial, part=self.part, quantity=2)
        p1, p2 = partial.plates.order_by("plate_number")
        _place(p1, self.part, 0)
        _place(p2, self.part, 1)
        complete_plate(p1)
        legacy = make_sliced_job(plates=1, plate_parts_known=False)
        PrintJobPart.objects.create(print_job=legacy, part=self.part, quantity=4)
        complete_plate(legacy.plates.get())
        draft = PrintJob.objects.create(name="draft")
        PrintJobPart.objects.create(print_job=draft, part=self.part, quantity=9)
        PrintedPartRecord.objects.create(part=self.part, quantity=2)

        self.assertPrinted(self.part, 1 + 4 + 2)
        project = Project.objects.prefetch_related(*Project.aggregate_prefetch_lookups()).get(pk=self.project.pk)
        with self.assertNumQueries(0):
            counts = {part.pk: part.printed_quantity for part in project.parts.all()}
        self.assertEqual(counts, {self.part.pk: 7, self.other.pk: 0})

    def test_db_path_query_count_independent_of_job_count(self) -> None:
        def queries_for(jobs: int) -> int:
            part = Part.objects.create(name=f"N{jobs}", project=self.project, quantity=1)
            for _ in range(jobs):
                job = make_sliced_job(plates=2)
                PrintJobPart.objects.create(print_job=job, part=part, quantity=1)
                for plate in job.plates.all():
                    _place(plate, part, 0, meshes_on_plate=1, mesh_count=2)
                    complete_plate(plate)
            fresh = Part.objects.get(pk=part.pk)
            from django.test.utils import CaptureQueriesContext

            with CaptureQueriesContext(connection) as captured:
                self.assertEqual(fresh.printed_quantity, jobs)
            return len(captured)

        self.assertEqual(queries_for(1), queries_for(5))

    def test_partial_prefetch_falls_back_to_db(self) -> None:
        """A prefetch without plate parts or records must not miscount (falls back to the DB path)."""
        job = make_sliced_job(plates=1)
        PrintJobPart.objects.create(print_job=job, part=self.part, quantity=2)
        _place(job.plates.get(), self.part, 0)
        complete_plate(job.plates.get())
        PrintedPartRecord.objects.create(part=self.part, quantity=1)
        part = Part.objects.prefetch_related("job_entries__print_job__plates").get(pk=self.part.pk)
        self.assertEqual(part.printed_quantity, 2)

    def test_progress_capped_at_need(self) -> None:
        """Over-booking one part (spares) never pushes the project past its need."""
        self.part.quantity = 2
        self.part.save()
        PrintedPartRecord.objects.create(part=self.part, quantity=5)
        PrintedPartRecord.objects.create(part=self.other, quantity=5)
        project = Project.objects.prefetch_related(*Project.aggregate_prefetch_lookups()).get(pk=self.project.pk)
        progress = project.build_progress()
        self.assertEqual(progress["needed"], 12)
        self.assertEqual(progress["printed"], 2 + 5)
        self.assertEqual(progress["percent"], 58)
        rows = {row["part"].pk: row for row in progress["parts"]}
        self.assertEqual(rows[self.part.pk]["printed"], 5)
        self.assertEqual(rows[self.part.pk]["remaining"], 0)

    def test_counts_not_lower_than_legacy_rule(self) -> None:
        """Review Focus 5: for prod-like jobs (all copies placed or mapping unknown) no count drops."""
        scenarios = [
            # (plates, placement per plate as copy indices, results per plate, known)
            (2, [[0, 1], [2]], ["passed", "passed"], True),
            (2, [[0], [1]], ["passed", "waiting"], True),
            (3, [[0], [1], [2]], ["passed", "failed", None], True),
            (2, [[0], []], ["passed", "passed"], False),
            (1, [[]], ["passed"], True),
            (2, [[0], [1]], [None, None], True),
        ]
        for plates, placement, results, known in scenarios:
            job = make_sliced_job(plates=plates, plate_parts_known=known)
            quantity = max([c for copies in placement for c in copies], default=0) + 1
            PrintJobPart.objects.create(print_job=job, part=self.part, quantity=quantity)
            for plate, copies, result in zip(job.plates.order_by("plate_number"), placement, results, strict=True):
                _place(plate, self.part, *copies)
                if result == "passed":
                    complete_plate(plate)
                elif result == "failed":
                    fail_plate(plate)
                elif result == "waiting":
                    add_attempt(plate, None, "waiting")
        self.assertGreaterEqual(Part.objects.get(pk=self.part.pk).printed_quantity, _legacy_printed(self.part))
        self.assertGreater(Part.objects.get(pk=self.part.pk).printed_quantity, _legacy_printed(self.part))


class KeepCountsMigrationTests(TransactionTestCase):
    """``0042_keep_counts_of_completed_jobs`` keeps completed jobs at their legacy count."""

    BEFORE = [("core", "0041_printed_part_records")]
    AFTER = [("core", "0042_keep_counts_of_completed_jobs")]

    def tearDown(self) -> None:
        executor = MigrationExecutor(connection)
        executor.loader.build_graph()
        executor.migrate(executor.loader.graph.leaf_nodes())
        super().tearDown()

    def _migrate(self, target: list[tuple[str, str]]) -> None:
        executor = MigrationExecutor(connection)
        executor.loader.build_graph()
        executor.migrate(target)

    def test_completed_job_with_unplaced_copies_keeps_legacy_count(self) -> None:
        self._migrate(self.BEFORE)
        project = Project.objects.create(name="P")
        part = Part.objects.create(name="R11", project=project, quantity=4)
        other = Part.objects.create(name="R10", project=project, quantity=4)

        done_unplaced = make_sliced_job("41", plates=2)
        PrintJobPart.objects.create(print_job=done_unplaced, part=part, quantity=4)
        p1, p2 = done_unplaced.plates.order_by("plate_number")
        _place(p1, part, 0)
        _place(p2, part, 1)  # copies 2 and 3 on no plate
        complete_plate(p1)
        complete_plate(p2)

        open_unplaced = make_sliced_job("open", plates=2)
        PrintJobPart.objects.create(print_job=open_unplaced, part=part, quantity=2)
        o1, _o2 = open_unplaced.plates.order_by("plate_number")
        _place(o1, part, 0)
        complete_plate(o1)

        done_complete = make_sliced_job("done", plates=1)
        PrintJobPart.objects.create(print_job=done_complete, part=other, quantity=2)
        _place(done_complete.plates.get(), other, 0, 1)
        complete_plate(done_complete.plates.get())

        legacy = {part.pk: _legacy_printed(part), other.pk: _legacy_printed(other)}
        self._migrate(self.AFTER)

        self.assertFalse(PrintJob.objects.get(pk=done_unplaced.pk).plate_parts_known)
        self.assertTrue(PrintJob.objects.get(pk=open_unplaced.pk).plate_parts_known)
        self.assertTrue(PrintJob.objects.get(pk=done_complete.pk).plate_parts_known)
        # 4 (legacy, completed job) + 1 (passed plate of the open job).
        self.assertEqual(Part.objects.get(pk=part.pk).printed_quantity, 5)
        self.assertEqual(Part.objects.get(pk=other.pk).printed_quantity, 2)
        for pk, count in legacy.items():
            self.assertGreaterEqual(Part.objects.get(pk=pk).printed_quantity, count)
