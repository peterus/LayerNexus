"""Tests for the plate-parts backfill (``0040_backfill_plate_parts``).

The tests build legacy jobs at ``0039`` (plates with G-code files but no plate
parts yet), write realistic G-code snippets and model files into a temporary
``MEDIA_ROOT``, run the real migration and check the recorded plate parts —
and that no printed quantity drops (Review Focus 5).
"""

from __future__ import annotations

import importlib
import io
import os
import shutil
import struct
import tempfile
import zipfile
from datetime import timedelta
from pathlib import Path

from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase, override_settings
from django.utils import timezone

from core.tests.gcode_fixtures import plate_gcode

BEFORE = [("core", "0039_plate_parts")]
BACKFILL = [("core", "0040_backfill_plate_parts")]

_NS = "http://schemas.microsoft.com/3dmanufacturing/core/2015/02"
_MESH = (
    '<object id="{id}" type="model"><mesh><vertices><vertex x="0" y="0" z="0"/><vertex x="1" y="0" z="0"/>'
    '<vertex x="0" y="1" z="0"/></vertices><triangles><triangle v1="0" v2="1" v3="2"/></triangles></mesh></object>'
)


def _binary_stl() -> bytes:
    facet = struct.pack("<12fH", 0, 0, 1, 0, 0, 0, 1, 0, 0, 0, 1, 0, 0)
    return b"\0" * 80 + struct.pack("<I", 1) + facet


def _two_mesh_3mf() -> bytes:
    meshes = f"{_MESH.format(id=1)}{_MESH.format(id=2)}"
    xml = f'<?xml version="1.0"?><model xmlns="{_NS}"><resources>{meshes}</resources></model>'
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        zf.writestr("3D/3dmodel.model", xml)
    return buffer.getvalue()


class PlatePartsBackfillTests(TransactionTestCase):
    """Run ``0040`` on legacy jobs built at ``0039``."""

    def setUp(self) -> None:
        """Migrate back to ``0039``, expose the historical models and a temporary MEDIA_ROOT."""
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
        self.PrintQueue = apps.get_model("core", "PrintQueue")
        self.PrintJob = apps.get_model("core", "PrintJob")
        self.PrintJobPlate = apps.get_model("core", "PrintJobPlate")
        self.PrintJobPart = apps.get_model("core", "PrintJobPart")
        self.PrintJobPlatePart = apps.get_model("core", "PrintJobPlatePart")
        self.Project = apps.get_model("core", "Project")
        self.Part = apps.get_model("core", "Part")
        self.project = self.Project.objects.create(name="Legacy Project")
        self.t0 = timezone.now() - timedelta(days=2)
        # Model files were uploaded before slicing: make them older than any G-code.
        self.model_mtime = (timezone.now() - timedelta(days=30)).timestamp()

    def tearDown(self) -> None:
        """Migrate forward to the leaf nodes so later tests see the current schema."""
        executor = MigrationExecutor(connection)
        executor.loader.build_graph()
        executor.migrate(executor.loader.graph.leaf_nodes())
        super().tearDown()

    # ── Fixtures ────────────────────────────────────────────────────────

    def _migrate(self, target: list[tuple[str, str]]) -> None:
        executor = MigrationExecutor(connection)
        executor.loader.build_graph()
        executor.migrate(target)

    def _part(self, name: str, *, data: bytes | None = None, suffix: str = "stl", need: int = 50):
        part = self.Part.objects.create(name=name, project=self.project, quantity=need)
        path = self.media / "stl_files" / f"{name}.{suffix}"
        path.write_bytes(data if data is not None else _binary_stl())
        os.utime(path, (self.model_mtime, self.model_mtime))
        part.stl_file.name = f"stl_files/{name}.{suffix}"
        part.save()
        return part

    def _job(self, name: str, parts: list[tuple[object, int]], plates: list[bytes | None], results=()):
        """Create a sliced legacy job; ``plates`` are G-code bytes (``None`` = file missing).

        ``results`` gives per plate the attempt result (``"passed"``, ``"failed"``,
        ``"open"`` or ``None`` = no attempt).
        """
        job = self.PrintJob.objects.create(name=name, status="sliced")
        for part, quantity in parts:
            self.PrintJobPart.objects.create(print_job=job, part=part, quantity=quantity)
        for number, gcode in enumerate(plates, start=1):
            plate = self.PrintJobPlate.objects.create(print_job=job, plate_number=number)
            relative = f"gcode_jobs/job_{job.pk}_plate_{number}.gcode"
            if gcode is not None:
                (self.media / relative).write_bytes(gcode)
            plate.gcode_file.name = relative
            plate.save()
            result = results[number - 1] if number - 1 < len(results) else None
            if result is not None:
                self._attempt(plate, result)
        return job

    def _attempt(self, plate, result: str) -> None:
        if result == "open":
            self.PrintQueue.objects.create(plate=plate, started_at=self.t0)
            return
        self.PrintQueue.objects.create(
            plate=plate,
            result=result,
            started_at=self.t0,
            completed_at=self.t0 + timedelta(minutes=30),
            resolved_at=self.t0 + timedelta(hours=1),
        )

    def _entries(self, job) -> list[tuple[int, str, int, int, int]]:
        return sorted(
            (e.plate.plate_number, e.part.name, e.copy_index, e.meshes_on_plate, e.mesh_count)
            for e in self.PrintJobPlatePart.objects.filter(plate__print_job=job).select_related("plate", "part")
        )

    def _known(self, job) -> bool:
        return self.PrintJob.objects.get(pk=job.pk).plate_parts_known

    # ── Mapping ─────────────────────────────────────────────────────────

    def test_backfill_maps_objects_in_pk_order(self) -> None:
        """Objects are numbered in ``PrintJobPart`` pk order (not by name)."""
        zeta = self._part("Zeta")
        alpha = self._part("Alpha")
        job = self._job("J", [(zeta, 2), (alpha, 1)], [plate_gcode([1, 3]), plate_gcode([2])])

        self._migrate(BACKFILL)

        self.assertTrue(self._known(job))
        self.assertEqual(
            self._entries(job),
            [(1, "Alpha", 0, 1, 1), (1, "Zeta", 0, 1, 1), (2, "Zeta", 1, 1, 1)],
        )

    def test_backfill_multi_mesh_part(self) -> None:
        """A two-mesh 3MF part: objects run mesh-major; a copy may be split across plates."""
        multi = self._part("Multi", data=_two_mesh_3mf(), suffix="3mf")
        # Multi ×2, two meshes → objects 1, 2 (mesh 0 of copies 0, 1), 3, 4 (mesh 1 of copies 0, 1).
        job = self._job("J", [(multi, 2)], [plate_gcode([1, 3, 2]), plate_gcode([4])])

        self._migrate(BACKFILL)

        self.assertEqual(
            self._entries(job),
            [(1, "Multi", 0, 2, 2), (1, "Multi", 1, 1, 2), (2, "Multi", 1, 1, 2)],
        )

    def test_backfill_single_plate_without_labels_maps_all(self) -> None:
        """Review focus 4: a single unlabelled plate gets every copy."""
        part = self._part("P")
        job = self._job("J", [(part, 3)], [plate_gcode([1, 2, 3], define=False, comments=False)])

        self._migrate(BACKFILL)

        self.assertTrue(self._known(job))
        self.assertEqual(len(self._entries(job)), 3)

    # ── Unknown mapping ─────────────────────────────────────────────────

    def test_backfill_missing_file_marks_unknown(self) -> None:
        """A missing G-code file → ``plate_parts_known`` False, no plate parts."""
        part = self._part("P")
        job = self._job("J", [(part, 2)], [plate_gcode([1]), None])

        self._migrate(BACKFILL)

        self.assertFalse(self._known(job))
        self.assertEqual(self._entries(job), [])

    def test_backfill_missing_model_file_marks_unknown(self) -> None:
        """A missing model file (mesh count unknown) → unknown."""
        part = self._part("P")
        job = self._job("J", [(part, 1)], [plate_gcode([1])])
        (self.media / "stl_files" / "P.stl").unlink()

        self._migrate(BACKFILL)

        self.assertFalse(self._known(job))

    def test_backfill_broken_model_file_does_not_block_migrate(self) -> None:
        """Any parser error on a stored upload marks the job unknown instead of failing the migration."""
        from unittest import mock

        part = self._part("P", data=_two_mesh_3mf(), suffix="3mf")
        job = self._job("J", [(part, 1)], [plate_gcode([1, 2])])

        with mock.patch("core.services.threemf.read_model_meshes", side_effect=RuntimeError("encrypted zip")):
            self._migrate(BACKFILL)

        self.assertFalse(self._known(job))

    def test_backfill_parses_each_3mf_once_and_never_stl(self) -> None:
        """Mesh counts are memoized across jobs; STL files are not parsed again."""
        from unittest import mock

        from core.services import threemf

        multi = self._part("Multi", data=_two_mesh_3mf(), suffix="3mf")
        stl = self._part("S")
        for name in ("J1", "J2", "J3"):
            self._job(name, [(multi, 1), (stl, 1)], [plate_gcode([1, 2, 3])])

        with mock.patch.object(threemf, "read_model_meshes", wraps=threemf.read_model_meshes) as spy:
            self._migrate(BACKFILL)

        self.assertEqual([Path(call.args[0]).name for call in spy.call_args_list], ["Multi.3mf"])
        self.assertEqual(self.PrintJobPlatePart.objects.count(), 6)

    def test_backfill_empty_gcode_file(self) -> None:
        """An empty G-code file (no labels) on a single plate falls back to 'all objects on this plate'."""
        part = self._part("P")
        job = self._job("J", [(part, 2)], [b""])

        self._migrate(BACKFILL)

        self.assertTrue(self._known(job))
        self.assertEqual(len(self._entries(job)), 2)

    def test_backfill_count_mismatch_marks_unknown(self) -> None:
        """An object ID beyond Σ quantity × meshes → the bundle order does not fit → unknown."""
        part = self._part("P")
        job = self._job("J", [(part, 2)], [plate_gcode([1, 2]), plate_gcode([3])])

        self._migrate(BACKFILL)

        self.assertFalse(self._known(job))
        self.assertEqual(self._entries(job), [])

    def test_backfill_multi_plate_without_labels_marks_unknown(self) -> None:
        """Several unlabelled plates cannot be attributed."""
        part = self._part("P")
        unlabelled = plate_gcode([1], define=False, comments=False)
        job = self._job("J", [(part, 2)], [unlabelled, unlabelled])

        self._migrate(BACKFILL)

        self.assertFalse(self._known(job))

    def test_backfill_model_replaced_after_slicing_marks_unknown(self) -> None:
        """A model file newer than the job's G-code may have another mesh count → unknown."""
        part = self._part("P")
        job = self._job("J", [(part, 2)], [plate_gcode([1, 2])])
        future = (timezone.now() + timedelta(days=1)).timestamp()
        os.utime(self.media / "stl_files" / "P.stl", (future, future))

        self._migrate(BACKFILL)

        self.assertFalse(self._known(job))

    # ── Unplaced copies (Job 41) ────────────────────────────────────────

    def _job41(self, results=()):
        """Job 41 shape: 25 copies, objects 12 and 13 (2× R11) on no plate."""
        r10 = self._part("R10")
        r11 = self._part("R11")
        r12 = self._part("R12")
        plates = [plate_gcode(list(range(1, 12))), plate_gcode(list(range(14, 26)))]
        # R10 ×11 → objects 1–11, R11 ×4 → 12–15, R12 ×10 → 16–25.
        return self._job("Job 41", [(r10, 11), (r11, 4), (r12, 10)], plates, results), r11

    def test_backfill_leaves_unplaced_copies_visible(self) -> None:
        """Unplaced copies are no error: the plate parts are stored and the job warns about them."""
        job, r11 = self._job41(results=("passed", None))

        self._migrate(BACKFILL)

        self.assertTrue(self._known(job))
        self.assertEqual(len(self._entries(job)), 23)

        from core.models import PrintJob

        current = PrintJob.objects.get(pk=job.pk)
        self.assertEqual(current.unplaced_copies(), {r11.pk: 2})
        self.assertEqual(current.plate_assignment_warnings(), ["2× R11 on no plate"])

    def test_completed_job_with_unplaced_copies_keeps_legacy_count(self) -> None:
        """A completed job with unplaced copies keeps the plate parts (warning) but counts by the legacy rule."""
        job, r11 = self._job41(results=("passed", "passed"))

        self._migrate(BACKFILL)

        self.assertFalse(self._known(job))
        self.assertEqual(len(self._entries(job)), 23)

        from core.models import PrintJob

        self.assertEqual(PrintJob.objects.get(pk=job.pk).plate_assignment_warnings(), ["2× R11 on no plate"])

    # ── Prod-like data: counts never drop (Review Focus 5) ──────────────

    def _legacy_printed(self) -> dict[int, int]:
        """Printed quantity per part by the legacy rule (all plates have a passed attempt)."""
        printed: dict[int, int] = {}
        for entry in self.PrintJobPart.objects.all():
            plates = list(self.PrintJobPlate.objects.filter(print_job_id=entry.print_job_id))
            done = bool(plates) and all(
                self.PrintQueue.objects.filter(plate=plate, result="passed").exists() for plate in plates
            )
            printed[entry.part_id] = printed.get(entry.part_id, 0) + (entry.quantity if done else 0)
        return printed

    def _plate_rule_printed(self) -> dict[int, int]:
        """Printed quantity per part as PR-E will count it from the backfilled data.

        Jobs with ``plate_parts_known``: copies whose meshes all lie on plates with
        a passed attempt.  Other jobs: the legacy rule.
        """
        printed: dict[int, int] = {}
        for job in self.PrintJob.objects.all():
            entries = list(self.PrintJobPart.objects.filter(print_job=job))
            plates = list(self.PrintJobPlate.objects.filter(print_job=job))
            passed = {p.pk for p in plates if self.PrintQueue.objects.filter(plate=p, result="passed").exists()}
            if not job.plate_parts_known:
                done = bool(plates) and len(passed) == len(plates)
                for entry in entries:
                    printed[entry.part_id] = printed.get(entry.part_id, 0) + (entry.quantity if done else 0)
                continue
            meshes: dict[tuple[int, int], int] = {}
            mesh_count: dict[tuple[int, int], int] = {}
            for row in self.PrintJobPlatePart.objects.filter(plate__print_job=job):
                key = (row.part_id, row.copy_index)
                meshes[key] = meshes.get(key, 0) + (row.meshes_on_plate if row.plate_id in passed else 0)
                mesh_count[key] = row.mesh_count
            for entry in entries:
                printed.setdefault(entry.part_id, 0)
            for (part_id, _copy), count in meshes.items():
                if count >= mesh_count[(part_id, _copy)]:
                    printed[part_id] = printed.get(part_id, 0) + 1
        return printed

    def _build_prod_like_data(self) -> None:
        """Completed, partial, failed, open (waiting / printing / review), missing-file and Job-41 jobs."""
        a, b, c = self._part("A"), self._part("B"), self._part("C")
        multi = self._part("Multi", data=_two_mesh_3mf(), suffix="3mf")
        self._job("done", [(a, 2), (b, 1)], [plate_gcode([1, 3]), plate_gcode([2])], ("passed", "passed"))
        self._job("partial", [(a, 2), (c, 2)], [plate_gcode([1, 2]), plate_gcode([3, 4])], ("passed", None))
        self._job("failed plate", [(b, 3)], [plate_gcode([1]), plate_gcode([2, 3])], ("passed", "failed"))
        self._job("waiting", [(c, 1)], [plate_gcode([1])], ("open",))
        self._job("printing", [(a, 1), (c, 1)], [plate_gcode([1]), plate_gcode([2])], ("passed", "open"))
        self._job("missing file", [(a, 4)], [plate_gcode([1, 2]), None], ("passed", "passed"))
        self._job("multi split", [(multi, 1)], [plate_gcode([1]), plate_gcode([2])], ("passed", "passed"))
        self._job41(results=("passed", "passed"))

    def test_printed_quantity_never_drops(self) -> None:
        """Review focus 5: per part, the plate-based count is never below the legacy count."""
        self._build_prod_like_data()
        legacy = self._legacy_printed()

        self._migrate(BACKFILL)

        plate_rule = self._plate_rule_printed()
        for part_id, count in legacy.items():
            self.assertGreaterEqual(plate_rule.get(part_id, 0), count, f"part {part_id}")
        # Partially printed jobs now count their passed plates (more correct, never lower).
        self.assertGreater(sum(plate_rule.values()), sum(legacy.values()))

    def test_current_printed_quantity_follows_plate_rule(self) -> None:
        """After all migrations, ``Part.printed_quantity`` (PR-E) counts by the plate rule, never below legacy."""
        self._build_prod_like_data()
        legacy = self._legacy_printed()

        self._migrate(BACKFILL)
        plate_rule = self._plate_rule_printed()
        executor = MigrationExecutor(connection)
        executor.loader.build_graph()
        self._migrate(executor.loader.graph.leaf_nodes())

        from core.models import Part

        for part in Part.objects.all():
            self.assertEqual(part.printed_quantity, plate_rule.get(part.pk, 0), part.name)
            self.assertGreaterEqual(part.printed_quantity, legacy.get(part.pk, 0), part.name)

    def test_backfill_is_idempotent(self) -> None:
        """Running the backfill again creates no duplicates and keeps the flags."""
        self._build_prod_like_data()
        module = importlib.import_module("core.migrations.0040_backfill_plate_parts")

        self._migrate(BACKFILL)
        executor = MigrationExecutor(connection)
        apps = executor.loader.project_state(BACKFILL).apps
        count = self.PrintJobPlatePart.objects.count()
        flags = dict(self.PrintJob.objects.values_list("pk", "plate_parts_known"))
        module.backfill_plate_parts(apps, None)

        self.assertEqual(self.PrintJobPlatePart.objects.count(), count)
        self.assertEqual(dict(self.PrintJob.objects.values_list("pk", "plate_parts_known")), flags)
