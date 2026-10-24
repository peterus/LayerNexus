"""Tests for HardwareShoppingItem and the per-project shopping-list logic."""

from django.test import TestCase

from core.models import (
    HardwarePart,
    HardwareShoppingItem,
    Project,
    ProjectComponent,
    ProjectHardware,
)
from core.models.hardware import ShoppingItemNotRequired, ShoppingQuantityChanged, ShoppingRow


class ShoppingListTestBase(TestCase):
    """Fixture: root R (M3 x4) with child C (x2; M3 x3, Bearing x1)."""

    def setUp(self) -> None:
        self.root = Project.objects.create(name="R")
        self.child = Project.objects.create(name="C")
        self.edge = ProjectComponent.objects.create(parent_project=self.root, child_project=self.child, quantity=2)
        self.m3 = HardwarePart.objects.create(name="M3", category=HardwarePart.CATEGORY_SCREWS)
        self.bearing = HardwarePart.objects.create(name="Bearing", category=HardwarePart.CATEGORY_BEARINGS)
        self.magnet = HardwarePart.objects.create(name="Magnet", category=HardwarePart.CATEGORY_MAGNETS)
        self.root_m3 = ProjectHardware.objects.create(project=self.root, hardware_part=self.m3, quantity=4)
        ProjectHardware.objects.create(project=self.child, hardware_part=self.m3, quantity=3)
        ProjectHardware.objects.create(project=self.child, hardware_part=self.bearing, quantity=1)

    def row(self, project: Project, part: HardwarePart) -> ShoppingRow:
        """Return the shopping-list row of ``part`` for ``project``."""
        return next(r for r in HardwareShoppingItem.build_list(project) if r.hardware_part.pk == part.pk)


class BuildListTests(ShoppingListTestBase):
    def test_build_list_sums_over_children_with_edge_quantity(self) -> None:
        self.assertEqual(self.row(self.root, self.m3).quantity, 10)
        self.assertEqual(self.row(self.root, self.bearing).quantity, 2)
        for r in HardwareShoppingItem.build_list(self.root):
            self.assertEqual(r.status, "open")
            self.assertIsNone(r.previous_quantity)

    def test_build_list_merges_part_used_directly_and_in_child(self) -> None:
        rows = [r for r in HardwareShoppingItem.build_list(self.root) if r.hardware_part.pk == self.m3.pk]
        self.assertEqual(len(rows), 1)
        self.assertEqual(sorted(rows[0].projects), ["C", "R"])

    def test_build_list_orders_by_category_choices_then_name(self) -> None:
        names = [r.hardware_part.name for r in HardwareShoppingItem.build_list(self.root)]
        self.assertEqual(names, ["M3", "Bearing"])

    def test_counts_returns_all_three_keys(self) -> None:
        self.assertEqual(HardwareShoppingItem.counts([]), {"open": 0, "in_cart": 0, "ordered": 0})

    def test_next_status_cycle(self) -> None:
        row = self.row(self.root, self.m3)
        self.assertEqual(row.next_status, "in_cart")
        row.status = "in_cart"
        self.assertEqual(row.next_status, "ordered")
        row.status = "ordered"
        self.assertEqual(row.next_status, "open")


class SetStatusTests(ShoppingListTestBase):
    def test_set_status_persists_snapshot_quantity(self) -> None:
        HardwareShoppingItem.set_status(self.root, self.m3.pk, "in_cart")
        item = HardwareShoppingItem.objects.get(project=self.root, hardware_part=self.m3)
        self.assertEqual((item.status, item.quantity), ("in_cart", 10))
        self.assertEqual(self.row(self.root, self.m3).status, "in_cart")

    def test_set_status_open_deletes_row(self) -> None:
        HardwareShoppingItem.set_status(self.root, self.m3.pk, "in_cart")
        HardwareShoppingItem.set_status(self.root, self.m3.pk, "open")
        self.assertEqual(HardwareShoppingItem.objects.count(), 0)

    def test_set_status_same_target_twice_is_idempotent(self) -> None:
        HardwareShoppingItem.set_status(self.root, self.m3.pk, "ordered")
        row = HardwareShoppingItem.set_status(self.root, self.m3.pk, "ordered")
        self.assertEqual(HardwareShoppingItem.objects.count(), 1)
        self.assertEqual(row.status, "ordered")

    def test_set_status_rejects_invalid_status(self) -> None:
        with self.assertRaises(ValueError):
            HardwareShoppingItem.set_status(self.root, self.m3.pk, "bogus")

    def test_set_status_rejects_part_not_required(self) -> None:
        with self.assertRaises(ShoppingItemNotRequired):
            HardwareShoppingItem.set_status(self.root, self.magnet.pk, "in_cart")
        self.assertEqual(HardwareShoppingItem.objects.count(), 0)

    def test_set_status_with_outdated_expected_quantity_writes_nothing(self) -> None:
        with self.assertRaises(ShoppingQuantityChanged):
            HardwareShoppingItem.set_status(self.root, self.m3.pk, "ordered", expected_quantity=8)
        self.assertEqual(HardwareShoppingItem.objects.count(), 0)
        row = HardwareShoppingItem.set_status(self.root, self.m3.pk, "ordered", expected_quantity=10)
        self.assertEqual(row.status, "ordered")

    def test_set_status_on_changed_row_takes_new_quantity(self) -> None:
        HardwareShoppingItem.set_status(self.root, self.m3.pk, "ordered")
        self.root_m3.quantity = 5
        self.root_m3.save()
        row = HardwareShoppingItem.set_status(self.root, self.m3.pk, "in_cart")
        self.assertEqual(row.quantity, 11)
        self.assertIsNone(row.previous_quantity)
        self.assertEqual(HardwareShoppingItem.objects.get(hardware_part=self.m3).quantity, 11)

    def test_orphan_rows_ignored_and_pruned_on_write(self) -> None:
        HardwareShoppingItem.set_status(self.root, self.m3.pk, "ordered")
        ProjectHardware.objects.filter(hardware_part=self.m3).delete()
        self.assertNotIn(self.m3.pk, [r.hardware_part.pk for r in HardwareShoppingItem.build_list(self.root)])
        HardwareShoppingItem.set_status(self.root, self.bearing.pk, "in_cart")
        self.assertFalse(HardwareShoppingItem.objects.filter(hardware_part=self.m3).exists())


class ChangedQuantityTests(ShoppingListTestBase):
    def test_changed_quantity_resets_only_that_row(self) -> None:
        HardwareShoppingItem.set_status(self.root, self.m3.pk, "ordered")
        HardwareShoppingItem.set_status(self.root, self.bearing.pk, "ordered")
        self.root_m3.quantity = 5
        self.root_m3.save()
        m3 = self.row(self.root, self.m3)
        self.assertEqual((m3.status, m3.previous_quantity, m3.quantity), ("open", 10, 11))
        self.assertEqual(self.row(self.root, self.bearing).status, "ordered")

    def test_changed_edge_quantity_resets_affected_rows(self) -> None:
        HardwareShoppingItem.set_status(self.root, self.m3.pk, "ordered")
        HardwareShoppingItem.set_status(self.root, self.bearing.pk, "ordered")
        self.edge.quantity = 3
        self.edge.save()
        m3, bearing = self.row(self.root, self.m3), self.row(self.root, self.bearing)
        self.assertEqual((m3.status, m3.previous_quantity), ("open", 10))
        self.assertEqual((bearing.status, bearing.previous_quantity), ("open", 2))


class BulkTests(ShoppingListTestBase):
    def test_mark_cart_ordered_only_effective_cart_rows(self) -> None:
        HardwareShoppingItem.set_status(self.root, self.m3.pk, "in_cart")
        HardwareShoppingItem.set_status(self.root, self.bearing.pk, "in_cart")
        ProjectHardware.objects.filter(hardware_part=self.bearing).update(quantity=5)
        self.assertEqual(HardwareShoppingItem.mark_cart_ordered(self.root), 1)
        self.assertEqual(self.row(self.root, self.m3).status, "ordered")
        stale = HardwareShoppingItem.objects.get(project=self.root, hardware_part=self.bearing)
        self.assertEqual(stale.status, "in_cart")

    def test_reset_only_affects_given_project(self) -> None:
        HardwareShoppingItem.set_status(self.root, self.m3.pk, "ordered")
        HardwareShoppingItem.set_status(self.child, self.m3.pk, "ordered")
        self.assertEqual(HardwareShoppingItem.reset(self.root), 1)
        self.assertEqual(HardwareShoppingItem.objects.filter(project=self.root).count(), 0)
        self.assertEqual(HardwareShoppingItem.objects.filter(project=self.child).count(), 1)

    def test_state_independent_per_project(self) -> None:
        HardwareShoppingItem.set_status(self.root, self.bearing.pk, "ordered")
        self.assertEqual(self.row(self.child, self.bearing).status, "open")

    def test_deleting_hardware_part_cascades_shopping_items(self) -> None:
        HardwareShoppingItem.set_status(self.root, self.m3.pk, "ordered")
        ProjectHardware.objects.filter(hardware_part=self.m3).delete()
        self.m3.delete()
        self.assertEqual(HardwareShoppingItem.objects.count(), 0)
