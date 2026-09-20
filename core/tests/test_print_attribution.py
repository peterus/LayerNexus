"""Per-assembly print attribution (Phase 4)."""

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse

from core.models import Part, PrintJob, PrintJobPart, PrintJobPlate, Project
from core.tests.mixins import TestDataMixin


class TargetAssemblyFieldTests(TestCase):
    """The ``PrintJobPart.target_assembly`` FK (Task 1)."""

    def test_job_part_can_be_attributed_to_assembly(self):
        home = Project.objects.create(name="home")
        truck = Project.objects.create(name="Truck A")
        part = Part.objects.create(project=home, name="bolt", quantity=1)
        job = PrintJob.objects.create()
        jp = PrintJobPart.objects.create(print_job=job, part=part, quantity=5, target_assembly=truck)
        self.assertEqual(jp.target_assembly, truck)

    def test_target_assembly_defaults_null(self):
        home = Project.objects.create(name="home")
        part = Part.objects.create(project=home, name="bolt", quantity=1)
        job = PrintJob.objects.create()
        jp = PrintJobPart.objects.create(print_job=job, part=part, quantity=1)
        self.assertIsNone(jp.target_assembly)

    def test_deleting_assembly_nulls_attribution(self):
        home = Project.objects.create(name="home")
        truck = Project.objects.create(name="Truck A")
        part = Part.objects.create(project=home, name="bolt", quantity=1)
        job = PrintJob.objects.create()
        jp = PrintJobPart.objects.create(print_job=job, part=part, quantity=5, target_assembly=truck)
        truck.delete()
        jp.refresh_from_db()
        self.assertIsNone(jp.target_assembly)


class PrintedForContextTests(TestCase):
    """``Part.printed_quantity_for(assembly)`` (Task 2)."""

    def _completed_job_for(self, part, qty, assembly):
        job = PrintJob.objects.create(status="completed")
        PrintJobPart.objects.create(print_job=job, part=part, quantity=qty, target_assembly=assembly)
        PrintJobPlate.objects.create(print_job=job, plate_number=1, status=PrintJobPlate.STATUS_COMPLETED)
        return job

    def test_printed_quantity_for_filters_by_assembly(self):
        home = Project.objects.create(name="home")
        a = Project.objects.create(name="Truck A")
        b = Project.objects.create(name="Truck B")
        bolt = Part.objects.create(project=home, name="bolt", quantity=1)
        self._completed_job_for(bolt, 10, a)  # 10 printed for A
        self.assertEqual(bolt.printed_quantity_for(a), 10)
        self.assertEqual(bolt.printed_quantity_for(b), 0)  # none for B
        self.assertEqual(bolt.printed_quantity, 10)  # global still counts it

    def test_unattributed_prints_not_counted_for_any_assembly(self):
        home = Project.objects.create(name="home")
        a = Project.objects.create(name="Truck A")
        bolt = Part.objects.create(project=home, name="bolt", quantity=1)
        # Completed but unattributed (target_assembly=None)
        job = PrintJob.objects.create(status="completed")
        PrintJobPart.objects.create(print_job=job, part=bolt, quantity=7)
        PrintJobPlate.objects.create(print_job=job, plate_number=1, status=PrintJobPlate.STATUS_COMPLETED)
        self.assertEqual(bolt.printed_quantity_for(a), 0)
        self.assertEqual(bolt.printed_quantity, 7)  # still in global

    def test_incomplete_jobs_not_counted(self):
        home = Project.objects.create(name="home")
        a = Project.objects.create(name="Truck A")
        bolt = Part.objects.create(project=home, name="bolt", quantity=1)
        job = PrintJob.objects.create(status="printing")
        PrintJobPart.objects.create(print_job=job, part=bolt, quantity=4, target_assembly=a)
        PrintJobPlate.objects.create(print_job=job, plate_number=1, status=PrintJobPlate.STATUS_PRINTING)
        self.assertEqual(bolt.printed_quantity_for(a), 0)


class VariantProgressTests(TestCase):
    """``Project.variant_progress()`` (Task 3)."""

    def _complete(self, part, qty, assembly):
        job = PrintJob.objects.create(status="completed")
        PrintJobPart.objects.create(print_job=job, part=part, quantity=qty, target_assembly=assembly)
        PrintJobPlate.objects.create(print_job=job, plate_number=1, status=PrintJobPlate.STATUS_COMPLETED)

    def test_shared_part_progress_is_per_variant(self):
        from core.models import ProjectPart

        a = Project.objects.create(name="Truck A")
        b = Project.objects.create(name="Truck B")
        home = Project.objects.create(name="home")
        bolt = Part.objects.create(project=home, name="bolt", quantity=1)
        ProjectPart.objects.create(project=a, part=bolt, quantity=10)  # edge count: each truck needs 10
        ProjectPart.objects.create(project=b, part=bolt, quantity=10)
        self._complete(bolt, 10, a)  # printed 10 bolts FOR Truck A

        self.assertEqual(a.variant_progress()["percent"], 100)  # A satisfied
        self.assertEqual(b.variant_progress()["percent"], 0)  # B still needs its own 10

    def test_variant_progress_structure_and_partial(self):
        from core.models import ProjectPart

        a = Project.objects.create(name="Truck A")
        home = Project.objects.create(name="home")
        bolt = Part.objects.create(project=home, name="bolt", quantity=1)
        ProjectPart.objects.create(project=a, part=bolt, quantity=10)  # edge count: 10 needed
        self._complete(bolt, 4, a)  # 4 of 10

        prog = a.variant_progress()
        self.assertEqual(prog["needed"], 10)
        self.assertEqual(prog["printed"], 4)
        self.assertEqual(prog["percent"], 40)
        self.assertEqual(len(prog["parts"]), 1)
        row = prog["parts"][0]
        self.assertEqual(row["part"], bolt)
        self.assertEqual(row["needed"], 10)
        self.assertEqual(row["printed"], 4)
        self.assertEqual(row["remaining"], 6)

    def test_empty_assembly_zero_percent(self):
        empty = Project.objects.create(name="Empty")
        prog = empty.variant_progress()
        self.assertEqual(prog["percent"], 0)
        self.assertEqual(prog["needed"], 0)
        self.assertEqual(prog["parts"], [])

    def test_child_module_edge_multiplier(self):
        from core.models import ProjectComponent, ProjectPart

        truck = Project.objects.create(name="Truck A")
        wheel = Project.objects.create(name="Wheel")
        home = Project.objects.create(name="home")
        bolt = Part.objects.create(project=home, name="bolt", quantity=1)
        ProjectPart.objects.create(project=wheel, part=bolt, quantity=2)  # edge count: 2 per wheel
        ProjectComponent.objects.create(parent_project=truck, child_project=wheel, quantity=4)  # 4 wheels

        prog = truck.variant_progress()
        self.assertEqual(prog["needed"], 8)  # 2 bolts × 4 wheels


class AggregatedStatusPerAssemblyTests(TestCase):
    """``Project.aggregated_status`` completion is per-assembly (Phase 6a, Task 2)."""

    def _complete(self, part, qty, assembly):
        job = PrintJob.objects.create(status="completed")
        PrintJobPart.objects.create(print_job=job, part=part, quantity=qty, target_assembly=assembly)
        PrintJobPlate.objects.create(print_job=job, plate_number=1, status=PrintJobPlate.STATUS_COMPLETED)

    def test_complete_only_when_printed_for_this_assembly(self):
        truck = Project.objects.create(name="Truck")
        other = Project.objects.create(name="Other")
        bolt = Part.objects.create(project=truck, name="bolt", quantity=2)  # edge into truck via dual-write

        # Printed 2, but attributed to a DIFFERENT assembly → truck is not complete.
        self._complete(bolt, 2, other)
        self.assertNotEqual(truck.aggregated_status, Project.STATUS_COMPLETE)

        # Printed 2 FOR truck → complete.
        self._complete(bolt, 2, truck)
        self.assertEqual(truck.aggregated_status, Project.STATUS_COMPLETE)

    def test_in_progress_uses_attributed_prints(self):
        truck = Project.objects.create(name="Truck")
        bolt = Part.objects.create(
            project=truck,
            name="bolt",
            quantity=3,
            filament_used_grams=10.0,
            estimation_status=Part.ESTIMATION_SUCCESS,
        )
        self._complete(bolt, 1, truck)  # 1 of 3 attributed to truck
        self.assertEqual(truck.aggregated_status, Project.STATUS_IN_PROGRESS)


@override_settings(ALLOWED_HOSTS=["testserver"])
class AddPartToJobTargetAssemblyTests(TestDataMixin, TestCase):
    """The add-part-to-job flow captures ``target_assembly`` (Task 4)."""

    def setUp(self):
        super().setUp()
        self.client.login(username="testuser", password="testpass123")
        self.part.stl_file = SimpleUploadedFile("part.stl", b"solid part")
        self.part.save()

    def test_add_part_to_job_sets_target_assembly(self):
        truck = Project.objects.create(name="Truck A", created_by=self.user)
        job = PrintJob.objects.create(created_by=self.user, status=PrintJob.STATUS_DRAFT)
        self.client.post(
            reverse("core:add_part_to_job", kwargs={"part_pk": self.part.pk}),
            {"job": job.pk, "quantity": 2, "target_assembly": truck.pk},
        )
        jp = PrintJobPart.objects.get(print_job=job, part=self.part)
        self.assertEqual(jp.target_assembly_id, truck.pk)

    def test_add_part_without_target_assembly_is_null(self):
        job = PrintJob.objects.create(created_by=self.user, status=PrintJob.STATUS_DRAFT)
        self.client.post(
            reverse("core:add_part_to_job", kwargs={"part_pk": self.part.pk}),
            {"job": job.pk, "quantity": 1},
        )
        jp = PrintJobPart.objects.get(print_job=job, part=self.part)
        self.assertIsNone(jp.target_assembly)

    def test_add_part_with_invalid_target_assembly_ignored(self):
        job = PrintJob.objects.create(created_by=self.user, status=PrintJob.STATUS_DRAFT)
        self.client.post(
            reverse("core:add_part_to_job", kwargs={"part_pk": self.part.pk}),
            {"job": job.pk, "quantity": 1, "target_assembly": 999999},
        )
        jp = PrintJobPart.objects.get(print_job=job, part=self.part)
        self.assertIsNone(jp.target_assembly)


@override_settings(ALLOWED_HOSTS=["testserver"])
class ProjectDetailVariantProgressTests(TestDataMixin, TestCase):
    """Project detail renders the per-variant build progress (Task 5)."""

    def setUp(self):
        super().setUp()
        self.client.login(username="testuser", password="testpass123")

    def test_detail_shows_variant_progress_bar(self):
        truck = Project.objects.create(name="Truck A", created_by=self.user)
        resp = self.client.get(reverse("core:project_detail", kwargs={"pk": truck.pk}))
        self.assertContains(resp, "Build progress")

    def test_detail_shows_per_part_remaining(self):
        # self.project has self.part (needed via dual-write edge), so the per-variant
        # build-progress table renders with a Remaining column (Phase 6a).
        resp = self.client.get(reverse("core:project_detail", kwargs={"pk": self.project.pk}))
        self.assertContains(resp, "Remaining")
