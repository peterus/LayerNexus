"""Tests for the composition edge layer (ProjectPart, ProjectComponent)."""

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import TestCase

from core.models import Part, Project, ProjectComponent, ProjectPart
from core.models.composition import (
    component_would_create_cycle,
    rebuild_composition_edges,
)


class ProjectPartModelTests(TestCase):
    """Structural tests for the ProjectPart through-model.

    In the expand phase ``Part.project`` is still required (NOT NULL), so each
    part is created inside a dedicated *home* project distinct from the ``module``
    it is linked into. Keeping the two projects distinct means the edge asserted
    here never collides with the ``Part.save()`` dual-write edge (which mirrors
    the home project), so these tests stay valid once dual-write lands.
    """

    def test_create_edge_and_reverse_relations(self):
        home = Project.objects.create(name="Library")
        module = Project.objects.create(name="Cabin")
        part = Part.objects.create(project=home, name="Bracket")
        link = ProjectPart.objects.create(project=module, part=part, quantity=4, position=1)
        self.assertEqual(link.quantity, 4)
        self.assertIn(link, module.part_links.all())
        self.assertIn(link, part.project_links.all())

    def test_unique_project_part(self):
        home = Project.objects.create(name="Library")
        module = Project.objects.create(name="Cabin")
        part = Part.objects.create(project=home, name="Bracket")
        ProjectPart.objects.create(project=module, part=part)
        with self.assertRaises(IntegrityError), transaction.atomic():
            ProjectPart.objects.create(project=module, part=part)

    def test_quantity_must_be_positive(self):
        home = Project.objects.create(name="Library")
        module = Project.objects.create(name="Cabin")
        part = Part.objects.create(project=home, name="Bracket")
        with self.assertRaises(IntegrityError), transaction.atomic():
            ProjectPart.objects.create(project=module, part=part, quantity=0)


class ProjectComponentModelTests(TestCase):
    """Structural tests for the ProjectComponent through-model."""

    def test_create_edge_and_reverse_relations(self):
        truck = Project.objects.create(name="Truck A")
        cabin = Project.objects.create(name="Cabin")
        edge = ProjectComponent.objects.create(parent_project=truck, child_project=cabin, quantity=2)
        self.assertEqual(edge.quantity, 2)
        self.assertIn(edge, truck.child_links.all())
        self.assertIn(edge, cabin.parent_links.all())

    def test_unique_parent_child(self):
        truck = Project.objects.create(name="Truck A")
        cabin = Project.objects.create(name="Cabin")
        ProjectComponent.objects.create(parent_project=truck, child_project=cabin)
        with self.assertRaises(IntegrityError), transaction.atomic():
            ProjectComponent.objects.create(parent_project=truck, child_project=cabin)

    def test_self_parent_rejected_by_db(self):
        # The DB-level check constraint rejects a self-parent edge even when the
        # application-level cycle guard on save() is bypassed (bulk_create).
        truck = Project.objects.create(name="Truck A")
        with self.assertRaises(IntegrityError), transaction.atomic():
            ProjectComponent.objects.bulk_create([ProjectComponent(parent_project=truck, child_project=truck)])


class ProjectComponentCycleTests(TestCase):
    """The composition graph must stay acyclic."""

    def test_direct_cycle_rejected(self):
        a = Project.objects.create(name="A")
        b = Project.objects.create(name="B")
        ProjectComponent.objects.create(parent_project=a, child_project=b)
        edge = ProjectComponent(parent_project=b, child_project=a)
        with self.assertRaises(ValidationError):
            edge.full_clean()

    def test_transitive_cycle_rejected_on_save(self):
        a = Project.objects.create(name="A")
        b = Project.objects.create(name="B")
        c = Project.objects.create(name="C")
        ProjectComponent.objects.create(parent_project=a, child_project=b)
        ProjectComponent.objects.create(parent_project=b, child_project=c)
        # c -> a would close the loop a -> b -> c -> a
        with self.assertRaises(ValidationError):
            ProjectComponent.objects.create(parent_project=c, child_project=a)

    def test_shared_child_is_not_a_cycle(self):
        # Two assemblies sharing the same child is legal (this is the whole point).
        a = Project.objects.create(name="Truck A")
        b = Project.objects.create(name="Truck B")
        cabin = Project.objects.create(name="Cabin")
        ProjectComponent.objects.create(parent_project=a, child_project=cabin)
        ProjectComponent.objects.create(parent_project=b, child_project=cabin)  # must not raise
        self.assertEqual(cabin.parent_links.count(), 2)

    def test_cycle_helper_terminates_on_corrupt_graph(self):
        a = Project.objects.create(name="A")
        b = Project.objects.create(name="B")
        # Force a corrupt cycle bypassing validation.
        ProjectComponent.objects.create(parent_project=a, child_project=b)
        ProjectComponent.objects.bulk_create([ProjectComponent(parent_project=b, child_project=a)])
        # Must return a bool without RecursionError.
        self.assertIsInstance(component_would_create_cycle(a.pk, b.pk), bool)


class RebuildCompositionEdgesTests(TestCase):
    """The backfill helper mirrors the legacy FK graph into edges, idempotently."""

    def _rebuild(self):
        rebuild_composition_edges(Project, Part, ProjectComponent, ProjectPart)

    def test_backfills_part_edges_with_quantity(self):
        module = Project.objects.create(name="Cabin")
        Part.objects.create(project=module, name="Bracket", quantity=5)
        ProjectPart.objects.all().delete()  # clear dual-write output to test the helper alone
        self._rebuild()
        link = ProjectPart.objects.get(project=module)
        self.assertEqual(link.part.name, "Bracket")
        self.assertEqual(link.quantity, 5)

    def test_backfills_component_edges_with_quantity(self):
        truck = Project.objects.create(name="Truck A")
        Project.objects.create(name="Cabin", parent=truck, quantity=2)
        ProjectComponent.objects.all().delete()
        self._rebuild()
        edge = ProjectComponent.objects.get(parent_project=truck)
        self.assertEqual(edge.child_project.name, "Cabin")
        self.assertEqual(edge.quantity, 2)

    def test_rebuild_is_idempotent(self):
        module = Project.objects.create(name="Cabin")
        Part.objects.create(project=module, name="Bracket", quantity=1)
        self._rebuild()
        self._rebuild()  # second run must not duplicate or raise
        self.assertEqual(ProjectPart.objects.filter(project=module).count(), 1)


class DualWriteTests(TestCase):
    """Saving via the legacy FKs keeps composition edges in sync."""

    def test_creating_part_creates_edge(self):
        module = Project.objects.create(name="Cabin")
        part = Part.objects.create(project=module, name="Bracket", quantity=3)
        link = ProjectPart.objects.get(part=part)
        self.assertEqual(link.project_id, module.pk)
        self.assertEqual(link.quantity, 3)

    def test_changing_part_quantity_updates_edge(self):
        module = Project.objects.create(name="Cabin")
        part = Part.objects.create(project=module, name="Bracket", quantity=3)
        part.quantity = 7
        part.save()
        self.assertEqual(ProjectPart.objects.get(part=part).quantity, 7)

    def test_reassigning_part_project_moves_edge(self):
        a = Project.objects.create(name="Cabin A")
        b = Project.objects.create(name="Cabin B")
        part = Part.objects.create(project=a, name="Bracket", quantity=1)
        part.project = b
        part.save()
        self.assertEqual(ProjectPart.objects.filter(part=part).count(), 1)
        self.assertEqual(ProjectPart.objects.get(part=part).project_id, b.pk)

    def test_creating_subproject_creates_component_edge(self):
        truck = Project.objects.create(name="Truck A")
        cabin = Project.objects.create(name="Cabin", parent=truck, quantity=2)
        edge = ProjectComponent.objects.get(child_project=cabin)
        self.assertEqual(edge.parent_project_id, truck.pk)
        self.assertEqual(edge.quantity, 2)

    def test_clearing_parent_removes_component_edge(self):
        truck = Project.objects.create(name="Truck A")
        cabin = Project.objects.create(name="Cabin", parent=truck, quantity=1)
        cabin.parent = None
        cabin.save()
        self.assertFalse(ProjectComponent.objects.filter(child_project=cabin).exists())
