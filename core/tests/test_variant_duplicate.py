"""Duplicate-as-variant (Phase 5)."""

from django.test import TestCase

from core.models import (
    HardwarePart,
    Part,
    Project,
    ProjectComponent,
    ProjectHardware,
    ProjectPart,
)


class DuplicateAsVariantModelTests(TestCase):
    """Tests for :meth:`Project.duplicate_as_variant`."""

    def _source(self):
        truck = Project.objects.create(name="Truck A", description="base")
        cabin = Project.objects.create(name="Cabin")
        home = Project.objects.create(name="home")
        bolt = Part.objects.create(project=home, name="bolt", quantity=1)
        hp = HardwarePart.objects.create(name="Screw", category="screws", unit_price="0.10")
        ProjectComponent.objects.create(parent_project=truck, child_project=cabin, quantity=2, position=0)
        ProjectPart.objects.create(project=truck, part=bolt, quantity=10, position=0)
        ProjectHardware.objects.create(project=truck, hardware_part=hp, quantity=4)
        return truck, cabin, bolt, hp

    def test_duplicate_copies_edges_and_shares_nodes(self):
        truck, cabin, bolt, hp = self._source()
        variant = truck.duplicate_as_variant("Truck B")

        self.assertNotEqual(variant.pk, truck.pk)
        self.assertEqual(variant.name, "Truck B")
        self.assertEqual(variant.description, "base")
        # component edge copied, pointing at the SAME cabin node
        ce = variant.child_links.get()
        self.assertEqual(ce.child_project_id, cabin.pk)
        self.assertEqual(ce.quantity, 2)
        # part edge copied, referencing the SAME part
        pe = variant.part_links.get()
        self.assertEqual(pe.part_id, bolt.pk)
        self.assertEqual(pe.quantity, 10)
        # hardware assignment copied
        hw = variant.hardware_assignments.get()
        self.assertEqual(hw.hardware_part_id, hp.pk)
        self.assertEqual(hw.quantity, 4)
        # source untouched
        self.assertEqual(truck.child_links.count(), 1)
        self.assertEqual(truck.part_links.count(), 1)
        self.assertEqual(truck.hardware_assignments.count(), 1)

    def test_variant_is_top_level(self):
        truck, *_ = self._source()
        variant = truck.duplicate_as_variant("Truck B")
        self.assertFalse(variant.is_subproject)  # no incoming component edge

    def test_documents_not_copied_and_created_by_recorded(self):
        from django.contrib.auth.models import User

        user = User.objects.create_user(username="dup", password="pw")
        truck, *_ = self._source()
        variant = truck.duplicate_as_variant("Truck B", created_by=user)
        self.assertEqual(variant.created_by_id, user.pk)
