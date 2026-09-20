"""DAG aggregation: shared modules and diamond multi-path counting via edges."""

from django.test import TestCase

from core.models import Part, Project, ProjectComponent, ProjectPart


class DagPartAggregationTests(TestCase):
    """_collect_parts_with_multiplier must count each path in the DAG."""

    def _part(self, name: str) -> Part:
        # Part.project is still NOT NULL in Phase 2; give each a distinct home project
        # so the FK is satisfied. Aggregation reads edges, not this FK.
        home = Project.objects.create(name=f"home-{name}")
        return Part.objects.create(project=home, name=name, quantity=1)

    def test_shared_module_counts_under_each_parent(self):
        # cabin (with 1 part) shared by truck_a and truck_b via ProjectComponent edges.
        # The multiplier is the product of ProjectComponent edge quantities; a direct
        # ProjectPart contributes membership (x1), its Part.quantity is the leaf count.
        cabin = Project.objects.create(name="cabin")
        bolt = self._part("bolt")
        ProjectPart.objects.create(project=cabin, part=bolt, quantity=1)
        truck_a = Project.objects.create(name="truck-a")
        truck_b = Project.objects.create(name="truck-b")
        ProjectComponent.objects.create(parent_project=truck_a, child_project=cabin, quantity=10)
        ProjectComponent.objects.create(parent_project=truck_b, child_project=cabin, quantity=2)

        a = {(p.pk, m) for p, m in truck_a._collect_parts_with_multiplier()}
        self.assertIn((bolt.pk, 10), a)
        b = {(p.pk, m) for p, m in truck_b._collect_parts_with_multiplier()}
        self.assertIn((bolt.pk, 2), b)

    def test_diamond_counts_every_path(self):
        # A->B(1), A->C(1), B->D(2), C->D(3); D has part x(qty 1). needed(x) = 2+3 = 5
        a = Project.objects.create(name="A")
        b = Project.objects.create(name="B")
        c = Project.objects.create(name="C")
        d = Project.objects.create(name="D")
        x = self._part("x")
        ProjectPart.objects.create(project=d, part=x, quantity=1)
        ProjectComponent.objects.create(parent_project=a, child_project=b, quantity=1)
        ProjectComponent.objects.create(parent_project=a, child_project=c, quantity=1)
        ProjectComponent.objects.create(parent_project=b, child_project=d, quantity=2)
        ProjectComponent.objects.create(parent_project=c, child_project=d, quantity=3)

        total = sum(p.quantity * m for p, m in a._collect_parts_with_multiplier() if p.pk == x.pk)
        self.assertEqual(total, 5)

    def test_corrupt_cycle_terminates(self):
        # Force a cycle via edges (bypassing save() guard) and ensure no RecursionError.
        a = Project.objects.create(name="A")
        b = Project.objects.create(name="B")
        ProjectComponent.objects.create(parent_project=a, child_project=b, quantity=1)
        ProjectComponent.objects.bulk_create([ProjectComponent(parent_project=b, child_project=a, quantity=1)])
        self.assertIsInstance(a._collect_parts_with_multiplier(), list)  # must return, not hang


class EdgeQuantityAuthoritativeTests(TestCase):
    """Phase 6a: the composition-edge quantity is the authoritative leaf count."""

    def test_same_part_different_edge_quantities_sum_independently(self):
        truck = Project.objects.create(name="Truck")
        cabin = Project.objects.create(name="Cabin")
        frame = Project.objects.create(name="Frame")
        home = Project.objects.create(name="home")
        # Part.quantity is now IGNORED by aggregation — the edge quantity rules.
        bolt = Part.objects.create(project=home, name="bolt", quantity=1)
        ProjectComponent.objects.create(parent_project=truck, child_project=cabin, quantity=1)
        ProjectComponent.objects.create(parent_project=truck, child_project=frame, quantity=1)
        ProjectPart.objects.create(project=cabin, part=bolt, quantity=4)  # 4 in cabin
        ProjectPart.objects.create(project=frame, part=bolt, quantity=10)  # 10 in frame
        # total bolts in truck = 4 + 10 = 14 (edge-authoritative)
        self.assertEqual(truck.total_parts_count, 14)

    def test_edge_quantity_beats_part_quantity(self):
        # Part.quantity=99 must not leak into the count; only the edge (3) counts.
        home = Project.objects.create(name="home")
        module = Project.objects.create(name="module")
        widget = Part.objects.create(project=home, name="widget", quantity=99)
        ProjectPart.objects.create(project=module, part=widget, quantity=3)
        self.assertEqual(module.total_parts_count, 3)


class DagHardwareDocumentTests(TestCase):
    def test_hardware_shared_module_via_edges(self):
        from core.models import HardwarePart, ProjectHardware

        sub = Project.objects.create(name="sub")
        hp = HardwarePart.objects.create(name="Bolt", category="bolts", unit_price="0.50")
        ProjectHardware.objects.create(project=sub, hardware_part=hp, quantity=4)
        root = Project.objects.create(name="root")
        ProjectComponent.objects.create(parent_project=root, child_project=sub, quantity=3)

        hw = root._collect_hardware_with_multiplier()
        self.assertEqual(len(hw), 1)
        obj, mult = hw[0]
        self.assertEqual(obj.quantity, 4)
        self.assertEqual(mult, 3)

    def test_documents_collected_via_edges(self):
        from django.core.files.uploadedfile import SimpleUploadedFile

        from core.models import ProjectDocument

        sub = Project.objects.create(name="sub")
        ProjectDocument.objects.create(project=sub, name="Sub Doc", file=SimpleUploadedFile("s.pdf", b"x"))
        root = Project.objects.create(name="root")
        ProjectComponent.objects.create(parent_project=root, child_project=sub, quantity=1)

        docs = root._collect_documents()
        self.assertIn("Sub Doc", {d.name for d, _ in docs})


class DagDescendantTests(TestCase):
    def test_descendants_via_edges_including_shared(self):
        root = Project.objects.create(name="root")
        m1 = Project.objects.create(name="m1")
        shared = Project.objects.create(name="shared")
        ProjectComponent.objects.create(parent_project=root, child_project=m1, quantity=1)
        ProjectComponent.objects.create(parent_project=root, child_project=shared, quantity=1)
        ProjectComponent.objects.create(parent_project=m1, child_project=shared, quantity=1)
        self.assertEqual(root.get_descendant_ids(), {m1.pk, shared.pk})
