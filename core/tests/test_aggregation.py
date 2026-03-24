"""Tests for project aggregation logic (descendants, status, documents, hardware)."""

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase

from core.models import (
    HardwarePart,
    Part,
    PrintJob,
    PrintJobPart,
    PrintJobPlate,
    Project,
    ProjectDocument,
    ProjectHardware,
)
from core.tests.mixins import TestDataMixin


class ProjectGetDescendantIdsTests(TestCase):
    """Tests for Project.get_descendant_ids() method."""

    def test_no_descendants(self):
        """A project without sub-projects returns an empty set."""
        project = Project.objects.create(name="Alone")
        self.assertEqual(project.get_descendant_ids(), set())

    def test_single_level_descendants(self):
        """Direct sub-projects are included."""
        parent = Project.objects.create(name="Parent")
        child1 = Project.objects.create(name="Child 1", parent=parent)
        child2 = Project.objects.create(name="Child 2", parent=parent)
        self.assertEqual(parent.get_descendant_ids(), {child1.pk, child2.pk})

    def test_nested_descendants(self):
        """Deeply nested sub-projects are included recursively."""
        root = Project.objects.create(name="Root")
        child = Project.objects.create(name="Child", parent=root)
        grandchild = Project.objects.create(name="Grandchild", parent=child)
        self.assertEqual(root.get_descendant_ids(), {child.pk, grandchild.pk})


class ProjectAggregatedStatusTests(TestDataMixin, TestCase):
    """Tests for the Project.aggregated_status property."""

    def test_empty_project(self):
        """A project with no parts returns 'empty'."""
        empty = Project.objects.create(name="Empty", created_by=self.user)
        self.assertEqual(empty.aggregated_status, Project.STATUS_EMPTY)
        self.assertEqual(empty.aggregated_status_display, "Empty")

    def test_pending_status(self):
        """Parts without filament estimates result in 'pending'."""
        proj = Project.objects.create(name="Pending", created_by=self.user)
        Part.objects.create(project=proj, name="P1", quantity=1, filament_used_grams=None)
        self.assertEqual(proj.aggregated_status, Project.STATUS_PENDING)

    def test_ready_status(self):
        """All parts estimated but none printed → 'ready'."""
        proj = Project.objects.create(name="Ready", created_by=self.user)
        Part.objects.create(
            project=proj,
            name="P1",
            quantity=1,
            filament_used_grams=10.0,
            estimation_status=Part.ESTIMATION_SUCCESS,
        )
        self.assertEqual(proj.aggregated_status, Project.STATUS_READY)

    def test_in_progress_status(self):
        """Some parts printed → 'in_progress'."""
        proj = Project.objects.create(name="InProgress", created_by=self.user)
        part = Part.objects.create(
            project=proj,
            name="P1",
            quantity=3,
            filament_used_grams=10.0,
            estimation_status=Part.ESTIMATION_SUCCESS,
        )
        job = PrintJob.objects.create(status="completed", created_by=self.user)
        PrintJobPart.objects.create(print_job=job, part=part, quantity=1)
        PrintJobPlate.objects.create(print_job=job, plate_number=1, status=PrintJobPlate.STATUS_COMPLETED)
        self.assertEqual(proj.aggregated_status, Project.STATUS_IN_PROGRESS)

    def test_complete_status(self):
        """All parts fully printed → 'complete'."""
        proj = Project.objects.create(name="Complete", created_by=self.user)
        part = Part.objects.create(
            project=proj,
            name="P1",
            quantity=2,
            filament_used_grams=10.0,
            estimation_status=Part.ESTIMATION_SUCCESS,
        )
        job = PrintJob.objects.create(status="completed", created_by=self.user)
        PrintJobPart.objects.create(print_job=job, part=part, quantity=2)
        PrintJobPlate.objects.create(print_job=job, plate_number=1, status=PrintJobPlate.STATUS_COMPLETED)
        self.assertEqual(proj.aggregated_status, Project.STATUS_COMPLETE)

    def test_error_status(self):
        """Any part with estimation error → 'error'."""
        proj = Project.objects.create(name="Error", created_by=self.user)
        Part.objects.create(
            project=proj,
            name="P1",
            quantity=1,
            estimation_status=Part.ESTIMATION_ERROR,
            estimation_error="OrcaSlicer timeout",
        )
        self.assertEqual(proj.aggregated_status, Project.STATUS_ERROR)

    def test_estimating_status(self):
        """Any part with pending estimation → 'estimating'."""
        proj = Project.objects.create(name="Estimating", created_by=self.user)
        Part.objects.create(
            project=proj,
            name="P1",
            quantity=1,
            estimation_status=Part.ESTIMATION_PENDING,
        )
        self.assertEqual(proj.aggregated_status, Project.STATUS_ESTIMATING)

    def test_estimating_active_status(self):
        """Any part actively estimating → 'estimating'."""
        proj = Project.objects.create(name="Active", created_by=self.user)
        Part.objects.create(
            project=proj,
            name="P1",
            quantity=1,
            estimation_status=Part.ESTIMATION_ESTIMATING,
        )
        self.assertEqual(proj.aggregated_status, Project.STATUS_ESTIMATING)

    def test_error_takes_priority_over_estimating(self):
        """Error status wins even if another part is estimating."""
        proj = Project.objects.create(name="Mixed", created_by=self.user)
        Part.objects.create(
            project=proj,
            name="P1",
            quantity=1,
            estimation_status=Part.ESTIMATION_ERROR,
            estimation_error="fail",
        )
        Part.objects.create(
            project=proj,
            name="P2",
            quantity=1,
            estimation_status=Part.ESTIMATION_PENDING,
        )
        self.assertEqual(proj.aggregated_status, Project.STATUS_ERROR)

    def test_subproject_parts_included(self):
        """Parts in sub-projects contribute to parent's status."""
        parent = Project.objects.create(name="Parent", created_by=self.user)
        child = Project.objects.create(name="Child", parent=parent, quantity=2, created_by=self.user)
        Part.objects.create(
            project=child,
            name="P1",
            quantity=1,
            filament_used_grams=5.0,
            estimation_status=Part.ESTIMATION_SUCCESS,
        )
        self.assertEqual(parent.aggregated_status, Project.STATUS_READY)

    def test_subproject_error_propagates(self):
        """Error in a sub-project part propagates to the parent."""
        parent = Project.objects.create(name="Parent", created_by=self.user)
        child = Project.objects.create(name="Child", parent=parent, quantity=1, created_by=self.user)
        Part.objects.create(
            project=child,
            name="P1",
            quantity=1,
            estimation_status=Part.ESTIMATION_ERROR,
            estimation_error="fail",
        )
        self.assertEqual(parent.aggregated_status, Project.STATUS_ERROR)


class ProjectDocumentAggregationTests(TestDataMixin, TestCase):
    """Test document aggregation across sub-projects."""

    def test_collect_documents_includes_subproject_docs(self):
        sub = Project.objects.create(name="SubProject", parent=self.project, quantity=2)
        fake_file = SimpleUploadedFile("main.pdf", b"content")
        ProjectDocument.objects.create(project=self.project, name="Main Doc", file=fake_file)
        fake_file2 = SimpleUploadedFile("sub.pdf", b"content")
        ProjectDocument.objects.create(project=sub, name="Sub Doc", file=fake_file2)

        all_docs = self.project._collect_documents()
        self.assertEqual(len(all_docs), 2)
        names = {doc.name for doc, _ in all_docs}
        self.assertIn("Main Doc", names)
        self.assertIn("Sub Doc", names)

    def test_collect_documents_returns_project_objects(self):
        """Second element of each tuple should be the owning Project instance."""
        sub = Project.objects.create(name="SubProject", parent=self.project, quantity=2)
        fake_file = SimpleUploadedFile("main.pdf", b"content")
        ProjectDocument.objects.create(project=self.project, name="Main Doc", file=fake_file)
        fake_file2 = SimpleUploadedFile("sub.pdf", b"content")
        ProjectDocument.objects.create(project=sub, name="Sub Doc", file=fake_file2)

        all_docs = self.project._collect_documents()
        project_ids = {proj.pk for _, proj in all_docs}
        self.assertIn(self.project.pk, project_ids)
        self.assertIn(sub.pk, project_ids)


class ProjectHardwareAggregationTests(TestDataMixin, TestCase):
    """Tests for hardware aggregation across sub-projects."""

    def test_collect_hardware_with_multiplier(self):
        sub = Project.objects.create(name="Sub", parent=self.project, quantity=3)
        hp = HardwarePart.objects.create(name="Bolt", category="bolts", unit_price="0.50")
        ProjectHardware.objects.create(project=sub, hardware_part=hp, quantity=4)

        hw_list = self.project._collect_hardware_with_multiplier()
        self.assertEqual(len(hw_list), 1)
        hw, mult = hw_list[0]
        self.assertEqual(hw.quantity, 4)
        self.assertEqual(mult, 3)  # sub-project quantity

    def test_total_hardware_cost_with_multiplier(self):
        sub = Project.objects.create(name="Sub", parent=self.project, quantity=2)
        hp = HardwarePart.objects.create(name="Screw", category="screws", unit_price="0.10")
        ProjectHardware.objects.create(project=self.project, hardware_part=hp, quantity=10)
        ProjectHardware.objects.create(project=sub, hardware_part=hp, quantity=5)

        # project: 10 × 0.10 × 1 = 1.00
        # sub:      5 × 0.10 × 2 = 1.00
        # total: 2.00
        self.assertAlmostEqual(self.project.total_hardware_cost, 2.00)

    def test_hardware_requirements_grouping(self):
        sub = Project.objects.create(name="Sub", parent=self.project, quantity=2)
        hp = HardwarePart.objects.create(name="Nut", category="nuts", unit_price="0.05")
        ProjectHardware.objects.create(project=self.project, hardware_part=hp, quantity=10)
        ProjectHardware.objects.create(project=sub, hardware_part=hp, quantity=5)

        reqs = self.project.hardware_requirements()
        self.assertEqual(len(reqs), 1)
        self.assertEqual(reqs[0]["total_quantity"], 20)  # 10×1 + 5×2
        self.assertAlmostEqual(reqs[0]["total_price"], 1.00)

    def test_total_hardware_cost_skips_none_prices(self):
        hp = HardwarePart.objects.create(name="Custom", category="other", unit_price=None)
        ProjectHardware.objects.create(project=self.project, hardware_part=hp, quantity=10)
        self.assertEqual(self.project.total_hardware_cost, 0.0)
