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


class ProjectAggregatePrefetchTests(TestCase):
    """N+1 regression: aggregate properties must not scale queries with node count."""

    def _make_project_tree(self, name: str) -> Project:
        """Create a top-level project with one sub-project, parts and a completed job."""
        root = Project.objects.create(name=f"{name}-root")
        sub = Project.objects.create(name=f"{name}-sub", parent=root, quantity=2)
        for proj in (root, sub):
            part = Part.objects.create(project=proj, name=f"{proj.name}-p", quantity=2, filament_used_grams=5)
            job = PrintJob.objects.create(status="completed")
            PrintJobPart.objects.create(print_job=job, part=part, quantity=1)
            PrintJobPlate.objects.create(print_job=job, plate_number=1, status=PrintJobPlate.STATUS_COMPLETED)
            PrintJobPlate.objects.create(print_job=job, plate_number=2, status=PrintJobPlate.STATUS_COMPLETED)
        return root

    def _touch_aggregates(self, projects: list[Project]) -> None:
        for p in projects:
            _ = p.total_parts_count
            _ = p.progress_percent
            _ = p.aggregated_status
            _ = p.total_filament_grams

    def test_aggregates_use_no_extra_queries_when_prefetched(self):
        """With the view prefetch applied, evaluating aggregates issues 0 extra queries."""
        for i in range(4):
            self._make_project_tree(f"n{i}")

        qs = Project.objects.filter(parent__isnull=True).prefetch_related(*Project.aggregate_prefetch_lookups())
        projects = list(qs)  # prefetch happens here
        self.assertEqual(len(projects), 4)
        with self.assertNumQueries(0):
            self._touch_aggregates(projects)

    def test_prefetch_flat_at_full_depth(self):
        """A tree as deep as the prefetch depth is fully cache-served (0 extra queries).

        Proves the trailing ``child_links__child_project`` lookup is required: it keeps
        ``.child_links.all()`` at the deepest covered node served from cache
        (empty) instead of firing a query there.
        """
        depth = 3
        node = Project.objects.create(name="d0")
        for level in range(1, depth + 1):
            node = Project.objects.create(name=f"d{level}", parent=node, quantity=1)
            part = Part.objects.create(project=node, name=f"d{level}-p", quantity=1, filament_used_grams=1)
            job = PrintJob.objects.create(status="completed")
            PrintJobPart.objects.create(print_job=job, part=part, quantity=1)
            PrintJobPlate.objects.create(print_job=job, plate_number=1, status=PrintJobPlate.STATUS_COMPLETED)

        qs = Project.objects.filter(parent__isnull=True).prefetch_related(
            *Project.aggregate_prefetch_lookups(depth=depth)
        )
        projects = list(qs)
        with self.assertNumQueries(0):
            self._touch_aggregates(projects)

    def test_query_count_independent_of_node_count(self):
        """Query count for the prefetched list is the same for 2 vs 6 top-level trees."""

        def count_for(prefix: str, n: int) -> int:
            for i in range(n):
                self._make_project_tree(f"{prefix}{i}")
            qs = Project.objects.filter(parent__isnull=True, name__startswith=f"{prefix}").prefetch_related(
                *Project.aggregate_prefetch_lookups()
            )
            from django.db import connection, reset_queries
            from django.test.utils import override_settings

            with override_settings(DEBUG=True):
                reset_queries()
                projects = list(qs)
                self.assertEqual(len(projects), n)
                self._touch_aggregates(projects)
                return len(connection.queries)

        # Distinct name prefixes keep the two measured sets isolated without
        # having to delete PROTECT-ed parent rows between runs.
        self.assertEqual(count_for("small", 2), count_for("large", 6))


class ProjectCycleGuardTests(TestCase):
    """Tests that Project.clean() rejects cyclic parent relationships."""

    def test_parent_self_rejected(self):
        """A project cannot be its own parent."""
        from django.core.exceptions import ValidationError

        project = Project.objects.create(name="Selfie")
        project.parent = project
        with self.assertRaises(ValidationError):
            project.full_clean()

    def test_parent_descendant_rejected(self):
        """A project cannot be re-parented under one of its descendants."""
        from django.core.exceptions import ValidationError

        root = Project.objects.create(name="Root")
        child = Project.objects.create(name="Child", parent=root)
        grandchild = Project.objects.create(name="Grandchild", parent=child)
        # Try to make root a sub-project of its own grandchild → cycle.
        root.parent = grandchild
        with self.assertRaises(ValidationError):
            root.full_clean()

    def test_save_rejects_self_parent(self):
        """A bare ``save()`` (shell/import path) must also reject a self parent."""
        from django.core.exceptions import ValidationError

        project = Project.objects.create(name="Selfie")
        project.parent = project
        with self.assertRaises(ValidationError):
            project.save()

    def test_save_rejects_descendant_parent(self):
        """A bare ``save()`` must reject re-parenting under a descendant (no cycle persisted)."""
        from django.core.exceptions import ValidationError

        root = Project.objects.create(name="Root")
        child = Project.objects.create(name="Child", parent=root)
        grandchild = Project.objects.create(name="Grandchild", parent=child)
        root.parent = grandchild
        with self.assertRaises(ValidationError):
            root.save()
        # Nothing was persisted: root is still a top-level project.
        root.refresh_from_db()
        self.assertIsNone(root.parent_id)

    def test_valid_parent_accepted(self):
        """A normal, acyclic parent assignment passes validation."""
        root = Project.objects.create(name="Root")
        other = Project.objects.create(name="Other")
        other.parent = root
        # Should not raise (exclude unrelated field validation noise).
        other.full_clean(exclude=["image"])

    def test_new_project_without_pk_accepted(self):
        """A brand-new unsaved project with a parent validates fine."""
        root = Project.objects.create(name="Root")
        fresh = Project(name="Fresh", parent=root)
        fresh.full_clean(exclude=["image"])

    def test_descendant_property_guarded_against_corrupt_cycle(self):
        """Even if a cycle is forced into the DB, recursion must not hang.

        ``get_descendant_ids`` traverses the composition ``child_links`` edges with a
        path-local guard, so a corrupt cycle (introduced outside validation, here via
        ``bulk_create`` which bypasses the ``ProjectComponent`` cycle guard) terminates
        instead of recursing forever.
        """
        from core.models import ProjectComponent

        a = Project.objects.create(name="A")
        b = Project.objects.create(name="B")
        ProjectComponent.objects.create(parent_project=a, child_project=b, quantity=1)
        # Force the closing edge b -> a bypassing the save()/clean() cycle guard.
        ProjectComponent.objects.bulk_create([ProjectComponent(parent_project=b, child_project=a, quantity=1)])
        # Must terminate and include both nodes rather than RecursionError.
        ids = a.get_descendant_ids()
        self.assertEqual(ids, {a.pk, b.pk})

    def test_upward_walks_guarded_against_corrupt_cycle(self):
        """Effective-preset parent walks must terminate on a DB cycle."""
        a = Project.objects.create(name="A")
        b = Project.objects.create(name="B", parent=a)
        Project.objects.filter(pk=a.pk).update(parent=b)  # a <-> b cycle
        a.refresh_from_db()
        b.refresh_from_db()
        # These upward parent walks must not loop forever.
        self.assertIsNone(a.effective_default_print_preset)
        self.assertIsNone(a.effective_default_print_preset_id)

    def test_aggregate_collectors_guarded_against_corrupt_cycle(self):
        """A persisted cycle must not RecursionError in the aggregate properties.

        The visited-set guard extends to ``_collect_parts_with_multiplier`` /
        ``_collect_documents`` / ``_collect_hardware_with_multiplier`` so status
        badges and totals render on a corrupt graph instead of blowing the stack.
        """
        a = Project.objects.create(name="A")
        b = Project.objects.create(name="B", parent=a)
        Part.objects.create(project=a, name="ap", quantity=1)
        Part.objects.create(project=b, name="bp", quantity=1)
        Project.objects.filter(pk=a.pk).update(parent=b)  # a <-> b cycle
        a.refresh_from_db()
        # None of these may raise RecursionError.
        self.assertGreaterEqual(a.total_parts_count, 2)
        self.assertIsInstance(a.aggregated_status, str)
        self.assertEqual(a._collect_documents(), [])
        self.assertEqual(a._collect_hardware_with_multiplier(), [])


class ProjectEditFormCycleTests(TestCase):
    """The edit form must reject cyclic re-parenting (defence beyond the queryset)."""

    def test_form_rejects_descendant_parent(self):
        from core.forms import ProjectEditForm

        root = Project.objects.create(name="Root")
        child = Project.objects.create(name="Child", parent=root)
        form = ProjectEditForm(
            data={
                "name": "Root",
                "description": "",
                "parent": child.pk,
                "quantity": 1,
            },
            instance=root,
        )
        self.assertFalse(form.is_valid())
        self.assertIn("parent", form.errors)

    def test_form_rejects_self_parent(self):
        from core.forms import ProjectEditForm

        root = Project.objects.create(name="Root")
        form = ProjectEditForm(
            data={
                "name": "Root",
                "description": "",
                "parent": root.pk,
                "quantity": 1,
            },
            instance=root,
        )
        self.assertFalse(form.is_valid())
        self.assertIn("parent", form.errors)


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
