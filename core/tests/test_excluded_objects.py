"""Tests for excluded part copies of a print attempt (Klipper ``EXCLUDE_OBJECT`` + manual marks).

Klipper reports the objects excluded during a print (upper-cased names like
``OBJECT_3_ID_0_COPY_0``); the worker records them on the printing attempt and
maps them to the plate's part copies via ``PrintJobPlatePart.object_ids``.  An
operator can also mark copies as not ok.  Excluded copies of a passed attempt
do not count as printed.
"""

from __future__ import annotations

from unittest import mock

from django.contrib.auth.models import User
from django.test import TestCase

from core.models import Part, PrintAttemptExclusion, PrintJobPart, PrintJobPlatePart, PrintQueue, Project
from core.services.gcode_objects import assign_plate_objects, parse_klipper_object_id
from core.services.operator_dialog import review_dialog
from core.services.printer_status_sync import apply_status_event
from core.services.threemf import bundle_objects
from core.tests.mixins import add_attempt, complete_plate, make_sliced_job


def _place(plate, part: Part, copy_index: int, object_ids: list[int], *, mesh_count: int = 1) -> PrintJobPlatePart:
    """Record one copy of ``part`` on ``plate`` with its bundle object IDs."""
    return PrintJobPlatePart.objects.create(
        plate=plate,
        part=part,
        copy_index=copy_index,
        meshes_on_plate=len(object_ids),
        mesh_count=mesh_count,
        object_ids=object_ids,
    )


def _status_event(status: dict) -> dict:
    return {"jsonrpc": "2.0", "method": "notify_status_update", "params": [status, 123.0]}


class ParseKlipperObjectIdTests(TestCase):
    """Klipper upper-cases the names OrcaSlicer writes (``Object_<N>_id_<i>_copy_0``)."""

    def test_upper_cased_klipper_name(self) -> None:
        self.assertEqual(parse_klipper_object_id("OBJECT_3_ID_0_COPY_0"), 3)

    def test_original_orca_name(self) -> None:
        self.assertEqual(parse_klipper_object_id("Object_12_id_1_copy_0"), 12)

    def test_quoted_name(self) -> None:
        self.assertEqual(parse_klipper_object_id("'OBJECT_7_ID_2_COPY_0'"), 7)

    def test_foreign_names_are_none(self) -> None:
        for name in ("PART_A", "OBJECT_", "MY_OBJECT_3", "", None, 5):
            with self.subTest(name=name):
                self.assertIsNone(parse_klipper_object_id(name))

    def test_no_prefix_match_across_digits(self) -> None:
        self.assertEqual(parse_klipper_object_id("OBJECT_31_ID_0_COPY_0"), 31)

    def test_absurdly_long_number_is_none(self) -> None:
        """Untrusted input: a huge number must neither raise (int() digit limit) nor match."""
        self.assertIsNone(parse_klipper_object_id(f"OBJECT_{'9' * 5000}_ID_0"))


class PlateSharesObjectIdsTests(TestCase):
    """``assign_plate_objects`` reports the object IDs of every copy share."""

    def test_object_ids_per_copy(self) -> None:
        # Part 0: 2 copies × 2 meshes → IDs 1..4 mesh-major (1, 2 = mesh 0 of copy 0, 1); part 1 → ID 5.
        objects = bundle_objects([(2, 2), (1, 1)])
        shares = assign_plate_objects(objects, {1: [1, 3, 5], 2: [2, 4]})
        by_key = {(s.part_index, s.copy_index): s.object_ids for s in shares[1]}
        self.assertEqual(by_key, {(0, 0): (1, 3), (1, 0): (5,)})
        by_key = {(s.part_index, s.copy_index): s.object_ids for s in shares[2]}
        self.assertEqual(by_key, {(0, 1): (2, 4)})


class ExclusionTestBase(TestCase):
    """A two-plate job: plate 1 carries 3 copies of R02 (IDs 1-3), plate 2 one 2-mesh copy of R05."""

    def setUp(self) -> None:
        self.user = User.objects.create_user("op", password="x")
        self.project = Project.objects.create(name="P")
        self.part = Part.objects.create(name="R02", project=self.project, quantity=10)
        self.multi = Part.objects.create(name="R05", project=self.project, quantity=10)
        self.job = make_sliced_job(plates=2)
        PrintJobPart.objects.create(print_job=self.job, part=self.part, quantity=3)
        PrintJobPart.objects.create(print_job=self.job, part=self.multi, quantity=1)
        self.plate1, self.plate2 = self.job.plates.order_by("plate_number")
        self.copies = [_place(self.plate1, self.part, index, [index + 1]) for index in range(3)]
        self.multi_copy = _place(self.plate2, self.multi, 0, [4, 5], mesh_count=2)


class RecordExcludedObjectsTests(ExclusionTestBase):
    """``PrintQueue.record_excluded_objects`` (Klipper source)."""

    def test_maps_names_to_plate_parts(self) -> None:
        attempt = add_attempt(self.plate1, None, "printing")
        self.assertTrue(attempt.record_excluded_objects(["OBJECT_2_ID_1_COPY_0"]))
        attempt.refresh_from_db()
        self.assertEqual(attempt.excluded_objects, ["OBJECT_2_ID_1_COPY_0"])
        exclusion = PrintAttemptExclusion.objects.get(attempt=attempt)
        self.assertEqual(exclusion.plate_part, self.copies[1])
        self.assertEqual(exclusion.source, PrintAttemptExclusion.SOURCE_KLIPPER)

    def test_unchanged_list_writes_nothing(self) -> None:
        attempt = add_attempt(self.plate1, None, "printing")
        attempt.record_excluded_objects(["OBJECT_2_ID_1_COPY_0"])
        with self.assertNumQueries(0):
            self.assertFalse(attempt.record_excluded_objects(["object_2_id_1_copy_0"]))
        self.assertFalse(attempt.record_excluded_objects([]))

    def test_names_accumulate(self) -> None:
        """Klipper's list only grows during a print; a shorter list (reset) never drops recorded names."""
        attempt = add_attempt(self.plate1, None, "printing")
        attempt.record_excluded_objects(["OBJECT_1_ID_0_COPY_0"])
        attempt.record_excluded_objects(["OBJECT_1_ID_0_COPY_0", "OBJECT_3_ID_2_COPY_0"])
        attempt.record_excluded_objects([])
        attempt.refresh_from_db()
        self.assertEqual(attempt.excluded_objects, ["OBJECT_1_ID_0_COPY_0", "OBJECT_3_ID_2_COPY_0"])
        self.assertEqual(
            set(attempt.exclusions.values_list("plate_part", flat=True)), {self.copies[0].pk, self.copies[2].pk}
        )

    def test_unknown_names_are_kept_without_rows(self) -> None:
        attempt = add_attempt(self.plate1, None, "printing")
        self.assertTrue(attempt.record_excluded_objects(["PURGE_LINE", "OBJECT_99_ID_0_COPY_0"]))
        attempt.refresh_from_db()
        self.assertEqual(attempt.excluded_objects, ["PURGE_LINE", "OBJECT_99_ID_0_COPY_0"])
        self.assertFalse(attempt.exclusions.exists())
        self.assertEqual(attempt.excluded_summary(), "2 object(s) excluded")

    def test_one_mesh_of_a_multi_mesh_copy(self) -> None:
        attempt = add_attempt(self.plate2, None, "printing")
        attempt.record_excluded_objects(["OBJECT_5_ID_1_COPY_0"])
        self.assertEqual(list(attempt.exclusions.values_list("plate_part", flat=True)), [self.multi_copy.pk])

    def test_ignored_once_resolved(self) -> None:
        attempt = add_attempt(self.plate1, None, "printing")
        PrintQueue.objects.filter(pk=attempt.pk).update(result="cancelled")
        self.assertFalse(attempt.record_excluded_objects(["OBJECT_1_ID_0_COPY_0"]))
        self.assertFalse(PrintAttemptExclusion.objects.exists())
        attempt.refresh_from_db()
        self.assertEqual(attempt.excluded_objects, [])

    def test_ignored_once_finished(self) -> None:
        """After the printer reported the end, a late (stale) update no longer adds exclusions."""
        attempt = add_attempt(self.plate1, None, "awaiting_review")
        self.assertFalse(attempt.record_excluded_objects(["OBJECT_1_ID_0_COPY_0"]))
        self.assertFalse(attempt.exclusions.exists())

    def test_stale_instance_merges_with_the_database(self) -> None:
        attempt = add_attempt(self.plate1, None, "printing")
        stale = PrintQueue.objects.get(pk=attempt.pk)
        attempt.record_excluded_objects(["OBJECT_1_ID_0_COPY_0"])
        stale.record_excluded_objects(["OBJECT_2_ID_1_COPY_0"])
        attempt.refresh_from_db()
        self.assertEqual(attempt.excluded_objects, ["OBJECT_1_ID_0_COPY_0", "OBJECT_2_ID_1_COPY_0"])
        self.assertEqual(attempt.exclusions.count(), 2)

    def test_names_are_bounded(self) -> None:
        from core.models.queue import MAX_EXCLUDED_OBJECT_NAME, MAX_EXCLUDED_OBJECTS

        attempt = add_attempt(self.plate1, None, "printing")
        attempt.record_excluded_objects(["X" * (MAX_EXCLUDED_OBJECT_NAME + 1)])
        attempt.record_excluded_objects([f"NAME_{index}" for index in range(MAX_EXCLUDED_OBJECTS + 50)])
        attempt.refresh_from_db()
        self.assertEqual(len(attempt.excluded_objects), MAX_EXCLUDED_OBJECTS)
        self.assertNotIn("X" * (MAX_EXCLUDED_OBJECT_NAME + 1), attempt.excluded_objects)

    def test_manual_removal_is_not_undone_by_a_repeated_klipper_list(self) -> None:
        attempt = add_attempt(self.plate1, None, "printing")
        attempt.record_excluded_objects(["OBJECT_1_ID_0_COPY_0"])
        attempt.exclusions.all().delete()
        self.assertFalse(attempt.record_excluded_objects(["OBJECT_1_ID_0_COPY_0"]))
        self.assertFalse(attempt.exclusions.exists())


class SetExcludedPlatePartsTests(ExclusionTestBase):
    """``PrintQueue.set_excluded_plate_parts`` (manual source)."""

    def test_replaces_the_set_and_keeps_klipper_rows(self) -> None:
        attempt = add_attempt(self.plate1, None, "printing")
        attempt.record_excluded_objects(["OBJECT_1_ID_0_COPY_0"])
        PrintQueue.objects.filter(pk=attempt.pk).update(result="passed")
        attempt.refresh_from_db()

        attempt.set_excluded_plate_parts([self.copies[0].pk, self.copies[2].pk], user=self.user)
        rows = {row.plate_part_id: row for row in attempt.exclusions.all()}
        self.assertEqual(set(rows), {self.copies[0].pk, self.copies[2].pk})
        self.assertEqual(rows[self.copies[0].pk].source, PrintAttemptExclusion.SOURCE_KLIPPER)
        self.assertEqual(rows[self.copies[2].pk].source, PrintAttemptExclusion.SOURCE_MANUAL)
        self.assertEqual(rows[self.copies[2].pk].created_by, self.user)

        attempt.set_excluded_plate_parts([], user=self.user)
        self.assertFalse(attempt.exclusions.exists())

    def test_foreign_plate_parts_are_rejected(self) -> None:
        attempt = add_attempt(self.plate1, None, "awaiting_review")
        with self.assertRaises(ValueError):
            attempt.set_excluded_plate_parts([self.multi_copy.pk], user=self.user)
        self.assertFalse(attempt.exclusions.exists())

    def test_only_awaiting_review_or_passed(self) -> None:
        for state in ("waiting", "printing", "failed", "cancelled"):
            with self.subTest(state=state):
                attempt = add_attempt(self.plate1, None, state)
                with self.assertRaises(ValueError):
                    attempt.set_excluded_plate_parts([self.copies[0].pk], user=self.user)
                attempt.delete()


class ExcludedCopiesNotPrintedTests(ExclusionTestBase):
    """Excluded copies of a passed attempt do not count in ``Part.printed_quantity``."""

    def assertPrinted(self, part: Part, expected: int) -> None:  # noqa: N802 — unittest naming
        self.assertEqual(Part.objects.get(pk=part.pk).printed_quantity, expected, "database path")
        prefetched = Part.objects.prefetch_related(*Part.printed_quantity_prefetch_lookups()).get(pk=part.pk)
        with self.assertNumQueries(0):
            self.assertEqual(prefetched.printed_quantity, expected, "prefetched path")

    def test_excluded_copy_is_not_printed(self) -> None:
        attempt = complete_plate(self.plate1)
        attempt.set_excluded_plate_parts([self.copies[1].pk], user=self.user)
        self.assertPrinted(self.part, 2)
        self.assertEqual(self.job.printed_copies(), 2)

    def test_excluded_mesh_makes_the_copy_unprinted(self) -> None:
        complete_plate(self.plate1)
        attempt = complete_plate(self.plate2)
        self.assertPrinted(self.multi, 1)
        attempt.set_excluded_plate_parts([self.multi_copy.pk], user=self.user)
        self.assertPrinted(self.multi, 0)

    def test_exclusion_of_a_failed_attempt_is_irrelevant(self) -> None:
        failed = add_attempt(self.plate1, None, "printing")
        failed.record_excluded_objects(["OBJECT_1_ID_0_COPY_0"])
        PrintQueue.objects.filter(pk=failed.pk).update(result="failed", completed_at=failed.started_at)
        complete_plate(self.plate1)
        self.assertPrinted(self.part, 3)

    def test_unknown_plate_assignment_ignores_exclusions(self) -> None:
        """``plate_parts_known=False`` keeps the legacy all-plates rule (exclusions are not trusted)."""
        type(self.job).objects.filter(pk=self.job.pk).update(plate_parts_known=False)
        attempt = complete_plate(self.plate1)
        attempt.set_excluded_plate_parts([self.copies[0].pk], user=self.user)
        complete_plate(self.plate2)
        self.assertPrinted(self.part, 3)

    def test_printed_plates_overview_skips_excluded_copies(self) -> None:
        attempt = complete_plate(self.plate1)
        attempt.set_excluded_plate_parts([self.copies[0].pk], user=self.user)
        (row,) = Part.objects.get(pk=self.part.pk).printed_plates()
        self.assertEqual((row["copies"], row["partial"], row["excluded"]), (2, 0, 1))


class StatusSyncExclusionTests(ExclusionTestBase):
    """The worker records Klipper's ``exclude_object.excluded_objects`` on the printing attempt."""

    def test_status_update_records_exclusions(self) -> None:
        attempt = add_attempt(self.plate1, None, "printing")
        apply_status_event(attempt, _status_event({"exclude_object": {"excluded_objects": ["OBJECT_3_ID_2_COPY_0"]}}))
        self.assertEqual(list(attempt.exclusions.values_list("plate_part", flat=True)), [self.copies[2].pk])

    def test_exclusions_in_the_final_update_are_recorded_before_the_end(self) -> None:
        attempt = add_attempt(self.plate1, None, "printing")
        apply_status_event(
            attempt,
            _status_event(
                {
                    "print_stats": {"state": "complete"},
                    "exclude_object": {"excluded_objects": ["OBJECT_1_ID_0_COPY_0"]},
                }
            ),
        )
        attempt.refresh_from_db()
        self.assertTrue(attempt.is_awaiting_review)
        self.assertEqual(attempt.exclusions.count(), 1)

    def test_failing_exclusion_recording_does_not_block_the_end(self) -> None:
        attempt = add_attempt(self.plate1, None, "printing")
        event = _status_event(
            {"print_stats": {"state": "complete"}, "exclude_object": {"excluded_objects": ["OBJECT_1_ID_0_COPY_0"]}}
        )
        with (
            mock.patch.object(PrintQueue, "record_excluded_objects", side_effect=RuntimeError("boom")),
            self.assertLogs("core.services.printer_status_sync", "ERROR"),
        ):
            self.assertTrue(apply_status_event(attempt, event))
        attempt.refresh_from_db()
        self.assertTrue(attempt.is_awaiting_review)

    def test_malformed_payload_is_ignored(self) -> None:
        attempt = add_attempt(self.plate1, None, "printing")
        for payload in ({"excluded_objects": "OBJECT_1"}, {"excluded_objects": [1, None]}, "x", {}):
            apply_status_event(attempt, _status_event({"exclude_object": payload}))
        attempt.refresh_from_db()
        self.assertEqual(attempt.excluded_objects, [])

    def test_not_printing_attempt_is_ignored(self) -> None:
        attempt = add_attempt(self.plate1, None, "awaiting_review")
        apply_status_event(attempt, _status_event({"exclude_object": {"excluded_objects": ["OBJECT_1_ID_0_COPY_0"]}}))
        self.assertFalse(attempt.exclusions.exists())

    def test_subscribed(self) -> None:
        from core.services.moonraker_ws import SUBSCRIBE_OBJECTS

        self.assertIn("excluded_objects", SUBSCRIBE_OBJECTS["exclude_object"])


class ReviewDialogExclusionTests(ExclusionTestBase):
    """The printer's review dialog says how many copies were excluded."""

    def test_line_when_excluded(self) -> None:
        attempt = add_attempt(self.plate1, None, "printing")
        attempt.record_excluded_objects(["OBJECT_1_ID_0_COPY_0", "OBJECT_2_ID_1_COPY_0"])
        PrintQueue.objects.filter(pk=attempt.pk).update(completed_at=attempt.started_at)
        attempt.refresh_from_db()
        self.assertIn("2 part(s) excluded", review_dialog(attempt).lines)

    def test_no_line_without_exclusions(self) -> None:
        attempt = add_attempt(self.plate1, None, "awaiting_review")
        self.assertFalse(any("excluded" in line for line in review_dialog(attempt).lines))
