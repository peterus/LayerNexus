"""Tests for ``Part.deep_copy`` / ``Project.deep_copy`` (copy content, never history).

Field audit (``Part._meta.concrete_fields``) and the copy decision per field:

* copied: ``name``, ``project`` (set to the target), ``quantity``, ``stl_file`` (new
  file), ``color``, ``material``, ``spoolman_filament_id``, ``print_preset``,
  ``estimated_with_preset``, ``notes``, ``filament_used_grams``,
  ``filament_used_meters``, ``estimated_print_time``, ``estimation_status``,
  ``estimation_error``;
* not copied: ``id`` (new row), ``created_at`` / ``updated_at`` (set on insert).

Field audit (``Project._meta.concrete_fields``):

* copied: ``name`` (top node overridable), ``description``, ``image`` (new file),
  ``parent`` (set to the target), ``quantity`` (1 for a copy made a new root),
  ``default_print_preset`` (the effective one for a new root),
  ``created_by`` (set to the copying user);
* not copied: ``id``, ``created_at``, ``updated_at``.

Reverse relations: ``ProjectDocument`` (new files), ``ProjectHardware`` (same catalog
entry) and ``PrintTimeEstimate`` are copied; ``PrintJobPart``, ``FileVersion`` and
``HardwareShoppingItem`` (history) are not.
"""

import os
import shutil
import tempfile
from datetime import timedelta
from pathlib import Path
from unittest import mock

from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings

from core.models import (
    FileVersion,
    HardwarePart,
    HardwareShoppingItem,
    OrcaPrintPreset,
    Part,
    PrinterProfile,
    PrintJob,
    PrintJobPart,
    PrintJobPlate,
    PrintTimeEstimate,
    Project,
    ProjectDocument,
    ProjectHardware,
)

MEDIA_ROOT = tempfile.mkdtemp(prefix="layernexus-deepcopy-")

PART_COPIED = {
    "name",
    "project",
    "quantity",
    "stl_file",
    "color",
    "material",
    "spoolman_filament_id",
    "print_preset",
    "estimated_with_preset",
    "notes",
    "filament_used_grams",
    "filament_used_meters",
    "estimated_print_time",
    "estimation_status",
    "estimation_error",
}
PART_NOT_COPIED = {"id", "created_at", "updated_at"}

PROJECT_COPIED = {"name", "description", "image", "parent", "quantity", "default_print_preset", "created_by"}
PROJECT_NOT_COPIED = {"id", "created_at", "updated_at"}


def _media_files() -> set[str]:
    """Return every file below the test MEDIA_ROOT, relative to it."""
    root = Path(MEDIA_ROOT)
    return {str(p.relative_to(root)) for p in root.rglob("*") if p.is_file()}


def _preset(name: str) -> OrcaPrintPreset:
    """Create a resolved, instantiable print preset."""
    return OrcaPrintPreset.objects.create(
        name=name, orca_name=name, state=OrcaPrintPreset.STATE_RESOLVED, instantiation=True
    )


@override_settings(MEDIA_ROOT=MEDIA_ROOT)
class DeepCopyTests(TestCase):
    """R (preset A) → S (no preset) → T, with one part in each node."""

    @classmethod
    def tearDownClass(cls) -> None:
        super().tearDownClass()
        shutil.rmtree(MEDIA_ROOT, ignore_errors=True)

    def setUp(self) -> None:
        for name in os.listdir(MEDIA_ROOT):
            shutil.rmtree(os.path.join(MEDIA_ROOT, name), ignore_errors=True)
        self.user = User.objects.create_user(username="copier", password="pw")
        self.preset_a = _preset("A")
        self.preset_b = _preset("B")
        self.root = Project.objects.create(
            name="R",
            description="root desc",
            default_print_preset=self.preset_a,
            image=SimpleUploadedFile("cover.png", b"PNGDATA"),
        )
        self.sub = Project.objects.create(name="S", parent=self.root, quantity=2)
        self.leaf = Project.objects.create(name="T", parent=self.sub, quantity=3)
        self.part_r = Part.objects.create(
            name="r-part",
            project=self.root,
            quantity=4,
            color="red",
            material="PLA",
            spoolman_filament_id=7,
            notes="n",
            stl_file=SimpleUploadedFile("r.stl", b"solid r"),
            filament_used_grams=12.5,
            filament_used_meters=4.2,
            estimated_print_time=timedelta(hours=1),
            estimation_status=Part.ESTIMATION_SUCCESS,
            estimated_with_preset=self.preset_a,
        )
        self.part_s = Part.objects.create(name="s-part", project=self.sub, print_preset=self.preset_b)
        self.part_t = Part.objects.create(name="t-part", project=self.leaf, stl_file=SimpleUploadedFile("t.stl", b"t"))
        self.doc = ProjectDocument.objects.create(
            project=self.sub, name="Manual", file=SimpleUploadedFile("manual.pdf", b"%PDF"), uploaded_by=self.user
        )
        self.hardware = HardwarePart.objects.create(name="M3 bolt", category="bolts")
        self.hw = ProjectHardware.objects.create(project=self.leaf, hardware_part=self.hardware, quantity=5, notes="x")

    def test_all_part_fields_have_copy_decision(self) -> None:
        names = {f.name for f in Part._meta.concrete_fields}
        self.assertEqual(names, PART_COPIED | PART_NOT_COPIED)

    def test_all_project_fields_have_copy_decision(self) -> None:
        names = {f.name for f in Project._meta.concrete_fields}
        self.assertEqual(names, PROJECT_COPIED | PROJECT_NOT_COPIED)

    def test_deep_copy_recursion_depth_2_independent(self) -> None:
        copy = self.root.deep_copy(parent=None, created_by=self.user)
        self.assertNotEqual(copy.pk, self.root.pk)
        self.assertIsNone(copy.parent)
        self.assertEqual(copy.description, "root desc")
        self.assertEqual(copy.default_print_preset, self.preset_a)
        self.assertEqual(copy.created_by, self.user)
        new_sub = copy.subprojects.get()
        new_leaf = new_sub.subprojects.get()
        self.assertEqual((new_sub.name, new_sub.quantity), ("S", 2))
        self.assertEqual((new_leaf.name, new_leaf.quantity), ("T", 3))
        self.assertNotIn(new_sub.pk, {self.sub.pk, self.leaf.pk})
        self.assertEqual(Part.objects.filter(project__in=copy.subtree_ids()).count(), 3)
        new_part = copy.parts.get()
        self.assertNotEqual(new_part.pk, self.part_r.pk)
        for field in PART_COPIED - {"project", "stl_file"}:
            self.assertEqual(getattr(new_part, field), getattr(self.part_r, field), field)
        new_part.name = "changed"
        new_part.save()
        self.part_r.refresh_from_db()
        self.assertEqual(self.part_r.name, "r-part")
        self.assertEqual(self.sub.subprojects.get(), self.leaf)
        self.assertEqual(new_sub.parts.get().print_preset, self.preset_b)

    def test_deep_copy_duplicates_files(self) -> None:
        copy = self.root.deep_copy(parent=None)
        new_part = copy.parts.get()
        new_doc = ProjectDocument.objects.get(project__parent=copy)
        pairs = [
            (self.part_r.stl_file, new_part.stl_file),
            (self.doc.file, new_doc.file),
            (self.root.image, copy.image),
        ]
        for original, duplicate in pairs:
            self.assertNotEqual(original.name, duplicate.name)
            self.assertTrue(os.path.exists(original.path))
            self.assertTrue(os.path.exists(duplicate.path))
            with open(original.path, "rb") as a, open(duplicate.path, "rb") as b:
                self.assertEqual(a.read(), b.read())
        self.assertEqual((new_doc.name, new_doc.uploaded_by), ("Manual", self.user))
        self.part_r.stl_file.delete(save=False)
        self.assertTrue(os.path.exists(new_part.stl_file.path))

    def test_deep_copy_shares_hardware_catalog_not_rows(self) -> None:
        copy = self.root.deep_copy(parent=None)
        new_hw = ProjectHardware.objects.get(project__parent__parent=copy)
        self.assertEqual(new_hw.hardware_part_id, self.hardware.pk)
        self.assertNotEqual(new_hw.pk, self.hw.pk)
        self.assertEqual((new_hw.quantity, new_hw.notes), (5, "x"))
        self.assertEqual(HardwarePart.objects.count(), 1)

    def test_deep_copy_does_not_copy_history(self) -> None:
        job = PrintJob.objects.create(name="done", status=PrintJob.STATUS_COMPLETED)
        PrintJobPart.objects.create(print_job=job, part=self.part_r, quantity=4)
        PrintJobPlate.objects.create(print_job=job, plate_number=1, status=PrintJobPlate.STATUS_COMPLETED)
        FileVersion.objects.create(part=self.part_r, file=SimpleUploadedFile("v1.stl", b"v1"), file_type="stl")
        HardwareShoppingItem.objects.create(
            project=self.root, hardware_part=self.hardware, status=HardwareShoppingItem.STATUS_ORDERED, quantity=30
        )
        self.assertEqual(self.root.build_progress()["printed"], 4)
        copy = self.root.deep_copy(parent=None)
        new_part = copy.parts.get()
        self.assertEqual(new_part.job_entries.count(), 0)
        self.assertEqual(new_part.file_versions.count(), 0)
        self.assertEqual(copy.hardware_shopping_items.count(), 0)
        self.assertEqual(copy.build_progress()["printed"], 0)

    def test_deep_copy_copies_estimates_and_time_estimates(self) -> None:
        printer = PrinterProfile.objects.create(name="P1")
        PrintTimeEstimate.objects.create(
            part=self.part_r, printer=printer, estimated_time=timedelta(hours=1), actual_time=timedelta(minutes=70)
        )
        new_part = self.part_r.deep_copy(project=self.sub)
        self.assertEqual(new_part.estimation_status, Part.ESTIMATION_SUCCESS)
        self.assertEqual(new_part.estimated_with_preset, self.preset_a)
        self.assertEqual(new_part.estimated_print_time, timedelta(hours=1))
        estimate = new_part.time_estimates.get()
        self.assertEqual(
            (estimate.printer, estimate.estimated_time, estimate.actual_time),
            (printer, timedelta(hours=1), timedelta(minutes=70)),
        )
        self.assertEqual(self.part_r.time_estimates.count(), 1)

    def test_deep_copy_in_flight_estimation_reset_to_none(self) -> None:
        for status in (Part.ESTIMATION_PENDING, Part.ESTIMATION_ESTIMATING):
            Part.objects.filter(pk=self.part_r.pk).update(estimation_status=status)
            self.part_r.refresh_from_db()
            new_part = self.part_r.deep_copy(project=self.root)
            self.assertEqual(new_part.estimation_status, Part.ESTIMATION_NONE)
            self.assertIsNone(new_part.estimated_with_preset)

    def test_subproject_copied_as_root_gets_effective_preset(self) -> None:
        copy = self.sub.deep_copy(parent=None)
        self.assertIsNone(copy.parent)
        self.assertEqual(copy.quantity, 1)  # S has quantity 2 in R; a root is needed once
        self.assertEqual(copy.default_print_preset, self.preset_a)
        self.assertIsNone(copy.subprojects.get().default_print_preset)

    def test_subproject_copied_into_parent_keeps_no_own_preset(self) -> None:
        other = Project.objects.create(name="O", default_print_preset=self.preset_b)
        copy = self.sub.deep_copy(parent=other)
        self.assertIsNone(copy.default_print_preset)
        self.assertEqual(copy.effective_default_print_preset, self.preset_b)

    def test_deep_copy_into_own_subtree_raises(self) -> None:
        before = (Project.objects.count(), Part.objects.count(), set(_media_files()))
        for target in (self.root, self.sub, self.leaf):
            with self.assertRaises(ValidationError):
                self.root.deep_copy(parent=target)
        self.assertEqual((Project.objects.count(), Part.objects.count(), set(_media_files())), before)

    def test_deep_copy_part_missing_stl_on_disk(self) -> None:
        os.remove(self.part_r.stl_file.path)
        with self.assertLogs("core.models.file_copy", "WARNING") as logs:
            new_part = self.part_r.deep_copy(project=self.root)
        self.assertFalse(new_part.stl_file)
        self.assertEqual(new_part.estimation_status, Part.ESTIMATION_NONE)
        self.assertIsNone(new_part.estimated_with_preset)
        self.assertIsNone(new_part.filament_used_grams)
        self.assertIsNone(new_part.filament_used_meters)
        self.assertIsNone(new_part.estimated_print_time)
        self.assertEqual(new_part.time_estimates.count(), 0)
        self.assertIn(self.part_r.stl_file.name, logs.output[0])

    def test_deep_copy_project_missing_image_and_document(self) -> None:
        os.remove(self.root.image.path)
        os.remove(self.doc.file.path)
        with self.assertLogs("core.models.file_copy", "WARNING"):
            copy = self.root.deep_copy(parent=None)
        self.assertFalse(copy.image)
        # A document without its file has nothing to copy, so it is skipped.
        self.assertFalse(ProjectDocument.objects.filter(project__parent=copy).exists())
        self.assertEqual(copy.subprojects.get().name, "S")

    def test_deep_copy_write_error_fails_and_cleans_up(self) -> None:
        from django.core.files.storage import FileSystemStorage

        before_files = _media_files()
        before = (Project.objects.count(), Part.objects.count(), ProjectDocument.objects.count())
        real_save = FileSystemStorage.save
        calls = {"n": 0}

        def flaky_save(storage: FileSystemStorage, *args, **kwargs) -> str:
            calls["n"] += 1
            if calls["n"] == 3:
                raise OSError("No space left on device")
            return real_save(storage, *args, **kwargs)

        with mock.patch.object(FileSystemStorage, "save", flaky_save), self.assertRaises(OSError):
            self.root.deep_copy(parent=None)
        self.assertEqual(calls["n"], 3)
        self.assertEqual((Project.objects.count(), Part.objects.count(), ProjectDocument.objects.count()), before)
        self.assertEqual(_media_files(), before_files)

    def test_deep_copy_recheck_under_lock_rejects_and_cleans_up(self) -> None:
        """The target is re-checked inside the transaction (tree may change after the file copy)."""
        other = Project.objects.create(name="O", default_print_preset=self.preset_b)
        before_files = _media_files()
        before = (Project.objects.count(), Part.objects.count(), ProjectDocument.objects.count())
        real_check = Project._assert_copy_target
        calls = {"n": 0}

        def check_twice(project: Project, parent: Project | None) -> None:
            calls["n"] += 1
            if calls["n"] == 2:
                raise ValidationError({"parent": "moved into the subtree meanwhile"})
            real_check(project, parent)

        with mock.patch.object(Project, "_assert_copy_target", check_twice), self.assertRaises(ValidationError):
            self.root.deep_copy(parent=other)
        self.assertEqual(calls["n"], 2)
        self.assertEqual((Project.objects.count(), Part.objects.count(), ProjectDocument.objects.count()), before)
        self.assertEqual(_media_files(), before_files)

    def test_row_transaction_reads_no_source_rows(self) -> None:
        """Inside the row transaction only the target re-check queries; the rest only writes."""
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        other = Project.objects.create(name="O", default_print_preset=self.preset_b)
        real_create = Project._create_subtree_copy
        captured: dict[str, list[str]] = {}

        def spy(project: Project, *args, **kwargs) -> Project:
            with CaptureQueriesContext(connection) as ctx:
                result = real_create(project, *args, **kwargs)
            captured["sql"] = [q["sql"] for q in ctx.captured_queries]
            return result

        with mock.patch.object(Project, "_create_subtree_copy", spy):
            self.sub.deep_copy(parent=other)
        reads = [sql for sql in captured["sql"] if sql.lstrip().upper().startswith("SELECT")]
        self.assertEqual(reads, [])

    def test_part_deep_copy_failure_removes_new_file(self) -> None:
        before_files = _media_files()
        count = Part.objects.count()
        with (
            mock.patch.object(Part.objects, "create", side_effect=RuntimeError("boom")),
            self.assertRaises(RuntimeError),
        ):
            self.part_r.deep_copy(project=self.root)
        self.assertEqual(Part.objects.count(), count)
        self.assertEqual(_media_files(), before_files)

    def test_default_copy_name_fits_max_length(self) -> None:
        self.root.name = "x" * 255
        self.root.save()
        copy = self.root.deep_copy(parent=None)
        self.assertEqual(len(copy.name), 255)
        self.assertTrue(copy.name.endswith(" (copy)"))

    def test_deep_copy_failure_removes_new_files(self) -> None:
        before_files = _media_files()
        before = (Project.objects.count(), Part.objects.count(), ProjectDocument.objects.count())
        with (
            mock.patch.object(ProjectHardware.objects, "bulk_create", side_effect=RuntimeError("boom")),
            self.assertRaises(RuntimeError),
        ):
            self.root.deep_copy(parent=None)
        self.assertEqual((Project.objects.count(), Part.objects.count(), ProjectDocument.objects.count()), before)
        self.assertEqual(_media_files(), before_files)

    def test_default_copy_name(self) -> None:
        copy = self.root.deep_copy(parent=None)
        self.assertEqual(copy.name, "R (copy)")
        self.assertEqual(copy.subprojects.get().name, "S")
        named = self.root.deep_copy(parent=None, name="Custom")
        self.assertEqual(named.name, "Custom")

    def test_part_deep_copy_into_other_project(self) -> None:
        other = Project.objects.create(name="O", default_print_preset=self.preset_b)
        new_part = self.part_r.deep_copy(project=other)
        self.assertEqual(new_part.project, other)
        self.assertEqual(new_part.name, "r-part")
        self.assertEqual(self.part_r.project, self.root)
        self.assertNotEqual(new_part.stl_file.name, self.part_r.stl_file.name)
