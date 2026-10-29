"""Tests for deleting media files with their rows and the ``cleanup_orphan_media`` command."""

import os
import shutil
import tempfile
import time
from io import StringIO
from pathlib import Path
from unittest import mock

from django.core.files.base import ContentFile
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.db import transaction
from django.test import TestCase, override_settings

from core.models import FileVersion, Part, PrintJob, PrintJobPart, PrintJobPlate, Project, ProjectDocument
from core.signals import MEDIA_FIELDS, file_is_referenced

#: Smallest valid GIF, so ``ImageField`` accepts it.
GIF = (
    b"GIF89a\x01\x00\x01\x00\x80\x00\x00\x00\x00\x00\xff\xff\xff!\xf9\x04\x00\x00\x00\x00\x00"
    b",\x00\x00\x00\x00\x01\x00\x01\x00\x00\x02\x02D\x01\x00;"
)


class _MediaTestBase(TestCase):
    """Run every test against its own temporary ``MEDIA_ROOT``."""

    def setUp(self) -> None:
        super().setUp()
        self.media_root = tempfile.mkdtemp(prefix="layernexus-media-")
        override = override_settings(MEDIA_ROOT=self.media_root)
        override.enable()
        self.addCleanup(override.disable)
        self.addCleanup(shutil.rmtree, self.media_root, ignore_errors=True)

    def exists(self, name: str) -> bool:
        return (Path(self.media_root) / name).is_file()

    def make_part(self, project: Project, filename: str = "part.stl") -> Part:
        return Part.objects.create(name=filename, project=project, stl_file=SimpleUploadedFile(filename, b"solid x"))


class FileDeleteSignalTests(_MediaTestBase):
    """Row deletes and file replacements remove the old file after commit."""

    def setUp(self) -> None:
        super().setUp()
        self.root = Project.objects.create(name="R", image=SimpleUploadedFile("cover.gif", GIF))
        self.sub = Project.objects.create(name="S", parent=self.root, image=SimpleUploadedFile("sub.gif", GIF))
        self.part = self.make_part(self.sub)

    def test_media_fields_cover_the_spec(self) -> None:
        self.assertEqual(
            {(model.__name__, field) for model, field in MEDIA_FIELDS},
            {("Part", "stl_file"), ("FileVersion", "file"), ("ProjectDocument", "file"), ("Project", "image")},
        )

    def test_deleting_part_removes_stl_after_commit(self) -> None:
        name = self.part.stl_file.name
        with self.captureOnCommitCallbacks(execute=False) as callbacks:
            self.part.delete()
        self.assertTrue(self.exists(name))  # not before commit
        for callback in callbacks:
            callback()
        self.assertFalse(self.exists(name))

    def test_subtree_delete_removes_all_files(self) -> None:
        doc = ProjectDocument.objects.create(project=self.sub, name="Manual", file=SimpleUploadedFile("m.pdf", b"%PDF"))
        names = [self.root.image.name, self.sub.image.name, self.part.stl_file.name, doc.file.name]
        self.assertTrue(all(self.exists(name) for name in names))
        with self.captureOnCommitCallbacks(execute=True):
            self.root.delete()
        self.assertEqual([name for name in names if self.exists(name)], [])

    def test_deleting_document_removes_file(self) -> None:
        doc = ProjectDocument.objects.create(project=self.sub, name="Manual", file=SimpleUploadedFile("m.pdf", b"%PDF"))
        with self.captureOnCommitCallbacks(execute=True):
            doc.delete()
        self.assertFalse(self.exists(doc.file.name))

    def test_file_shared_with_fileversion_is_kept(self) -> None:
        name = self.part.stl_file.name
        other = Part.objects.create(name="other", project=self.root)
        FileVersion.objects.create(part=other, version=1, file=name, file_type="stl")
        with self.captureOnCommitCallbacks(execute=True):
            self.part.delete()
        self.assertTrue(self.exists(name))
        self.assertTrue(file_is_referenced(name))

    def test_rolled_back_delete_keeps_file(self) -> None:
        name, pk = self.part.stl_file.name, self.part.pk
        with self.captureOnCommitCallbacks(execute=True) as callbacks:
            try:
                with transaction.atomic():
                    self.part.delete()
                    raise RuntimeError("abort")
            except RuntimeError:
                pass
        self.assertEqual(callbacks, [])
        self.assertTrue(self.exists(name))
        self.assertTrue(Part.objects.filter(pk=pk).exists())

    def test_replacing_stl_removes_old_file_unless_versioned(self) -> None:
        old = self.part.stl_file.name
        with self.captureOnCommitCallbacks(execute=True):
            self.part.stl_file = SimpleUploadedFile("new.stl", b"solid new")
            self.part.save()
        self.assertFalse(self.exists(old))
        self.assertTrue(self.exists(self.part.stl_file.name))

        versioned = self.part.stl_file.name
        FileVersion.objects.create(part=self.part, version=1, file=versioned, file_type="stl")
        with self.captureOnCommitCallbacks(execute=True):
            self.part.stl_file = SimpleUploadedFile("newer.stl", b"solid newer")
            self.part.save()
        self.assertTrue(self.exists(versioned))

    def test_clearing_project_image_removes_file(self) -> None:
        old = self.root.image.name
        with self.captureOnCommitCallbacks(execute=True):
            self.root.image = None
            self.root.save()
        self.assertFalse(self.exists(old))

    def test_saving_without_file_change_keeps_file(self) -> None:
        name = self.part.stl_file.name
        with self.captureOnCommitCallbacks(execute=True):
            self.part.name = "renamed"
            self.part.save()
            Part.objects.get(pk=self.part.pk).save(update_fields=["name"])
        self.assertTrue(self.exists(name))

    def test_deep_copy_shares_no_file_so_deleting_copy_keeps_original(self) -> None:
        copy = self.part.deep_copy(project=self.root)
        with self.captureOnCommitCallbacks(execute=True):
            copy.delete()
        self.assertTrue(self.exists(self.part.stl_file.name))
        self.assertFalse(self.exists(copy.stl_file.name))

    def test_print_job_gcode_untouched_on_part_delete(self) -> None:
        job = PrintJob.objects.create(name="done", status=PrintJob.STATUS_COMPLETED)
        PrintJobPart.objects.create(print_job=job, part=self.part, quantity=1)
        plate = PrintJobPlate.objects.create(print_job=job, plate_number=1)
        plate.gcode_file.save("plate.gcode", ContentFile(b"G28"), save=False)
        plate.thumbnail.save("thumb.png", ContentFile(b"png"), save=False)
        plate.save()
        with self.captureOnCommitCallbacks(execute=True):
            self.part.delete()
        self.assertTrue(self.exists(plate.gcode_file.name))
        self.assertTrue(self.exists(plate.thumbnail.name))

    def test_missing_file_on_delete_is_ignored(self) -> None:
        os.remove(Path(self.media_root) / self.part.stl_file.name)
        with self.captureOnCommitCallbacks(execute=True):
            self.part.delete()
        self.assertFalse(Part.objects.filter(pk=self.part.pk).exists())

    def test_storage_error_is_logged_not_raised(self) -> None:
        storage = self.part.stl_file.storage
        with (
            mock.patch.object(storage, "delete", side_effect=PermissionError("denied")),
            self.assertLogs("core.signals", level="WARNING"),
            self.captureOnCommitCallbacks(execute=True),
        ):
            self.part.delete()


class CleanupOrphanMediaCommandTests(_MediaTestBase):
    """``manage.py cleanup_orphan_media`` lists (dry run) or deletes unreferenced files."""

    def setUp(self) -> None:
        super().setUp()
        self.project = Project.objects.create(name="R", image=SimpleUploadedFile("cover.gif", GIF))
        self.part = self.make_part(self.project)
        self.doc = ProjectDocument.objects.create(
            project=self.project, name="d", file=SimpleUploadedFile("d.pdf", b"x")
        )
        self.orphans = ["stl_files/orphan.stl", "project_documents/old.pdf", "project_images/gone.gif"]
        for name in [*self.orphans, "file_versions/stale.stl"]:
            self._write(name, b"x" * 1024)
        self.orphans.append("file_versions/stale.stl")
        self._write("gcode_jobs/job.gcode", b"G28")  # job history: never scanned
        self._age_all()

    def _write(self, name: str, content: bytes) -> None:
        path = Path(self.media_root) / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)

    def _age_all(self, seconds: int = 2 * 3600) -> None:
        old = time.time() - seconds
        for path in Path(self.media_root).rglob("*"):
            if path.is_file():
                os.utime(path, (old, old))

    def _run(self, *args: str) -> str:
        out = StringIO()
        call_command("cleanup_orphan_media", *args, stdout=out)
        return out.getvalue()

    def _referenced(self) -> list[str]:
        return [self.project.image.name, self.part.stl_file.name, self.doc.file.name]

    def test_cleanup_command_dry_run_lists_and_keeps(self) -> None:
        output = self._run()
        for name in self.orphans:
            self.assertIn(name, output)
            self.assertTrue(self.exists(name))
        for name in self._referenced():
            self.assertNotIn(name, output)
        self.assertIn("4 orphaned files", output)
        self.assertIn("Dry run", output)
        self.assertNotIn("gcode_jobs", output)

    def test_cleanup_command_delete_removes_only_orphans(self) -> None:
        output = self._run("--delete")
        self.assertIn("Deleted 4", output)
        self.assertEqual([name for name in self.orphans if self.exists(name)], [])
        self.assertTrue(all(self.exists(name) for name in self._referenced()))
        self.assertTrue(self.exists("gcode_jobs/job.gcode"))

    def test_fileversion_reference_is_kept(self) -> None:
        FileVersion.objects.create(part=self.part, version=1, file="file_versions/stale.stl", file_type="stl")
        self._run("--delete")
        self.assertTrue(self.exists("file_versions/stale.stl"))

    def test_recent_files_are_skipped(self) -> None:
        """A file younger than ``--min-age`` may belong to an upload or copy still in progress."""
        self._write("stl_files/in-flight.stl", b"x")
        output = self._run("--delete")
        self.assertTrue(self.exists("stl_files/in-flight.stl"))
        self.assertIn("1 newer file", output)
        self._run("--delete", "--min-age", "0")
        self.assertFalse(self.exists("stl_files/in-flight.stl"))

    def test_partial_deep_copy_target_is_an_orphan(self) -> None:
        """A half-written file left by a failing deep-copy save has no row and is listed."""
        self._write("stl_files/part_partial.stl", b"half")
        self._age_all()
        self.assertIn("stl_files/part_partial.stl", self._run())

    def test_nothing_to_do(self) -> None:
        self._run("--delete")
        output = self._run()
        self.assertIn("0 orphaned files", output)
