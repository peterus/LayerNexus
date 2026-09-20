"""Edge-based navigation/display helpers (Phase 3a)."""

from django.test import TestCase

from core.models import Part, Project, ProjectComponent, ProjectPart


class ParentAssembliesTests(TestCase):
    def test_parent_assemblies_lists_all_containing_assemblies(self):
        cabin = Project.objects.create(name="cabin")
        a = Project.objects.create(name="truck-a")
        b = Project.objects.create(name="truck-b")
        ProjectComponent.objects.create(parent_project=a, child_project=cabin)
        ProjectComponent.objects.create(parent_project=b, child_project=cabin)
        names = {p.name for p in cabin.parent_assemblies()}
        self.assertEqual(names, {"truck-a", "truck-b"})
        self.assertEqual(a.parent_assemblies(), [])  # top-level

    def test_is_subproject_is_edge_based(self):
        cabin = Project.objects.create(name="cabin")
        a = Project.objects.create(name="truck-a")
        self.assertFalse(cabin.is_subproject)
        ProjectComponent.objects.create(parent_project=a, child_project=cabin)
        self.assertTrue(cabin.is_subproject)
        self.assertFalse(a.is_subproject)


class ChildAndPartDisplayTests(TestCase):
    def test_child_modules_and_direct_parts_with_quantity(self):
        truck = Project.objects.create(name="truck")
        cabin = Project.objects.create(name="cabin")
        ProjectComponent.objects.create(parent_project=truck, child_project=cabin, quantity=2, position=0)
        bolt = Part.objects.create(name="bolt")
        ProjectPart.objects.create(project=truck, part=bolt, quantity=10, position=0)

        self.assertEqual([(c.name, q) for c, q in truck.child_modules()], [("cabin", 2)])
        self.assertEqual([(p.name, q) for p, q in truck.direct_parts()], [("bolt", 10)])
        self.assertEqual(truck.direct_part_count(), 1)

    def test_part_containing_projects(self):
        bolt = Part.objects.create(name="bolt")
        m1 = Project.objects.create(name="m1")
        m2 = Project.objects.create(name="m2")
        ProjectPart.objects.create(project=m1, part=bolt)
        ProjectPart.objects.create(project=m2, part=bolt)
        self.assertEqual({p.name for p in bolt.containing_projects()}, {"m1", "m2"})
