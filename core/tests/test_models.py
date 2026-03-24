"""Tests for core models."""

from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import IntegrityError
from django.test import TestCase, override_settings

from core.models import (
    CostProfile,
    FileVersion,
    HardwarePart,
    Part,
    PrinterProfile,
    PrintJob,
    PrintJobPart,
    PrintJobPlate,
    PrintQueue,
    PrintTimeEstimate,
    Project,
    ProjectDocument,
    ProjectHardware,
)
from core.tests.mixins import TestDataMixin


class ProjectModelTests(TestDataMixin, TestCase):
    """Tests for the Project model."""

    def test_str(self):
        self.assertEqual(str(self.project), "Test Project")

    def test_total_parts_count(self):
        self.assertEqual(self.project.total_parts_count, 3)

    def test_total_parts_count_multiple(self):
        Part.objects.create(project=self.project, name="Part B", quantity=5)
        self.assertEqual(self.project.total_parts_count, 8)

    def test_printed_parts_count_no_jobs(self):
        self.assertEqual(self.project.printed_parts_count, 0)

    def test_printed_parts_count_with_completed_jobs(self):
        job = PrintJob.objects.create(status="completed", created_by=self.user)
        PrintJobPart.objects.create(print_job=job, part=self.part, quantity=2)
        PrintJobPlate.objects.create(print_job=job, plate_number=1, status=PrintJobPlate.STATUS_COMPLETED)
        self.assertEqual(self.project.printed_parts_count, 2)

    def test_printed_parts_count_ignores_non_completed(self):
        job = PrintJob.objects.create(status="failed", created_by=self.user)
        PrintJobPart.objects.create(print_job=job, part=self.part, quantity=1)
        self.assertEqual(self.project.printed_parts_count, 0)

    def test_progress_percent_no_parts(self):
        empty = Project.objects.create(name="Empty", created_by=self.user)
        self.assertEqual(empty.progress_percent, 0)

    def test_progress_percent_partial(self):
        job = PrintJob.objects.create(status="completed", created_by=self.user)
        PrintJobPart.objects.create(print_job=job, part=self.part, quantity=1)
        PrintJobPlate.objects.create(print_job=job, plate_number=1, status=PrintJobPlate.STATUS_COMPLETED)
        # 1 completed out of 3 total = 33%
        self.assertEqual(self.project.progress_percent, 33)

    def test_progress_percent_complete(self):
        job = PrintJob.objects.create(status="completed", created_by=self.user)
        PrintJobPart.objects.create(print_job=job, part=self.part, quantity=3)
        PrintJobPlate.objects.create(print_job=job, plate_number=1, status=PrintJobPlate.STATUS_COMPLETED)
        self.assertEqual(self.project.progress_percent, 100)

    def test_total_filament_grams(self):
        # part has filament_used_grams=10.5, quantity=3 => 31.5
        self.assertAlmostEqual(self.project.total_filament_grams, 31.5)

    def test_total_filament_grams_excludes_null(self):
        Part.objects.create(
            project=self.project,
            name="No filament",
            quantity=2,
            filament_used_grams=None,
        )
        self.assertAlmostEqual(self.project.total_filament_grams, 31.5)

    def test_total_filament_meters(self):
        # part has filament_used_meters=3.4, quantity=3 => 10.2
        self.assertAlmostEqual(self.project.total_filament_meters, 10.2)


class PartModelTests(TestDataMixin, TestCase):
    """Tests for the Part model."""

    def test_str(self):
        self.assertEqual(str(self.part), "Test Part (Test Project)")

    def test_color_display_standard(self):
        self.assertEqual(self.part.color_display, "red")

    def test_color_display_with_value(self):
        self.part.color = "Neon Pink"
        self.assertEqual(self.part.color_display, "Neon Pink")

    def test_color_display_empty(self):
        self.part.color = ""
        self.assertEqual(self.part.color_display, "—")

    def test_printed_quantity_no_jobs(self):
        self.assertEqual(self.part.printed_quantity, 0)

    def test_printed_quantity_with_completed(self):
        job = PrintJob.objects.create(status="completed", created_by=self.user)
        PrintJobPart.objects.create(print_job=job, part=self.part, quantity=2)
        PrintJobPlate.objects.create(print_job=job, plate_number=1, status=PrintJobPlate.STATUS_COMPLETED)
        self.assertEqual(self.part.printed_quantity, 2)

    def test_printed_quantity_multiple_jobs(self):
        job1 = PrintJob.objects.create(status="completed", created_by=self.user)
        PrintJobPart.objects.create(print_job=job1, part=self.part, quantity=1)
        PrintJobPlate.objects.create(print_job=job1, plate_number=1, status=PrintJobPlate.STATUS_COMPLETED)
        job2 = PrintJob.objects.create(status="completed", created_by=self.user)
        PrintJobPart.objects.create(print_job=job2, part=self.part, quantity=1)
        PrintJobPlate.objects.create(print_job=job2, plate_number=1, status=PrintJobPlate.STATUS_COMPLETED)
        self.assertEqual(self.part.printed_quantity, 2)

    def test_remaining_quantity(self):
        job = PrintJob.objects.create(status="completed", created_by=self.user)
        PrintJobPart.objects.create(print_job=job, part=self.part, quantity=1)
        PrintJobPlate.objects.create(print_job=job, plate_number=1, status=PrintJobPlate.STATUS_COMPLETED)
        self.assertEqual(self.part.remaining_quantity, 2)

    def test_remaining_quantity_never_negative(self):
        job = PrintJob.objects.create(status="completed", created_by=self.user)
        PrintJobPart.objects.create(print_job=job, part=self.part, quantity=10)
        PrintJobPlate.objects.create(print_job=job, plate_number=1, status=PrintJobPlate.STATUS_COMPLETED)
        self.assertEqual(self.part.remaining_quantity, 0)

    def test_is_complete_false(self):
        self.assertFalse(self.part.is_complete)

    def test_is_complete_true(self):
        job = PrintJob.objects.create(status="completed", created_by=self.user)
        PrintJobPart.objects.create(print_job=job, part=self.part, quantity=3)
        PrintJobPlate.objects.create(print_job=job, plate_number=1, status=PrintJobPlate.STATUS_COMPLETED)
        self.assertTrue(self.part.is_complete)

    def test_estimation_status_default(self):
        """New parts should have estimation_status 'none' by default."""
        self.assertEqual(self.part.estimation_status, Part.ESTIMATION_NONE)
        self.assertEqual(self.part.estimation_error, "")

    def test_estimation_status_error(self):
        """Parts can store estimation error status and message."""
        self.part.estimation_status = Part.ESTIMATION_ERROR
        self.part.estimation_error = "OrcaSlicer API unreachable"
        self.part.save()
        self.part.refresh_from_db()
        self.assertEqual(self.part.estimation_status, Part.ESTIMATION_ERROR)
        self.assertEqual(self.part.estimation_error, "OrcaSlicer API unreachable")

    def test_estimation_status_success(self):
        """Parts can store successful estimation status."""
        self.part.estimation_status = Part.ESTIMATION_SUCCESS
        self.part.estimation_error = ""
        self.part.save()
        self.part.refresh_from_db()
        self.assertEqual(self.part.estimation_status, Part.ESTIMATION_SUCCESS)

    def test_estimation_status_estimating(self):
        """Parts can store actively-estimating status."""
        self.part.estimation_status = Part.ESTIMATION_ESTIMATING
        self.part.save()
        self.part.refresh_from_db()
        self.assertEqual(self.part.estimation_status, Part.ESTIMATION_ESTIMATING)


class PrinterProfileModelTests(TestDataMixin, TestCase):
    """Tests for the PrinterProfile model."""

    def test_str(self):
        self.assertEqual(str(self.printer), "Test Printer")

    def test_default_values(self):
        p = PrinterProfile.objects.create(name="Defaults", created_by=self.user)
        self.assertIsNone(p.bed_size_x)
        self.assertIsNone(p.nozzle_diameter)


class PrintJobModelTests(TestDataMixin, TestCase):
    """Tests for the PrintJob model."""

    def test_str_with_name(self):
        job = PrintJob.objects.create(name="My Job", status="draft", created_by=self.user)
        self.assertIn("My Job", str(job))

    def test_str_without_name(self):
        job = PrintJob.objects.create(status="draft", created_by=self.user)
        self.assertIn(f"Job #{job.pk}", str(job))

    def test_default_status(self):
        job = PrintJob.objects.create(created_by=self.user)
        self.assertEqual(job.status, "draft")

    def test_total_part_count(self):
        job = PrintJob.objects.create(created_by=self.user)
        PrintJobPart.objects.create(print_job=job, part=self.part, quantity=3)
        self.assertEqual(job.total_part_count, 3)

    def test_plate_count(self):
        job = PrintJob.objects.create(created_by=self.user)
        PrintJobPlate.objects.create(print_job=job, plate_number=1)
        PrintJobPlate.objects.create(print_job=job, plate_number=2)
        self.assertEqual(job.plate_count, 2)

    def test_all_plates_completed(self):
        job = PrintJob.objects.create(created_by=self.user)
        PrintJobPlate.objects.create(print_job=job, plate_number=1, status=PrintJobPlate.STATUS_COMPLETED)
        self.assertTrue(job.all_plates_completed)

    def test_all_plates_completed_false_when_waiting(self):
        job = PrintJob.objects.create(created_by=self.user)
        PrintJobPlate.objects.create(print_job=job, plate_number=1, status=PrintJobPlate.STATUS_COMPLETED)
        PrintJobPlate.objects.create(print_job=job, plate_number=2, status=PrintJobPlate.STATUS_WAITING)
        self.assertFalse(job.all_plates_completed)


class PrintJobPartModelTests(TestDataMixin, TestCase):
    """Tests for the PrintJobPart through model."""

    def test_str(self):
        job = PrintJob.objects.create(name="Test Job", created_by=self.user)
        jp = PrintJobPart.objects.create(print_job=job, part=self.part, quantity=2)
        self.assertIn("Test Part", str(jp))

    def test_unique_together(self):
        job = PrintJob.objects.create(name="Test Job", created_by=self.user)
        PrintJobPart.objects.create(print_job=job, part=self.part, quantity=1)
        from django.db import IntegrityError

        with self.assertRaises(IntegrityError):
            PrintJobPart.objects.create(print_job=job, part=self.part, quantity=1)


@override_settings(ALLOWED_HOSTS=["testserver"])
class CostProfileModelTests(TestDataMixin, TestCase):
    """Tests for CostProfile model."""

    def setUp(self):
        super().setUp()
        self.printer = PrinterProfile.objects.create(name="Test Printer", created_by=self.user)
        self.cost = CostProfile.objects.create(
            printer=self.printer,
            electricity_cost_per_kwh=0.30,
            printer_power_watts=200,
            printer_purchase_cost=500,
            printer_lifespan_hours=5000,
            maintenance_cost_per_hour=0.05,
        )

    def test_str(self):
        self.assertEqual(str(self.cost), "Cost Profile: Test Printer")

    def test_depreciation_per_hour(self):
        self.assertAlmostEqual(self.cost.depreciation_per_hour, 0.1)

    def test_electricity_per_hour(self):
        self.assertAlmostEqual(self.cost.electricity_per_hour, 0.06)

    def test_calculate_print_cost(self):
        result = self.cost.calculate_print_cost(2.0, 50, 20)
        self.assertIn("filament_cost", result)
        self.assertIn("electricity_cost", result)
        self.assertIn("depreciation_cost", result)
        self.assertIn("maintenance_cost", result)
        self.assertIn("total_cost", result)
        self.assertEqual(result["filament_cost"], 1.0)  # 50g / 1000 * 20
        self.assertEqual(result["electricity_cost"], 0.12)  # 0.06 * 2
        self.assertEqual(result["depreciation_cost"], 0.2)  # 0.1 * 2
        self.assertEqual(result["maintenance_cost"], 0.1)  # 0.05 * 2

    def test_depreciation_zero_lifespan(self):
        self.cost.printer_lifespan_hours = 0
        self.cost.save()
        self.assertEqual(self.cost.depreciation_per_hour, 0)


@override_settings(ALLOWED_HOSTS=["testserver"])
class PrintQueueModelTests(TestDataMixin, TestCase):
    """Tests for PrintQueue model."""

    def setUp(self):
        super().setUp()
        self.printer = PrinterProfile.objects.create(name="Test Printer", created_by=self.user)
        self.job = PrintJob.objects.create(
            name="Queue Test Job",
            status="sliced",
            created_by=self.user,
        )
        PrintJobPart.objects.create(print_job=self.job, part=self.part, quantity=1)
        self.plate = PrintJobPlate.objects.create(print_job=self.job, plate_number=1)

    def test_str(self):
        entry = PrintQueue.objects.create(plate=self.plate, printer=self.printer, priority=3, position=0)
        self.assertIn("High", str(entry))

    def test_ordering(self):
        """Higher priority should come first."""
        plate2 = PrintJobPlate.objects.create(print_job=self.job, plate_number=2)
        PrintQueue.objects.create(plate=self.plate, printer=self.printer, priority=1, position=0)
        e2 = PrintQueue.objects.create(plate=plate2, printer=self.printer, priority=4, position=0)
        entries = list(PrintQueue.objects.all())
        self.assertEqual(entries[0], e2)  # Urgent first


@override_settings(ALLOWED_HOSTS=["testserver"])
class PrintTimeEstimateModelTests(TestDataMixin, TestCase):
    """Tests for PrintTimeEstimate model."""

    def setUp(self):
        super().setUp()
        self.printer = PrinterProfile.objects.create(name="Test Printer", created_by=self.user)

    def test_accuracy_factor(self):
        from datetime import timedelta

        est = PrintTimeEstimate.objects.create(
            part=self.part,
            printer=self.printer,
            estimated_time=timedelta(hours=2),
            actual_time=timedelta(hours=2, minutes=30),
        )
        self.assertAlmostEqual(est.accuracy_factor, 1.25)

    def test_accuracy_factor_no_actual(self):
        from datetime import timedelta

        est = PrintTimeEstimate.objects.create(
            part=self.part,
            printer=self.printer,
            estimated_time=timedelta(hours=2),
        )
        self.assertIsNone(est.accuracy_factor)


@override_settings(ALLOWED_HOSTS=["testserver"])
class FileVersionModelTests(TestDataMixin, TestCase):
    """Tests for FileVersion model."""

    def test_str(self):
        fv = FileVersion.objects.create(
            part=self.part,
            version=1,
            file_type="stl",
            file="test.stl",
            uploaded_by=self.user,
        )
        self.assertIn("v1", str(fv))
        self.assertIn("stl", str(fv))


class ProjectDocumentModelTests(TestDataMixin, TestCase):
    """Tests for the ProjectDocument model."""

    def _create_doc(self, name: str = "readme.pdf", filename: str = "readme.pdf") -> ProjectDocument:
        fake_file = SimpleUploadedFile(filename, b"fake content", content_type="application/pdf")
        return ProjectDocument.objects.create(
            project=self.project,
            name=name,
            file=fake_file,
            uploaded_by=self.user,
        )

    def test_str(self):
        doc = self._create_doc()
        self.assertEqual(str(doc), "readme.pdf")

    def test_file_extension_property(self):
        doc = self._create_doc(filename="drawing.step")
        self.assertEqual(doc.file_extension, "step")

    def test_ordering(self):
        self._create_doc(name="First")
        doc2 = self._create_doc(name="Second")
        docs = list(self.project.documents.all())
        self.assertEqual(docs[0], doc2)  # newest first

    def test_cascade_delete(self):
        self._create_doc()
        self.assertEqual(ProjectDocument.objects.count(), 1)
        self.project.delete()
        self.assertEqual(ProjectDocument.objects.count(), 0)


class HardwarePartModelTests(TestDataMixin, TestCase):
    """Tests for the HardwarePart model."""

    def test_str(self):
        hp = HardwarePart.objects.create(name="M3x10", category="screws")
        self.assertEqual(str(hp), "Screws: M3x10")

    def test_unique_together(self):
        HardwarePart.objects.create(name="M3x10", category="screws")
        with self.assertRaises(IntegrityError):
            HardwarePart.objects.create(name="M3x10", category="screws")

    def test_different_category_allowed(self):
        HardwarePart.objects.create(name="M3", category="screws")
        hp2 = HardwarePart.objects.create(name="M3", category="nuts")
        self.assertEqual(hp2.category, "nuts")


class ProjectHardwareModelTests(TestDataMixin, TestCase):
    """Tests for the ProjectHardware through model."""

    def _create_hw(self, price=None, qty=5) -> ProjectHardware:
        hp = HardwarePart.objects.create(name="M3x10", category="screws", unit_price=price)
        return ProjectHardware.objects.create(project=self.project, hardware_part=hp, quantity=qty)

    def test_str(self):
        ph = self._create_hw(qty=3)
        self.assertIn("×3", str(ph))

    def test_total_price_with_price(self):
        ph = self._create_hw(price="0.15", qty=10)
        self.assertAlmostEqual(ph.total_price, 1.5)

    def test_total_price_without_price(self):
        ph = self._create_hw(price=None)
        self.assertIsNone(ph.total_price)

    def test_unique_together(self):
        hp = HardwarePart.objects.create(name="M3x10", category="screws")
        ProjectHardware.objects.create(project=self.project, hardware_part=hp, quantity=1)
        with self.assertRaises(IntegrityError):
            ProjectHardware.objects.create(project=self.project, hardware_part=hp, quantity=2)

    def test_reuse_across_projects(self):
        hp = HardwarePart.objects.create(name="Motor", category="motors")
        ProjectHardware.objects.create(project=self.project, hardware_part=hp, quantity=2)
        ProjectHardware.objects.create(project=self.other_project, hardware_part=hp, quantity=4)
        self.assertEqual(hp.project_assignments.count(), 2)
