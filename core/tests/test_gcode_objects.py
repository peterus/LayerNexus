"""Tests for plate parts: G-code object parsing, plate assignment and slicing integration."""

import struct
import tempfile
from pathlib import Path
from unittest import mock

from django.test import TestCase, override_settings
from django.urls import reverse

from core.models import (
    OrcaMachineProfile,
    OrcaPrintPreset,
    Part,
    PrintJob,
    PrintJobPart,
    PrintJobPlate,
    PrintJobPlatePart,
    Project,
)
from core.services.gcode_objects import PlateCopyShare, assign_plate_objects, parse_gcode_object_ids
from core.services.orcaslicer import MultiPlateSliceResult, PlateResult
from core.services.threemf import bundle_objects
from core.tests.gcode_fixtures import plate_gcode
from core.tests.mixins import TestDataMixin, make_sliced_job


def _binary_stl() -> bytes:
    facet = struct.pack("<12fH", 0, 0, 1, 0, 0, 0, 1, 0, 0, 0, 1, 0, 0)
    return b"\0" * 80 + struct.pack("<I", 1) + facet


class ParseGcodeObjectIdsTests(TestCase):
    """``parse_gcode_object_ids`` reads OrcaSlicer's object labels."""

    def test_parse_exclude_object_define(self) -> None:
        """``EXCLUDE_OBJECT_DEFINE NAME=Object_<N>_id_<i>_copy_0`` yields N (the spike's lines)."""
        gcode = plate_gcode([12, 3, 25], comments=False)
        self.assertEqual(parse_gcode_object_ids(gcode), [3, 12, 25])

    def test_parse_does_not_confuse_prefixes(self) -> None:
        """``Object_1`` and ``Object_12`` are different objects."""
        self.assertEqual(parse_gcode_object_ids(plate_gcode([1, 12])), [1, 12])

    def test_parse_printing_object_fallback(self) -> None:
        """Without DEFINE lines the ``; printing object`` comments are used."""
        gcode = plate_gcode([4, 5], define=False)
        self.assertEqual(parse_gcode_object_ids(gcode), [4, 5])

    def test_parse_ignores_stop_printing_comments(self) -> None:
        """Only ``; printing object`` lines count, not ``; stop printing object``."""
        gcode = b"; stop printing object Object_9_id_0_copy_0\n"
        self.assertEqual(parse_gcode_object_ids(gcode), [])

    def test_parse_no_labels_returns_empty(self) -> None:
        """G-code without object labels yields no IDs."""
        self.assertEqual(parse_gcode_object_ids(plate_gcode([1, 2], define=False, comments=False)), [])
        self.assertEqual(parse_gcode_object_ids(b""), [])

    def test_parse_quoted_names(self) -> None:
        """Quoted object names are accepted as well."""
        self.assertEqual(parse_gcode_object_ids(b"EXCLUDE_OBJECT_DEFINE NAME='Object_7_id_0_copy_0'\n"), [7])


class AssignPlateObjectsTests(TestCase):
    """``assign_plate_objects`` maps parsed object IDs to part copies per plate."""

    def test_assigns_copies_to_plates(self) -> None:
        """Every object lands on the plate whose G-code lists it."""
        objects = bundle_objects([(2, 1), (1, 1)])  # A×2, B×1 → objects 1, 2 (A), 3 (B)
        result = assign_plate_objects(objects, {1: [1, 3], 2: [2]})
        self.assertEqual(
            result,
            {
                1: [PlateCopyShare(0, 0, 1, 1), PlateCopyShare(1, 0, 1, 1)],
                2: [PlateCopyShare(0, 1, 1, 1)],
            },
        )

    def test_multi_mesh_copy_split_across_plates(self) -> None:
        """A two-mesh copy split over two plates has one mesh on each."""
        objects = bundle_objects([(1, 2)])  # one copy, two meshes → objects 1, 2
        result = assign_plate_objects(objects, {1: [1], 2: [2]})
        self.assertEqual(result, {1: [PlateCopyShare(0, 0, 1, 2)], 2: [PlateCopyShare(0, 0, 1, 2)]})

    def test_unplaced_objects_are_absent(self) -> None:
        """Objects on no plate are simply missing from the result (no error)."""
        objects = bundle_objects([(3, 1)])
        result = assign_plate_objects(objects, {1: [1, 3]})
        self.assertEqual(result, {1: [PlateCopyShare(0, 0, 1, 1), PlateCopyShare(0, 2, 1, 1)]})

    def test_single_plate_without_labels_maps_all(self) -> None:
        """Review focus 4: one plate without labels gets every object."""
        objects = bundle_objects([(2, 1), (1, 1)])
        result = assign_plate_objects(objects, {1: []})
        self.assertEqual(
            result, {1: [PlateCopyShare(0, 0, 1, 1), PlateCopyShare(0, 1, 1, 1), PlateCopyShare(1, 0, 1, 1)]}
        )

    def test_multi_plate_without_labels_is_unknown(self) -> None:
        """Several plates without labels cannot be attributed."""
        self.assertIsNone(assign_plate_objects(bundle_objects([(2, 1)]), {1: [], 2: []}))

    def test_partly_labelled_plates_are_unknown(self) -> None:
        """A labelled and an unlabelled plate cannot be attributed either."""
        self.assertIsNone(assign_plate_objects(bundle_objects([(2, 1)]), {1: [1], 2: []}))

    def test_unknown_object_id_is_unknown(self) -> None:
        """An object ID beyond the bundle means the mapping does not fit."""
        self.assertIsNone(assign_plate_objects(bundle_objects([(1, 1)]), {1: [1, 2]}))

    def test_object_on_two_plates_is_unknown(self) -> None:
        """The same object on two plates is contradictory."""
        self.assertIsNone(assign_plate_objects(bundle_objects([(2, 1)]), {1: [1], 2: [1, 2]}))

    def test_empty_plate_list(self) -> None:
        """Review focus 4: no plates → empty assignment, no exception."""
        self.assertEqual(assign_plate_objects(bundle_objects([(2, 1)]), {}), {})
        self.assertEqual(assign_plate_objects([], {}), {})


class UnplacedCopiesTests(TestCase):
    """``PrintJob.unplaced_copies`` / ``plate_assignment_warnings``."""

    def setUp(self) -> None:
        project = Project.objects.create(name="P")
        self.r10 = Part.objects.create(project=project, name="R10", quantity=1)
        self.r11 = Part.objects.create(project=project, name="R11", quantity=1)

    def _job(self, plates: int = 2) -> PrintJob:
        job = make_sliced_job("J", plates=plates)
        return job

    def test_unplaced_copies_warning(self) -> None:
        """Job-41 case: 25 copies, 23 on plates → 2× R11 on no plate."""
        job = self._job()
        PrintJobPart.objects.create(print_job=job, part=self.r10, quantity=20)
        PrintJobPart.objects.create(print_job=job, part=self.r11, quantity=5)
        plate1, plate2 = job.plates.all()
        for copy in range(20):
            PrintJobPlatePart.objects.create(plate=plate1 if copy < 12 else plate2, part=self.r10, copy_index=copy)
        for copy in (0, 1, 4):  # copies 2 and 3 (objects 23, 24) were not placed
            PrintJobPlatePart.objects.create(plate=plate2, part=self.r11, copy_index=copy)

        self.assertEqual(job.unplaced_copies(), {self.r11.pk: 2})
        self.assertEqual(job.plate_assignment_warnings(), ["2× R11 on no plate"])

    def test_multi_mesh_copy_counts_as_placed_only_when_complete(self) -> None:
        """A two-mesh copy with only one mesh on a plate is unplaced."""
        job = self._job()
        PrintJobPart.objects.create(print_job=job, part=self.r10, quantity=2)
        plate1, plate2 = job.plates.all()
        PrintJobPlatePart.objects.create(plate=plate1, part=self.r10, copy_index=0, meshes_on_plate=1, mesh_count=2)
        PrintJobPlatePart.objects.create(plate=plate2, part=self.r10, copy_index=0, meshes_on_plate=1, mesh_count=2)
        PrintJobPlatePart.objects.create(plate=plate2, part=self.r10, copy_index=1, meshes_on_plate=1, mesh_count=2)

        self.assertEqual(job.unplaced_copies(), {self.r10.pk: 1})

    def test_all_placed_has_no_warning(self) -> None:
        """No warning when every copy lies on a plate."""
        job = self._job(plates=1)
        PrintJobPart.objects.create(print_job=job, part=self.r10, quantity=1)
        PrintJobPlatePart.objects.create(plate=job.plates.get(), part=self.r10, copy_index=0)
        self.assertEqual(job.unplaced_copies(), {})
        self.assertEqual(job.plate_assignment_warnings(), [])

    def test_unknown_assignment_warns(self) -> None:
        """A job whose plate assignment is unknown says so instead of listing copies."""
        job = self._job()
        PrintJobPart.objects.create(print_job=job, part=self.r10, quantity=2)
        PrintJob.objects.filter(pk=job.pk).update(plate_parts_known=False)
        job.refresh_from_db()
        self.assertEqual(job.unplaced_copies(), {})
        self.assertEqual(len(job.plate_assignment_warnings()), 1)
        self.assertIn("unknown", job.plate_assignment_warnings()[0])

    def test_unsliced_job_has_no_warning(self) -> None:
        """A draft without plates has nothing to warn about."""
        job = PrintJob.objects.create(name="Draft")
        PrintJobPart.objects.create(print_job=job, part=self.r10, quantity=2)
        self.assertEqual(job.unplaced_copies(), {})
        self.assertEqual(job.plate_assignment_warnings(), [])

    def test_str(self) -> None:
        """``__str__`` names the part, copy and plate; partial copies show their mesh share."""
        job = self._job(plates=1)
        entry = PrintJobPlatePart.objects.create(
            plate=job.plates.get(), part=self.r10, copy_index=0, meshes_on_plate=1, mesh_count=2
        )
        self.assertIn("R10 copy 1 (1/2 meshes)", str(entry))


@override_settings(MEDIA_ROOT=tempfile.mkdtemp())
class SlicingCreatesPlatePartsTests(TestCase):
    """The slicing worker records the plate parts from the G-code labels."""

    def setUp(self) -> None:
        from django.conf import settings

        self.media = Path(settings.MEDIA_ROOT)
        (self.media / "stl_files").mkdir(parents=True, exist_ok=True)
        project = Project.objects.create(name="Proj")
        preset = OrcaPrintPreset.objects.create(
            name="P", orca_name="P", state=OrcaPrintPreset.STATE_RESOLVED, instantiation=True
        )
        machine = OrcaMachineProfile.objects.create(
            name="M", orca_name="M", state=OrcaMachineProfile.STATE_RESOLVED, instantiation=True
        )
        self.job = PrintJob.objects.create(
            name="J", machine_profile=machine, print_preset=preset, status=PrintJob.STATUS_SLICING
        )
        self.parts = []
        for name, quantity in (("A", 2), ("B", 1)):
            (self.media / "stl_files" / f"{name}.stl").write_bytes(_binary_stl())
            part = Part.objects.create(name=name, project=project, print_preset=preset)
            part.stl_file.name = f"stl_files/{name}.stl"
            part.save(update_fields=["stl_file"])
            PrintJobPart.objects.create(print_job=self.job, part=part, quantity=quantity)
            self.parts.append(part)

    def _slice(self, plates: list[bytes]) -> None:
        from core.services import slicing_worker

        result = MultiPlateSliceResult(
            plates=[
                PlateResult(plate_number=n, gcode_content=g, filament_used_grams=5.0)
                for n, g in enumerate(plates, start=1)
            ],
        )
        with (
            mock.patch.object(slicing_worker, "_build_slicer_kwargs", return_value={}),
            mock.patch.object(slicing_worker, "OrcaSlicerAPIClient") as client_cls,
        ):
            client_cls.return_value.slice_bundle.return_value = result
            slicing_worker._slice_job_in_background(self.job.pk)
        self.job.refresh_from_db()

    def _entries(self) -> list[tuple[int, str, int]]:
        return [
            (e.plate.plate_number, e.part.name, e.copy_index)
            for e in PrintJobPlatePart.objects.filter(plate__print_job=self.job).select_related("plate", "part")
        ]

    def test_slicing_creates_plate_parts(self) -> None:
        """Objects 1, 2 (A) and 3 (B) are recorded on the plates that print them."""
        self._slice([plate_gcode([1, 3]), plate_gcode([2])])

        self.assertEqual(self.job.status, PrintJob.STATUS_SLICED)
        self.assertTrue(self.job.plate_parts_known)
        self.assertEqual(sorted(self._entries()), [(1, "A", 0), (1, "B", 0), (2, "A", 1)])
        self.assertEqual(self.job.unplaced_copies(), {})

    def test_bundle_order_follows_job_part_pk(self) -> None:
        """The bundle is built in ``PrintJobPart`` pk order (deterministic)."""
        from core.services import slicing_worker

        with mock.patch.object(slicing_worker, "create_3mf_bundle", wraps=slicing_worker.create_3mf_bundle) as spy:
            self._slice([plate_gcode([1, 2, 3])])
        paths = [Path(path).name for path, _qty in spy.call_args[0][0]]
        self.assertEqual(paths, ["A.stl", "B.stl"])

    def test_slicing_reports_unplaced_copy(self) -> None:
        """A copy missing from every plate is recorded as unplaced; slicing still succeeds."""
        self._slice([plate_gcode([1]), plate_gcode([3])])

        self.assertEqual(self.job.status, PrintJob.STATUS_SLICED)
        self.assertEqual(self.job.unplaced_copies(), {self.parts[0].pk: 1})

    def test_single_plate_without_labels_maps_all(self) -> None:
        """Review focus 4: a single unlabelled plate gets every copy."""
        self._slice([plate_gcode([1, 2, 3], define=False, comments=False)])

        self.assertTrue(self.job.plate_parts_known)
        self.assertEqual(sorted(self._entries()), [(1, "A", 0), (1, "A", 1), (1, "B", 0)])

    def test_multi_plate_without_labels_marks_unknown(self) -> None:
        """Several unlabelled plates → no plate parts, ``plate_parts_known`` False, still sliced."""
        unlabelled = plate_gcode([1], define=False, comments=False)
        self._slice([unlabelled, unlabelled])

        self.assertEqual(self.job.status, PrintJob.STATUS_SLICED)
        self.assertFalse(self.job.plate_parts_known)
        self.assertEqual(self._entries(), [])
        self.assertIn("unknown", self.job.plate_assignment_warnings()[0])

    def test_reslice_replaces_plate_parts(self) -> None:
        """Re-slicing deletes the old plates and their plate parts with them."""
        self._slice([plate_gcode([1, 3]), plate_gcode([2])])
        self.job.plates.all().delete()
        PrintJob.objects.filter(pk=self.job.pk).update(status=PrintJob.STATUS_SLICING)
        self._slice([plate_gcode([1, 2, 3])])

        self.assertEqual(sorted(self._entries()), [(1, "A", 0), (1, "A", 1), (1, "B", 0)])


class JobDetailPlateWarningTests(TestDataMixin, TestCase):
    """The job detail page shows the plate-assignment warning."""

    def test_detail_shows_unplaced_warning(self) -> None:
        """'N× <part> on no plate' is rendered for unplaced copies."""
        job = make_sliced_job("J", plates=1)
        PrintJobPart.objects.create(print_job=job, part=self.part, quantity=3)
        PrintJobPlatePart.objects.create(plate=PrintJobPlate.objects.get(print_job=job), part=self.part, copy_index=0)

        self.client.login(username="testuser", password="testpass123")
        response = self.client.get(reverse("core:printjob_detail", args=[job.pk]))

        self.assertContains(response, "2× Test Part on no plate")

    def test_detail_without_warning(self) -> None:
        """No warning block when every copy is placed."""
        job = make_sliced_job("J", plates=1)
        PrintJobPart.objects.create(print_job=job, part=self.part, quantity=1)
        PrintJobPlatePart.objects.create(plate=PrintJobPlate.objects.get(print_job=job), part=self.part, copy_index=0)

        self.client.login(username="testuser", password="testpass123")
        response = self.client.get(reverse("core:printjob_detail", args=[job.pk]))

        self.assertNotContains(response, "plate-assignment-warning")


class JobDetailPlatePartsQueryCountTests(TestDataMixin, TestCase):
    """The job detail page shows the parts per plate without an N+1."""

    def _count_queries(self, job: PrintJob) -> int:
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        with CaptureQueriesContext(connection) as ctx:
            response = self.client.get(reverse("core:printjob_detail", args=[job.pk]))
        self.assertEqual(response.status_code, 200)
        return len(ctx.captured_queries)

    def test_detail_query_count_independent_of_plate_parts(self) -> None:
        """More plates, parts and copies do not add queries; the per-plate list is rendered."""
        self.client.login(username="testuser", password="testpass123")
        small = make_sliced_job("Small", plates=1)
        PrintJobPart.objects.create(print_job=small, part=self.part, quantity=1)
        PrintJobPlatePart.objects.create(plate=small.plates.get(), part=self.part, copy_index=0)

        large = make_sliced_job("Large", plates=3)
        extra = Part.objects.create(name="Extra Part", project=self.project, quantity=1)
        PrintJobPart.objects.create(print_job=large, part=self.part, quantity=6)
        PrintJobPart.objects.create(print_job=large, part=extra, quantity=3)
        for index, plate in enumerate(large.plates.all()):
            for copy in (2 * index, 2 * index + 1):
                PrintJobPlatePart.objects.create(plate=plate, part=self.part, copy_index=copy)
            PrintJobPlatePart.objects.create(plate=plate, part=extra, copy_index=index)

        self.assertEqual(self._count_queries(large), self._count_queries(small))
        response = self.client.get(reverse("core:printjob_detail", args=[large.pk]))
        self.assertContains(response, "Test Part ×2", count=3)
        self.assertContains(response, "Extra Part ×1", count=3)


class PlatePartHistoryGuardTests(TestCase):
    """Parts on plates with attempts are print history (not deletable)."""

    def setUp(self) -> None:
        self.project = Project.objects.create(name="P")
        self.part = Part.objects.create(project=self.project, name="R11", quantity=1)
        self.job = make_sliced_job("J", plates=1)
        self.plate = self.job.plates.get()
        PrintJobPlatePart.objects.create(plate=self.plate, part=self.part, copy_index=0)

    def test_plate_part_without_attempt_is_no_history(self) -> None:
        """A plate entry alone (no attempt) is not history."""
        self.assertFalse(self.part.has_print_history())
        self.assertFalse(self.project.history_blocking_parts().exists())

    def test_plate_part_on_plate_with_attempt_is_history(self) -> None:
        """A plate entry on a plate with an attempt makes the part print history."""
        from core.tests.mixins import add_attempt

        add_attempt(self.plate, None)
        self.assertTrue(self.part.has_print_history())
        self.assertEqual(list(self.part.history_jobs()), [self.job])
        self.assertEqual(list(self.project.history_blocking_parts()), [self.part])

    def test_part_with_plate_entries_is_protected(self) -> None:
        """The database refuses deleting a part that lies on a plate."""
        from django.db.models import ProtectedError

        with self.assertRaises(ProtectedError):
            self.part.delete()


class PlatePartAdminTests(TestDataMixin, TestCase):
    """Plate parts are read-only in the admin."""

    def test_plate_admin_shows_read_only_plate_parts(self) -> None:
        """The plate change page lists the plate parts without add/delete controls."""
        from django.contrib.auth.models import User

        admin_user = User.objects.create_superuser("root", "root@example.com", "pw")
        job = make_sliced_job("J", plates=1)
        plate = job.plates.get()
        PrintJobPlatePart.objects.create(plate=plate, part=self.part, copy_index=0)

        self.client.force_login(admin_user)
        response = self.client.get(reverse("admin:core_printjobplate_change", args=[plate.pk]))

        self.assertContains(response, "Test Part")
        self.assertNotContains(response, "plate_parts-0-DELETE")
        self.assertContains(response, 'name="plate_parts-MAX_NUM_FORMS" value="0"')

    def test_plate_parts_known_is_read_only_in_job_admin(self) -> None:
        """``plate_parts_known`` is a slicer fact, not editable in the job admin."""
        from django.contrib.admin.sites import site
        from django.test import RequestFactory

        job = make_sliced_job("J", plates=1)
        request = RequestFactory().get("/")
        request.user = self.user
        self.assertIn("plate_parts_known", site._registry[PrintJob].get_readonly_fields(request, job))


class PlatePartLabelTests(TestCase):
    """Same-named parts are told apart; history queries return each row once."""

    def test_same_named_parts_are_disambiguated(self) -> None:
        """Two parts called 'Bracket' in different projects get their project name appended."""
        left = Part.objects.create(project=Project.objects.create(name="Left"), name="Bracket", quantity=1)
        right = Part.objects.create(project=Project.objects.create(name="Right"), name="Bracket", quantity=1)
        job = make_sliced_job("J", plates=1)
        PrintJobPart.objects.create(print_job=job, part=left, quantity=2)
        PrintJobPart.objects.create(print_job=job, part=right, quantity=1)
        plate = job.plates.get()
        PrintJobPlatePart.objects.create(plate=plate, part=left, copy_index=0)
        PrintJobPlatePart.objects.create(plate=plate, part=right, copy_index=0)

        self.assertEqual(sorted(plate.parts_summary()), ["Bracket (Left) ×1", "Bracket (Right) ×1"])
        self.assertEqual(job.plate_assignment_warnings(), ["1× Bracket (Left) on no plate"])

    def test_history_queries_return_each_row_once(self) -> None:
        """Many plates, plate parts and attempts still yield one job / one part (no fan-out duplicates)."""
        from core.tests.mixins import add_attempt

        project = Project.objects.create(name="P")
        part = Part.objects.create(project=project, name="R", quantity=1)
        job = make_sliced_job("J", plates=3)
        PrintJobPart.objects.create(print_job=job, part=part, quantity=6)
        for index, plate in enumerate(job.plates.all()):
            for copy in (2 * index, 2 * index + 1):
                PrintJobPlatePart.objects.create(plate=plate, part=part, copy_index=copy)
            add_attempt(plate, None, "failed")
            add_attempt(plate, None, "passed")

        self.assertEqual(list(part.history_jobs()), [job])
        self.assertEqual(list(project.history_blocking_parts()), [part])
        self.assertEqual(project.deletion_summary()["print_history"], 1)
