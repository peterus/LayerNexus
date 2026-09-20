# Project Variants — Phase 1 (Expand) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a reusable-building-block composition layer (`ProjectComponent`,
`ProjectPart` through-models) *additively* alongside the existing `Part.project` and
`Project.parent` FKs, backfilled and kept in sync via dual-write — without changing any
consumer, so the whole existing test suite stays green untouched.

**Architecture:** Expand step of an expand–migrate–contract refactor. Two new through-models
express many-to-many composition with a per-edge `quantity`. The old FKs stay authoritative
in this phase; a data migration backfills one edge per legacy FK relation, and `save()`
dual-write on `Part`/`Project` keeps edges consistent with the FKs so later phases can read
edges without staleness.

**Tech Stack:** Django 6.0, Python 3.13 (local `.venv`), SQLite, Django test runner (no
pytest), ruff 0.15.20.

**Spec:** `docs/superpowers/specs/2026-09-20-project-variants-dag-composition-design.md`

## Global Constraints

- Python ≥ 3.12 (Django 6.0 floor); local `.venv/bin/python` is 3.13, CI is 3.14.
- **Strings: double quotes only.** f-strings for interpolation (never `+` or `%`).
- **Type hints** on every function/method signature and return type.
- **Google-style docstrings** (English) on public functions, classes, modules.
- **Never hand-edit schema migrations.** Schema migrations come from `makemigrations`.
  Data migrations are scaffolded with `makemigrations --empty core` and only their
  `RunPython` body is authored — this keeps `makemigrations --check` green (it verifies
  model↔migration schema sync, which a data migration does not affect).
- Models: explicit `related_name`, `__str__()`, `Meta.ordering`, core validators.
- Coverage gate: `fail_under = 55`. CI gates: `ruff check` + `ruff format --check` →
  `makemigrations --check --dry-run` → `manage.py check --fail-level WARNING` →
  `manage.py test core`.
- Test commands (run from repo root):
  - Single module: `.venv/bin/python manage.py test core.tests.test_composition`
  - Full suite: `.venv/bin/python manage.py test core`
  - Migration gate: `.venv/bin/python manage.py makemigrations --check --dry-run`
  - Lint: `ruff check . && ruff format --check .`
- New models re-exported from `core/models/__init__.py` and added to `__all__`.

---

## File Structure

- **Create** `core/models/composition.py` — `ProjectPart`, `ProjectComponent`, the shared
  cycle-guard helper, and the `rebuild_composition_edges(...)` backfill helper. One clear
  responsibility: the composition edge layer.
- **Modify** `core/models/__init__.py` — import + `__all__` the two new models.
- **Modify** `core/models/parts.py` — add `save()` dual-write of the `ProjectPart` edge.
- **Modify** `core/models/projects.py` — extend `save()` to dual-write the
  `ProjectComponent` edge.
- **Create** `core/migrations/0018_projectpart_projectcomponent.py` — generated
  create-tables migration (do not hand-author).
- **Create** `core/migrations/0019_backfill_composition_edges.py` — `--empty`-scaffolded
  data migration whose `RunPython` calls `rebuild_composition_edges`.
- **Create** `core/tests/test_composition.py` — all Phase 1 tests.

Phase 1 touches **no** view, form, template, admin, or service file. That is what keeps the
existing suite green without edits.

---

### Task 1: Composition through-models with constraints

**Files:**
- Create: `core/models/composition.py`
- Modify: `core/models/__init__.py`
- Create (generated): `core/migrations/0018_projectpart_projectcomponent.py`
- Test: `core/tests/test_composition.py`

**Interfaces:**
- Consumes: existing `core.Project`, `core.Part`.
- Produces:
  - `ProjectPart(project: Project, part: Part, quantity: int = 1, position: int = 0)`
    with reverse relations `Project.part_links` and `Part.project_links`.
  - `ProjectComponent(parent_project: Project, child_project: Project, quantity: int = 1,
    position: int = 0)` with reverse relations `Project.child_links`
    (edges where it is the parent) and `Project.parent_links` (edges where it is the child).
  - Unique constraints `uniq_projectpart_project_part`,
    `uniq_projectcomponent_parent_child`; check constraints `projectpart_quantity_gte_1`,
    `projectcomponent_quantity_gte_1`, `projectcomponent_no_self_parent`.

- [ ] **Step 1: Write the failing test**

Add to `core/tests/test_composition.py`:

```python
"""Tests for the composition edge layer (ProjectPart, ProjectComponent)."""

from django.db import IntegrityError, transaction
from django.test import TestCase

from core.models import Part, Project, ProjectComponent, ProjectPart


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
        truck = Project.objects.create(name="Truck A")
        with self.assertRaises(IntegrityError), transaction.atomic():
            ProjectComponent.objects.create(parent_project=truck, child_project=truck)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python manage.py test core.tests.test_composition -v 2`
Expected: FAIL — `ImportError: cannot import name 'ProjectPart' from 'core.models'`.

- [ ] **Step 3: Write minimal implementation**

Create `core/models/composition.py`:

```python
"""Composition edge models: reusable parts and modules in a DAG.

``ProjectPart`` links a reusable :class:`~core.models.parts.Part` into a project/module
with a per-edge quantity; ``ProjectComponent`` links a child project/module into a parent
assembly. Together they express the many-to-many composition graph that lets a building
block be shared across multiple assemblies (e.g. two truck variants).
"""

from __future__ import annotations

from django.core.validators import MinValueValidator
from django.db import models
from django.db.models import CheckConstraint, F, Q, UniqueConstraint


class ProjectPart(models.Model):
    """A reusable part included in a project/module with a quantity."""

    project = models.ForeignKey(
        "core.Project",
        on_delete=models.CASCADE,
        related_name="part_links",
        help_text="The module/assembly this part is included in.",
    )
    part = models.ForeignKey(
        "core.Part",
        on_delete=models.CASCADE,
        related_name="project_links",
        help_text="The reusable part referenced by the module/assembly.",
    )
    quantity = models.PositiveIntegerField(
        default=1,
        validators=[MinValueValidator(1)],
        help_text="How many of this part the module needs.",
    )
    position = models.PositiveIntegerField(
        default=0,
        help_text="Ordering of this part within the module.",
    )

    class Meta:
        ordering = ["position", "pk"]
        constraints = [
            UniqueConstraint(fields=["project", "part"], name="uniq_projectpart_project_part"),
            CheckConstraint(condition=Q(quantity__gte=1), name="projectpart_quantity_gte_1"),
        ]

    def __str__(self) -> str:
        return f"{self.quantity}× {self.part_id} in {self.project_id}"


class ProjectComponent(models.Model):
    """A child project/module included in a parent assembly with a quantity."""

    parent_project = models.ForeignKey(
        "core.Project",
        on_delete=models.CASCADE,
        related_name="child_links",
        help_text="The assembly that contains the child module.",
    )
    child_project = models.ForeignKey(
        "core.Project",
        on_delete=models.CASCADE,
        related_name="parent_links",
        help_text="The module included in the parent assembly.",
    )
    quantity = models.PositiveIntegerField(
        default=1,
        validators=[MinValueValidator(1)],
        help_text="How many of this child module the assembly needs.",
    )
    position = models.PositiveIntegerField(
        default=0,
        help_text="Ordering of this child within the assembly.",
    )

    class Meta:
        ordering = ["position", "pk"]
        constraints = [
            UniqueConstraint(
                fields=["parent_project", "child_project"],
                name="uniq_projectcomponent_parent_child",
            ),
            CheckConstraint(condition=Q(quantity__gte=1), name="projectcomponent_quantity_gte_1"),
            CheckConstraint(
                condition=~Q(parent_project=F("child_project")),
                name="projectcomponent_no_self_parent",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.quantity}× {self.child_project_id} in {self.parent_project_id}"
```

Then edit `core/models/__init__.py` — add the import (keep alphabetical grouping) and
`__all__` entries:

```python
from core.models.composition import ProjectComponent, ProjectPart
```

Add `"ProjectComponent",` and `"ProjectPart",` to `__all__`.

- [ ] **Step 4: Generate the migration**

Run: `.venv/bin/python manage.py makemigrations core`
Expected: creates `core/migrations/0018_projectpart_projectcomponent.py` (CreateModel for
both). Do not edit it. If the filename numbering differs, keep whatever `makemigrations`
produced and use that name in Task 3's dependency.

- [ ] **Step 5: Run test + migration gate to verify pass**

Run: `.venv/bin/python manage.py test core.tests.test_composition -v 2`
Expected: PASS.
Run: `.venv/bin/python manage.py makemigrations --check --dry-run`
Expected: exit 0 (no missing migrations).

- [ ] **Step 6: Commit**

```bash
git add core/models/composition.py core/models/__init__.py \
        core/migrations/0018_projectpart_projectcomponent.py core/tests/test_composition.py
git commit -m "feat: add ProjectPart/ProjectComponent composition through-models"
```

---

### Task 2: DAG cycle guard on ProjectComponent

**Files:**
- Modify: `core/models/composition.py`
- Test: `core/tests/test_composition.py`

**Interfaces:**
- Consumes: `ProjectComponent` from Task 1.
- Produces:
  - `ProjectComponent.clean() -> None` and `ProjectComponent.save(*args, **kwargs) -> None`
    raise `django.core.exceptions.ValidationError` when the edge would make an assembly
    (transitively) contain itself.
  - Module-level `component_would_create_cycle(parent_id: int, child_id: int) -> bool`
    (visited-set guarded DAG walk; safe against already-persisted corrupt cycles).

- [ ] **Step 1: Write the failing test**

Add to `core/tests/test_composition.py`:

```python
from django.core.exceptions import ValidationError

from core.models.composition import component_would_create_cycle


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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python manage.py test core.tests.test_composition.ProjectComponentCycleTests -v 2`
Expected: FAIL — `ImportError: cannot import name 'component_would_create_cycle'`.

- [ ] **Step 3: Write minimal implementation**

In `core/models/composition.py`, add the import and helper, and add `clean()`/`save()` to
`ProjectComponent`:

```python
from django.core.exceptions import ValidationError
```

Module-level helper (place above the classes or below — after imports):

```python
def component_would_create_cycle(parent_id: int, child_id: int) -> bool:
    """Return True if adding a ``parent_id -> child_id`` edge would create a cycle.

    A cycle forms when the prospective parent is reachable *from* the child by
    following existing ``ProjectComponent`` edges downward (parent → child), or when
    parent and child are the same node. A visited-set guard makes the descent terminate
    even if a corrupt cycle already exists in the database.

    Args:
        parent_id: PK of the prospective parent project.
        child_id: PK of the prospective child project.

    Returns:
        True if the edge would introduce a cycle, else False.
    """
    if parent_id == child_id:
        return True
    visited: set[int] = set()
    stack: list[int] = [child_id]
    while stack:
        current = stack.pop()
        if current == parent_id:
            return True
        if current in visited:
            continue
        visited.add(current)
        stack.extend(
            ProjectComponent.objects.filter(parent_project_id=current).values_list(
                "child_project_id", flat=True
            )
        )
    return False
```

Add to `ProjectComponent`:

```python
    def clean(self) -> None:
        """Reject edges that would make an assembly (transitively) contain itself."""
        super().clean()
        if self.parent_project_id and self.child_project_id and component_would_create_cycle(
            self.parent_project_id, self.child_project_id
        ):
            raise ValidationError(
                {"child_project": "This would make an assembly contain itself (cycle)."}
            )

    def save(self, *args, **kwargs) -> None:
        """Persist the edge, refusing to store one that closes a cycle."""
        if self.parent_project_id and self.child_project_id and component_would_create_cycle(
            self.parent_project_id, self.child_project_id
        ):
            raise ValidationError(
                {"child_project": "This would make an assembly contain itself (cycle)."}
            )
        super().save(*args, **kwargs)
```

Note: `test_cycle_helper_terminates_on_corrupt_graph` uses `bulk_create` deliberately to
bypass `save()` and plant a corrupt cycle; the helper's visited-set is what keeps it
terminating.

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python manage.py test core.tests.test_composition -v 2`
Expected: PASS (all classes).

- [ ] **Step 5: Commit**

```bash
git add core/models/composition.py core/tests/test_composition.py
git commit -m "feat: reject cyclic composition edges (DAG guard)"
```

---

### Task 3: Backfill helper + data migration

**Files:**
- Modify: `core/models/composition.py`
- Create (scaffolded via `--empty`): `core/migrations/0019_backfill_composition_edges.py`
- Test: `core/tests/test_composition.py`

**Interfaces:**
- Consumes: `Project`, `Part`, `ProjectPart`, `ProjectComponent`.
- Produces:
  - `rebuild_composition_edges(project_model, part_model, component_model, part_link_model) -> None`
    — idempotently (re)creates one `ProjectPart` edge per `Part.project` relation and one
    `ProjectComponent` edge per `Project.parent` relation, copying the legacy `quantity`.
    Accepts model classes so both the migration (`apps.get_model(...)`) and unit tests
    (real models) can call it.

- [ ] **Step 1: Write the failing test**

Add to `core/tests/test_composition.py`:

```python
from core.models.composition import rebuild_composition_edges


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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python manage.py test core.tests.test_composition.RebuildCompositionEdgesTests -v 2`
Expected: FAIL — `ImportError: cannot import name 'rebuild_composition_edges'`.

- [ ] **Step 3: Write the helper**

Add to `core/models/composition.py`:

```python
def rebuild_composition_edges(project_model, part_model, component_model, part_link_model) -> None:
    """Mirror the legacy FK graph into composition edges, idempotently.

    Creates one ``ProjectPart`` per ``Part.project`` relation and one
    ``ProjectComponent`` per ``Project.parent`` relation, copying the legacy per-node
    ``quantity`` onto the edge. Uses ``update_or_create`` so repeated runs (backfill +
    later reconciliation) neither duplicate nor error. Written to accept model classes so
    the data migration can pass historical ``apps.get_model(...)`` classes while unit
    tests pass the real models.

    Args:
        project_model: The ``Project`` model class.
        part_model: The ``Part`` model class.
        component_model: The ``ProjectComponent`` model class.
        part_link_model: The ``ProjectPart`` model class.
    """
    for part in part_model.objects.filter(project__isnull=False).iterator():
        part_link_model.objects.update_or_create(
            project_id=part.project_id,
            part_id=part.pk,
            defaults={"quantity": part.quantity},
        )
    for child in project_model.objects.filter(parent__isnull=False).iterator():
        component_model.objects.update_or_create(
            parent_project_id=child.parent_id,
            child_project_id=child.pk,
            defaults={"quantity": child.quantity},
        )
```

Note: the migration calls this via `apps.get_model`, so `component_model.save()` (with its
cycle guard) is **not** invoked here — `update_or_create` on the historical model uses the
plain manager. That is intentional: backfill mirrors an already-acyclic tree, so no guard
is needed, and historical models never carry custom `save()` anyway.

- [ ] **Step 4: Scaffold + author the data migration**

Run: `.venv/bin/python manage.py makemigrations core --empty -n backfill_composition_edges`
Expected: creates `core/migrations/0019_backfill_composition_edges.py`.

Author its body (this is a *data* migration — allowed to edit the `RunPython` body only):

```python
"""Backfill composition edges from the legacy Part.project / Project.parent FKs."""

from django.db import migrations

from core.models.composition import rebuild_composition_edges


def forwards(apps, schema_editor):
    """Create edges mirroring the current FK graph."""
    rebuild_composition_edges(
        apps.get_model("core", "Project"),
        apps.get_model("core", "Part"),
        apps.get_model("core", "ProjectComponent"),
        apps.get_model("core", "ProjectPart"),
    )


def backwards(apps, schema_editor):
    """Remove all composition edges (the FKs remain the source of truth)."""
    apps.get_model("core", "ProjectPart").objects.all().delete()
    apps.get_model("core", "ProjectComponent").objects.all().delete()


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0018_projectpart_projectcomponent"),
    ]
    operations = [
        migrations.RunPython(forwards, backwards),
    ]
```

If Task 1's migration got a different name/number, update the `dependencies` entry to match.

- [ ] **Step 5: Run tests + gates to verify pass**

Run: `.venv/bin/python manage.py test core.tests.test_composition -v 2`
Expected: PASS.
Run: `.venv/bin/python manage.py makemigrations --check --dry-run`
Expected: exit 0.
Run: `.venv/bin/python manage.py migrate` (applies cleanly on a fresh DB).
Expected: no errors.

- [ ] **Step 6: Commit**

```bash
git add core/models/composition.py core/migrations/0019_backfill_composition_edges.py \
        core/tests/test_composition.py
git commit -m "feat: backfill composition edges from legacy FKs (data migration)"
```

---

### Task 4: Dual-write edges from Part/Project save()

**Files:**
- Modify: `core/models/parts.py`
- Modify: `core/models/projects.py`
- Test: `core/tests/test_composition.py`

**Interfaces:**
- Consumes: `ProjectPart`, `ProjectComponent`, `component_would_create_cycle` (unused here
  but same module).
- Produces:
  - `Part.save()` upserts the single `ProjectPart` edge mirroring `self.project`
    (`quantity=self.quantity`) and removes stale edges for this part pointing at other
    projects. A part with `project=None` ends up with no edge.
  - `Project.save()` (in addition to its existing acyclic check) upserts the single
    `ProjectComponent` edge mirroring `self.parent` (`quantity=self.quantity`) and removes
    stale edges where this project is the child of a different parent. A top-level project
    (`parent=None`) ends up with no parent edge.

- [ ] **Step 1: Write the failing test**

Add to `core/tests/test_composition.py`:

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python manage.py test core.tests.test_composition.DualWriteTests -v 2`
Expected: FAIL — `ProjectPart.DoesNotExist` (no edge is written yet).

- [ ] **Step 3: Implement dual-write in Part.save()**

In `core/models/parts.py`, add a `save()` override to `Part` (it currently has none). Place
it after `__str__`:

```python
    def save(self, *args, **kwargs) -> None:
        """Persist the part and keep its ``ProjectPart`` edge in sync (transition shim).

        During the expand phase the legacy ``project`` FK stays authoritative; this mirror
        keeps exactly one ``ProjectPart`` edge consistent with it so later phases can read
        edges without staleness. Removed in the contract phase.
        """
        super().save(*args, **kwargs)
        from core.models.composition import ProjectPart

        if self.project_id is None:
            ProjectPart.objects.filter(part=self).delete()
            return
        ProjectPart.objects.filter(part=self).exclude(project_id=self.project_id).delete()
        ProjectPart.objects.update_or_create(
            project_id=self.project_id,
            part=self,
            defaults={"quantity": self.quantity},
        )
```

- [ ] **Step 4: Implement dual-write in Project.save()**

In `core/models/projects.py`, extend the existing `save()`. It currently reads:

```python
    def save(self, *args, **kwargs) -> None:
        self._assert_parent_acyclic()
        super().save(*args, **kwargs)
```

Replace the body's tail so it also mirrors the component edge (keep the existing docstring,
append a sentence about the mirror):

```python
    def save(self, *args, **kwargs) -> None:
        self._assert_parent_acyclic()
        super().save(*args, **kwargs)
        from core.models.composition import ProjectComponent

        if self.parent_id is None:
            ProjectComponent.objects.filter(child_project=self).delete()
            return
        ProjectComponent.objects.filter(child_project=self).exclude(
            parent_project_id=self.parent_id
        ).delete()
        ProjectComponent.objects.update_or_create(
            parent_project_id=self.parent_id,
            child_project=self,
            defaults={"quantity": self.quantity},
        )
```

Note: imports are function-local to avoid a circular import (`composition` has no import of
`parts`/`projects`, but `projects` imports `parts` at module load — the local import keeps
the dependency direction clean and matches the codebase's existing lazy-import style, e.g.
`from collections import defaultdict` inside methods).

- [ ] **Step 5: Run tests to verify pass**

Run: `.venv/bin/python manage.py test core.tests.test_composition -v 2`
Expected: PASS (all classes).

- [ ] **Step 6: Run the FULL suite to prove no regression**

Run: `.venv/bin/python manage.py test core`
Expected: PASS — existing tests are untouched and still green (dual-write only *adds* edge
rows; no existing assertion inspects edge counts).

- [ ] **Step 7: Lint + format + migration gate**

Run: `ruff check . && ruff format --check .`
Expected: clean.
Run: `.venv/bin/python manage.py makemigrations --check --dry-run`
Expected: exit 0.

- [ ] **Step 8: Commit**

```bash
git add core/models/parts.py core/models/projects.py core/tests/test_composition.py
git commit -m "feat: dual-write composition edges from Part/Project save()"
```

---

## Self-Review

**Spec coverage (Phase 1 / Expand scope only):**
- "add the `ProjectComponent`/`ProjectPart` tables additively" → Task 1. ✅
- per-edge `quantity` (≥1), uniqueness, ordering (`position`) → Task 1. ✅
- DAG cycle detection enforced on edges → Task 2. ✅
- "data migration backfills one edge per existing FK relation (`quantity` copied)" →
  Task 3. ✅
- "Dual-write keeps the edges in sync with the old FKs" → Task 4. ✅
- "old fields authoritative … no consumer changes yet … suite stays green" → Task 4 Step 6
  runs the full suite; no view/form/template/admin/service file is in any task's file list.
  ✅
- Deferred to later phases (correctly out of scope here): aggregation rewrite (Phase 2),
  navigation/editing/parts-library (Phase 3), `PrintJobPart.target_assembly` &
  per-context progress (Phase 4), "duplicate as variant" (Phase 5), field removal
  (Phase 6). ✅

**Placeholder scan:** No "TBD"/"handle edge cases"/"similar to"/"add validation"
placeholders; every code and test step carries concrete content. ✅

**Type consistency:** Reverse relation names are used consistently —
`part_links`/`project_links` (ProjectPart) and `child_links`/`parent_links`
(ProjectComponent) appear identically in Tasks 1, 2, 4. Helper names
`component_would_create_cycle` (Task 2) and `rebuild_composition_edges` with the argument
order `(project_model, part_model, component_model, part_link_model)` are used identically
in Task 3's migration and tests. Constraint names are unique across both models. ✅

**Migration-gate note:** Task 3 authors only the `RunPython` body of an `--empty`
scaffold, which does not change schema state, so `makemigrations --check --dry-run` stays
green (verified explicitly in Task 3 Step 5 and Task 4 Step 7).
