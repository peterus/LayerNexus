"""Tests for the composition edge layer (ProjectPart, ProjectComponent)."""

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import TestCase

from core.models import Part, Project, ProjectComponent, ProjectPart
from core.models.composition import component_would_create_cycle


class ProjectPartModelTests(TestCase):
    """Structural tests for the ProjectPart through-model."""

    def test_create_edge_and_reverse_relations(self):
        module = Project.objects.create(name="Cabin")
        part = Part.objects.create(name="Bracket")
        link = ProjectPart.objects.create(project=module, part=part, quantity=4, position=1)
        self.assertEqual(link.quantity, 4)
        self.assertIn(link, module.part_links.all())
        self.assertIn(link, part.project_links.all())

    def test_unique_project_part(self):
        module = Project.objects.create(name="Cabin")
        part = Part.objects.create(name="Bracket")
        ProjectPart.objects.create(project=module, part=part)
        with self.assertRaises(IntegrityError), transaction.atomic():
            ProjectPart.objects.create(project=module, part=part)

    def test_quantity_must_be_positive(self):
        module = Project.objects.create(name="Cabin")
        part = Part.objects.create(name="Bracket")
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

    def test_resaving_existing_edge_is_not_a_false_cycle(self):
        # The reachability walk is descendant-only (follows parent->child edges from the
        # child downward); it never traverses the incoming edge being validated, so
        # re-saving an unchanged edge must not raise a spurious cycle error.
        a = Project.objects.create(name="A")
        b = Project.objects.create(name="B")
        edge = ProjectComponent.objects.create(parent_project=a, child_project=b, quantity=1)
        edge.quantity = 5
        edge.save()  # must not raise
        edge.full_clean()  # clean() must not raise a false positive either
        self.assertEqual(ProjectComponent.objects.get(pk=edge.pk).quantity, 5)

    def test_helper_false_for_existing_edge(self):
        a = Project.objects.create(name="A")
        b = Project.objects.create(name="B")
        ProjectComponent.objects.create(parent_project=a, child_project=b)
        # Adding/keeping the already-present a -> b edge is not a cycle.
        self.assertFalse(component_would_create_cycle(a.pk, b.pk))


class ProjectSaveDoesNotTouchEdgesTests(TestCase):
    """Regression: ``Project.save()`` must never *delete* ``ProjectComponent`` edges.

    A leftover Phase-6 dual-write shim in :meth:`Project.save` deleted the parent
    edges of any project whose legacy ``parent`` FK was ``None`` (always the case for
    edge-based projects), so a plain GUI/API edit silently wiped composition edges and
    corrupted assemblies. Edge reconciliation on save is now additive only (upsert when
    the legacy ``parent`` FK is set); saving an edge-based project must leave its
    composition edges untouched.
    """

    def test_save_keeps_incoming_component_edges(self):
        truck = Project.objects.create(name="Truck")
        cabin = Project.objects.create(name="Cabin")
        ProjectComponent.objects.create(parent_project=truck, child_project=cabin, quantity=2)

        # A plain edit + save of the child (its legacy parent FK is None).
        cabin.name = "Cabin v2"
        cabin.save()

        self.assertEqual(cabin.parent_links.count(), 1)
        edge = cabin.parent_links.get()
        self.assertEqual(edge.parent_project_id, truck.pk)
        self.assertEqual(edge.quantity, 2)

    def test_save_keeps_outgoing_component_edges(self):
        truck = Project.objects.create(name="Truck")
        cabin = Project.objects.create(name="Cabin")
        ProjectComponent.objects.create(parent_project=truck, child_project=cabin, quantity=3)

        # Saving the assembly must not disturb the edges it owns either.
        truck.name = "Truck v2"
        truck.save()

        self.assertEqual(truck.child_links.count(), 1)
        self.assertEqual(truck.child_links.get().child_project_id, cabin.pk)

    def test_save_does_not_clobber_edge_quantity_of_legacy_parent(self):
        # A project with a legacy parent FK gets its mirror edge seeded once; if the
        # edge quantity is later changed through the edge UI, a subsequent scalar save
        # of the project must NOT revert it to the stale legacy Project.quantity.
        parent = Project.objects.create(name="Assembly")
        child = Project.objects.create(name="Module", parent=parent, quantity=1)
        edge = child.parent_links.get()
        self.assertEqual(edge.quantity, 1)

        # Edge UI changes only the edge quantity (not the legacy Project.quantity).
        edge.quantity = 7
        edge.save()

        # An ordinary scalar edit of the child must leave the edge quantity intact.
        child.name = "Module v2"
        child.save()

        edge.refresh_from_db()
        self.assertEqual(edge.quantity, 7)
        self.assertEqual(child.parent_links.count(), 1)

    def test_save_keeps_edges_across_shared_module(self):
        # A module shared by two assemblies: saving it must keep BOTH parent edges.
        truck_a = Project.objects.create(name="Truck A")
        truck_b = Project.objects.create(name="Truck B")
        cabin = Project.objects.create(name="Cabin")
        ProjectComponent.objects.create(parent_project=truck_a, child_project=cabin)
        ProjectComponent.objects.create(parent_project=truck_b, child_project=cabin)

        cabin.description = "shared module"
        cabin.save()

        self.assertEqual(cabin.parent_links.count(), 2)
