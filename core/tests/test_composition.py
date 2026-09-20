"""Tests for the composition edge layer (ProjectPart, ProjectComponent)."""

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import TestCase

from core.models import Part, Project, ProjectComponent, ProjectPart
from core.models.composition import component_would_create_cycle


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
            ProjectComponent.objects.bulk_create(
                [ProjectComponent(parent_project=truck, child_project=truck)]
            )


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
