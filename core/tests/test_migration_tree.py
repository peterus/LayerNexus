"""Tests for the tree-rollback data migration (``0030_backfill_tree``).

The migration backfills ``Part.project`` / ``Part.quantity`` and ``Project.parent`` /
``Project.quantity`` from the composition edges and aborts on shared data. The tests run
the real migration with :class:`~django.db.migrations.executor.MigrationExecutor` against
historical models.
"""

from __future__ import annotations

import contextlib
import io

from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase

BEFORE = [("core", "0029_part_project_quantity")]
BACKFILL = [("core", "0030_backfill_tree")]


class TreeMigrationTests(TransactionTestCase):
    """Run ``0030_backfill_tree`` on historical data built at ``0029``."""

    serialized_rollback = True

    def setUp(self) -> None:
        """Migrate back to ``0029`` and expose the historical models."""
        super().setUp()
        executor = MigrationExecutor(connection)
        executor.migrate(BEFORE)
        apps = executor.loader.project_state(BEFORE).apps
        self.Project = apps.get_model("core", "Project")
        self.Part = apps.get_model("core", "Part")
        self.ProjectPart = apps.get_model("core", "ProjectPart")
        self.ProjectComponent = apps.get_model("core", "ProjectComponent")
        self.PrintJob = apps.get_model("core", "PrintJob")
        self.PrintJobPart = apps.get_model("core", "PrintJobPart")
        self.OrcaPrintPreset = apps.get_model("core", "OrcaPrintPreset")

    def tearDown(self) -> None:
        """Migrate forward to the leaf nodes so later tests see the current schema.

        The test data is removed first so an abort scenario cannot fail the forward run.
        """
        executor = MigrationExecutor(connection)
        if ("core", "0030_backfill_tree") not in executor.loader.applied_migrations:
            self.PrintJob.objects.all().delete()
            self.Part.objects.all().delete()
            self.Project.objects.update(parent=None)
            self.Project.objects.all().delete()
        executor.migrate(executor.loader.graph.leaf_nodes())
        super().tearDown()

    def _migrate(self, target: list[tuple[str, str]] = BACKFILL) -> None:
        """Run the migrations up to ``target`` on a fresh executor."""
        executor = MigrationExecutor(connection)
        executor.loader.build_graph()
        executor.migrate(target)

    def _preset(self):
        """Create a historical print preset."""
        return self.OrcaPrintPreset.objects.create(name="Preset A", orca_name="Preset A")

    def _clean_tree(self) -> tuple:
        """Build root R (preset) → S (edge qty 2, stale legacy parent X) → part P (qty 3)."""
        preset = self._preset()
        root = self.Project.objects.create(name="R", default_print_preset=preset)
        unrelated = self.Project.objects.create(name="X", default_print_preset=preset)
        sub = self.Project.objects.create(name="S", parent=unrelated, quantity=7)
        self.ProjectComponent.objects.create(parent_project=root, child_project=sub, quantity=2)
        part = self.Part.objects.create(name="P")
        self.ProjectPart.objects.create(project=sub, part=part, quantity=3)
        return root, unrelated, sub, part

    def test_backfill_clean_data(self) -> None:
        """Edges become the tree fields; a stale legacy parent without an edge is cleared."""
        root, unrelated, sub, part = self._clean_tree()
        stale = self.Project.objects.create(name="Stale", parent=root)

        self._migrate()

        part.refresh_from_db()
        sub.refresh_from_db()
        root.refresh_from_db()
        stale.refresh_from_db()
        self.assertEqual(part.project_id, sub.pk)
        self.assertEqual(part.quantity, 3)
        self.assertEqual(sub.parent_id, root.pk)
        self.assertEqual(sub.quantity, 2)
        self.assertIsNone(root.parent_id)
        self.assertIsNone(stale.parent_id)

    def test_abort_on_part_in_two_projects(self) -> None:
        """A part linked into two projects aborts the migration with its pk."""
        root, unrelated, sub, part = self._clean_tree()
        self.ProjectPart.objects.create(project=root, part=part, quantity=1)

        with self.assertRaises(RuntimeError) as ctx:
            self._migrate()
        self.assertIn("Tree migration aborted", str(ctx.exception))
        self.assertIn(f"Part {part.pk}", str(ctx.exception))

    def test_abort_on_project_with_two_parents(self) -> None:
        """A project with two parent edges aborts the migration with its pk."""
        root, unrelated, sub, part = self._clean_tree()
        self.ProjectComponent.objects.create(parent_project=unrelated, child_project=sub, quantity=1)

        with self.assertRaises(RuntimeError) as ctx:
            self._migrate()
        self.assertIn("Tree migration aborted", str(ctx.exception))
        self.assertIn(f"Project {sub.pk}", str(ctx.exception))

    def test_abort_on_part_without_edge(self) -> None:
        """A part that belongs to no project aborts the migration with its pk."""
        self._clean_tree()
        orphan = self.Part.objects.create(name="Orphan")

        with self.assertRaises(RuntimeError) as ctx:
            self._migrate()
        self.assertIn("Tree migration aborted", str(ctx.exception))
        self.assertIn(f"Part {orphan.pk}", str(ctx.exception))

    def test_reports_root_without_preset(self) -> None:
        """A root without a default preset is reported, not fatal."""
        self._clean_tree()
        bare = self.Project.objects.create(name="Bare root")

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self._migrate()
        self.assertIn("has no default_print_preset", out.getvalue())
        self.assertIn(f"root project {bare.pk}", out.getvalue())

    def test_print_history_untouched(self) -> None:
        """Existing print job entries keep their rows and part ids."""
        root, unrelated, sub, part = self._clean_tree()
        job = self.PrintJob.objects.create(name="Job")
        entry = self.PrintJobPart.objects.create(print_job=job, part=part, quantity=2)

        self._migrate()

        self.assertEqual(self.PrintJobPart.objects.count(), 1)
        entry.refresh_from_db()
        self.assertEqual(entry.part_id, part.pk)

    def test_full_forward_to_leaf_keeps_history(self) -> None:
        """Clean data at 0029 migrates to the leaf: tree fields set, history kept, edges gone."""
        root, unrelated, sub, part = self._clean_tree()
        job = self.PrintJob.objects.create(name="Job")
        self.PrintJobPart.objects.create(print_job=job, part=part, quantity=2)

        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())

        from core.models import Part, PrintJobPart, Project

        self.assertEqual(Part.objects.get(pk=part.pk).project_id, sub.pk)
        self.assertEqual(Project.objects.get(pk=sub.pk).parent_id, root.pk)
        self.assertEqual(PrintJobPart.objects.filter(part_id=part.pk).count(), 1)
        tables = connection.introspection.table_names()
        self.assertNotIn("core_projectpart", tables)
        self.assertNotIn("core_projectcomponent", tables)
