"""Tests for the plate-part object-ID backfill (``0045_backfill_plate_part_object_ids``).

Legacy plate parts (recorded before ``object_ids`` existed) are built at ``0044``
with realistic G-code files in a temporary ``MEDIA_ROOT``; the real migration
must fill the bundle object IDs where the G-code fits and leave them empty
otherwise (missing file, labels that do not fit).
"""

from __future__ import annotations

import importlib
import os
import shutil
import struct
import tempfile
from datetime import timedelta
from pathlib import Path

from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase, override_settings
from django.utils import timezone

from core.tests.gcode_fixtures import plate_gcode

BEFORE = [("core", "0044_excluded_objects")]
BACKFILL = [("core", "0045_backfill_plate_part_object_ids")]


def _binary_stl() -> bytes:
    facet = struct.pack("<12fH", 0, 0, 1, 0, 0, 0, 1, 0, 0, 0, 1, 0, 0)
    return b"\0" * 80 + struct.pack("<I", 1) + facet


class ObjectIdsBackfillTests(TransactionTestCase):
    """Run ``0045`` on plate parts built at ``0044``."""

    def setUp(self) -> None:
        super().setUp()
        self.media = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.media, ignore_errors=True)
        settings_override = override_settings(MEDIA_ROOT=str(self.media))
        settings_override.enable()
        self.addCleanup(settings_override.disable)
        (self.media / "stl_files").mkdir()
        (self.media / "gcode_jobs").mkdir()

        executor = MigrationExecutor(connection)
        executor.migrate(BEFORE)
        apps = executor.loader.project_state(BEFORE).apps
        self.PrintJob = apps.get_model("core", "PrintJob")
        self.PrintJobPlate = apps.get_model("core", "PrintJobPlate")
        self.PrintJobPart = apps.get_model("core", "PrintJobPart")
        self.PrintJobPlatePart = apps.get_model("core", "PrintJobPlatePart")
        self.Project = apps.get_model("core", "Project")
        self.Part = apps.get_model("core", "Part")
        self.project = self.Project.objects.create(name="Legacy Project")
        self.model_mtime = (timezone.now() - timedelta(days=30)).timestamp()

    def tearDown(self) -> None:
        executor = MigrationExecutor(connection)
        executor.loader.build_graph()
        executor.migrate(executor.loader.graph.leaf_nodes())
        super().tearDown()

    def _run(self) -> None:
        executor = MigrationExecutor(connection)
        executor.loader.build_graph()
        executor.migrate(BACKFILL)

    def _part(self, name: str, *, with_file: bool = False):
        part = self.Part.objects.create(name=name, project=self.project, quantity=10)
        if with_file:
            path = self.media / "stl_files" / f"{name}.stl"
            path.write_bytes(_binary_stl())
            os.utime(path, (self.model_mtime, self.model_mtime))
            part.stl_file.name = f"stl_files/{name}.stl"
            part.save()
        return part

    def _job(self, parts: list[tuple[object, int]], plates: list[bytes | None]):
        job = self.PrintJob.objects.create(name="Legacy", status="sliced")
        for part, quantity in parts:
            self.PrintJobPart.objects.create(print_job=job, part=part, quantity=quantity)
        result = []
        for number, gcode in enumerate(plates, start=1):
            plate = self.PrintJobPlate.objects.create(print_job=job, plate_number=number)
            relative = f"gcode_jobs/job_{job.pk}_plate_{number}.gcode"
            if gcode is not None:
                (self.media / relative).write_bytes(gcode)
            plate.gcode_file.name = relative
            plate.save()
            result.append(plate)
        return job, result

    def _row(self, plate, part, copy_index: int, *, meshes_on_plate: int = 1, mesh_count: int = 1):
        return self.PrintJobPlatePart.objects.create(
            plate=plate, part=part, copy_index=copy_index, meshes_on_plate=meshes_on_plate, mesh_count=mesh_count
        )

    def _ids(self, row) -> list[int]:
        row.refresh_from_db()
        return row.object_ids

    def test_fills_object_ids_including_split_multi_mesh_copy(self) -> None:
        bolt = self._part("Bolt")
        frame = self._part("Frame")
        # Bundle: Bolt ×2 → IDs 1, 2; Frame ×1 with 2 meshes → IDs 3 (mesh 0), 4 (mesh 1).
        _job, (plate1, plate2) = self._job([(bolt, 2), (frame, 1)], [plate_gcode([1, 3]), plate_gcode([2, 4])])
        bolt0 = self._row(plate1, bolt, 0)
        frame_p1 = self._row(plate1, frame, 0, mesh_count=2)
        bolt1 = self._row(plate2, bolt, 1)
        frame_p2 = self._row(plate2, frame, 0, mesh_count=2)

        self._run()

        self.assertEqual(
            [self._ids(bolt0), self._ids(frame_p1), self._ids(bolt1), self._ids(frame_p2)], [[1], [3], [2], [4]]
        )

    def test_missing_gcode_leaves_that_plate_empty(self) -> None:
        bolt = self._part("Bolt")
        _job, (plate1, plate2) = self._job([(bolt, 2)], [None, plate_gcode([2], define=False)])
        row1 = self._row(plate1, bolt, 0)
        row2 = self._row(plate2, bolt, 1)

        self._run()

        self.assertEqual((self._ids(row1), self._ids(row2)), ([], [2]))

    def test_labels_that_do_not_fit_leave_the_plate_empty(self) -> None:
        bolt = self._part("Bolt")
        _job, (plate,) = self._job([(bolt, 2)], [plate_gcode([2])])
        row = self._row(plate, bolt, 0)  # recorded copy 0, but the G-code carries copy 1

        self._run()

        self.assertEqual(self._ids(row), [])

    def test_part_without_plate_parts_takes_the_mesh_count_from_its_file(self) -> None:
        unplaced = self._part("Unplaced", with_file=True)
        bolt = self._part("Bolt")
        # Bundle: Unplaced → ID 1 (on no plate), Bolt → ID 2.
        _job, (plate,) = self._job([(unplaced, 1), (bolt, 1)], [plate_gcode([2])])
        row = self._row(plate, bolt, 0)

        self._run()

        self.assertEqual(self._ids(row), [2])

    def test_unknown_mesh_count_leaves_the_job_empty(self) -> None:
        unplaced = self._part("Unplaced")  # no rows, no model file
        bolt = self._part("Bolt")
        _job, (plate,) = self._job([(unplaced, 1), (bolt, 1)], [plate_gcode([2])])
        row = self._row(plate, bolt, 0)

        self._run()

        self.assertEqual(self._ids(row), [])

    def test_idempotent(self) -> None:
        bolt = self._part("Bolt")
        _job, (plate,) = self._job([(bolt, 1)], [plate_gcode([1])])
        row = self._row(plate, bolt, 0)
        self._run()
        module = importlib.import_module("core.migrations.0045_backfill_plate_part_object_ids")
        executor = MigrationExecutor(connection)
        apps = executor.loader.project_state(BACKFILL).apps
        module.backfill_object_ids(apps, None)
        self.assertEqual(self._ids(row), [1])
