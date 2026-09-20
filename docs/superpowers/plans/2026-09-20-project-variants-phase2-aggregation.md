# Project Variants — Phase 2 (Migrate: aggregation onto edges) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Switch the recursive aggregation (`_collect_parts_with_multiplier`,
`_collect_hardware_with_multiplier`, `_collect_documents`, `get_descendant_ids`,
`aggregate_prefetch_lookups`) from the legacy FK relations (`self.parts`,
`self.subprojects`) onto the Phase-1 composition edges (`part_links`, `child_links`), so
aggregation traverses the DAG — including shared building blocks reached via multiple paths.

**Architecture:** Migrate step of expand–migrate–contract. Phase 1 (merged, `main@85ea7e3`)
added `ProjectPart`/`ProjectComponent` edges kept in sync with the old FKs via dual-write.
This phase changes only the **read side** to consume edges. Because dual-write keeps edges
equivalent to the FK tree, every existing tree-shaped aggregation test stays green; new
tests add DAG-only behavior (a module shared by two assemblies, diamond multi-path counts).
The old FKs remain authoritative and untouched (removed only in Phase 6).

**Tech Stack:** Django 6.0, Python 3.13 (local `.venv`), SQLite, Django test runner, ruff 0.15.20.

**Spec:** `docs/superpowers/specs/2026-09-20-project-variants-dag-composition-design.md`

## Global Constraints

- Base branch: `main` at/after `85ea7e3` (Phase 1 merged). Branch this work from current main.
- Double quotes only; f-strings; type hints on all signatures; Google-style docstrings.
- **No migration** in this phase (no model/schema change — read-path only). `makemigrations
  --check --dry-run` must stay clean.
- **File ownership:** touch ONLY `core/models/projects.py`, `core/models/parts.py` (only if a
  read-path helper needs it — avoid if possible), `core/tests/test_aggregation.py`, and a new
  `core/tests/test_aggregation_dag.py`. Do NOT touch views/forms/templates/admin/services,
  `core/models/composition.py`, or migrations. (Disjoint from in-flight PR #33 which owns
  Dockerfile/compose/apps.py.)
- Test: `.venv/bin/python manage.py test core` (coverage ≥ 55). Lint: `ruff check . &&
  ruff format --check .`. Gate: `makemigrations --check --dry-run`, `manage.py check
  --fail-level WARNING`. Fresh worktree: create `.venv` (`python3 -m venv .venv`,
  `pip install -r requirements.txt coverage`); tests need `DEBUG=1` + `DJANGO_SECRET_KEY` +
  `collectstatic` first.

## Merged Phase-1 interfaces this phase consumes

- `ProjectPart`: `project` FK (reverse `Project.part_links`), `part` FK, `quantity`,
  `position`; `Meta.ordering = ["position", "pk"]`.
- `ProjectComponent`: `parent_project` FK (reverse `Project.child_links`), `child_project`
  FK, `quantity`, `position`; same ordering.
- So for a project `p`: its parts-with-qty are `p.part_links.all()` (each `.part`,
  `.quantity`); its child modules-with-qty are `p.child_links.all()` (each `.child_project`,
  `.quantity`).

## Core design: DAG traversal (read this before Task 1)

Today's collectors use a **global** `_visited` set that skips any node seen once — correct
for a tree, **wrong for a DAG**: a module legitimately reached via two edges (a "diamond")
must contribute **once per path**, scaled by each path's edge-quantity product.

Replace global-skip with **path-local cycle guard + per-node memoized *relative* expansion**:

- `_expand_parts_relative(_path, _memo)` returns `list[(Part, mult_relative_to_self)]` where
  the node itself counts as ×1. It is a pure function of the node in an acyclic graph, so it
  is memoized in `_memo` (keyed by `pk`) and reused across paths — a diamond computes the
  shared subtree once and scales it per incoming edge.
- `_path` is the set of nodes on the **current** recursion stack; if a node recurs on its own
  path it is a corrupt cycle → return `[]` for that branch (terminates; degrades gracefully,
  matching Phase 1's documented corrupt-cycle stance).
- The public `_collect_parts_with_multiplier(multiplier=1)` calls the relative expansion with
  fresh `_path`/`_memo` and scales: `[(p, multiplier * m) for p, m in rel]`.

Diamond example: `A→B (q=1)`, `A→C (q=1)`, `B→D (q=2)`, `C→D (q=3)`, `D` has part `x` (qty 1
via `part_links`). Then `needed(x)` under A = `1*2*1 + 1*3*1 = 5`. Global-skip would wrongly
yield `2` (or `3`). The new traversal yields `5`.

---

### Task 1: DAG-correct part aggregation on edges

**Files:**
- Modify: `core/models/projects.py`
- Test: `core/tests/test_aggregation_dag.py` (new)

**Interfaces:**
- Produces: `Project._collect_parts_with_multiplier(multiplier: int = 1) -> list[tuple[Part, int]]`
  now traversing `part_links`/`child_links`; a private
  `Project._expand_parts_relative(_path: set[int], _memo: dict[int, list[tuple[Part, int]]]) ->
  list[tuple[Part, int]]`.

- [ ] **Step 1: Write the failing DAG tests**

Create `core/tests/test_aggregation_dag.py`:

```python
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
        # cabin (with 1 part) shared by truck_a and truck_b via edges
        cabin = Project.objects.create(name="cabin")
        bolt = self._part("bolt")
        ProjectPart.objects.create(project=cabin, part=bolt, quantity=10)
        truck_a = Project.objects.create(name="truck-a")
        truck_b = Project.objects.create(name="truck-b")
        ProjectComponent.objects.create(parent_project=truck_a, child_project=cabin, quantity=1)
        ProjectComponent.objects.create(parent_project=truck_b, child_project=cabin, quantity=1)

        a = {(p.pk, m) for p, m in truck_a._collect_parts_with_multiplier()}
        self.assertIn((bolt.pk, 10), a)
        b = {(p.pk, m) for p, m in truck_b._collect_parts_with_multiplier()}
        self.assertIn((bolt.pk, 10), b)

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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python manage.py test core.tests.test_aggregation_dag -v 2`
Expected: FAIL — `test_diamond_counts_every_path` yields 2 or 3 (global-skip undercount);
`test_shared_module_counts_under_each_parent` may pass by luck but keep it as a guard.

- [ ] **Step 3: Rewrite the collector on edges**

In `core/models/projects.py`, replace `_collect_parts_with_multiplier` and add
`_expand_parts_relative`:

```python
    def _expand_parts_relative(
        self,
        _path: set[int],
        _memo: dict[int, list[tuple[Part, int]]],
    ) -> list[tuple[Part, int]]:
        """Return ``[(part, multiplier_relative_to_self)]`` over the composition DAG.

        Traverses ``part_links`` (direct parts) and ``child_links`` (child modules),
        multiplying each edge ``quantity`` along the path. The node itself counts as ×1.
        A per-node ``_memo`` caches the (path-independent, in an acyclic graph) expansion so
        a module shared via several paths is expanded once and scaled per incoming edge.
        ``_path`` guards against a corrupt persisted cycle: a node recurring on its own
        recursion stack contributes nothing further (traversal terminates, degrading
        gracefully — consistent with the Phase-1 corrupt-cycle stance).
        """
        if self.pk in _memo:
            return _memo[self.pk]
        if self.pk in _path:
            return []
        next_path = _path | {self.pk}
        rel: list[tuple[Part, int]] = [(link.part, link.quantity) for link in self.part_links.all()]
        for edge in self.child_links.all():
            for part, mult in edge.child_project._expand_parts_relative(next_path, _memo):
                rel.append((part, edge.quantity * mult))
        _memo[self.pk] = rel
        return rel

    def _collect_parts_with_multiplier(self, multiplier: int = 1) -> list[tuple[Part, int]]:
        """Collect all parts over the composition DAG with their effective multiplier.

        Reads the Phase-1 composition edges (``part_links``/``child_links``). Each part is
        returned once per distinct path to it, scaled by the product of edge quantities on
        that path times ``multiplier`` — so a building block shared by two assemblies (or
        reached via a diamond) contributes correctly to each.

        Args:
            multiplier: Outer quantity factor applied to every collected part.

        Returns:
            List of ``(part, effective_multiplier)`` tuples.
        """
        rel = self._expand_parts_relative(set(), {})
        return [(part, multiplier * mult) for part, mult in rel]
```

Note: the old `_visited` parameter is removed from `_collect_parts_with_multiplier`. If any
caller passed `_visited=`, update it (grep confirms callers use it with no args or
`multiplier` only).

- [ ] **Step 4: Run DAG tests + full aggregation module**

Run: `.venv/bin/python manage.py test core.tests.test_aggregation_dag core.tests.test_aggregation -v 2`
Expected: PASS. Existing tree tests in `test_aggregation.py` stay green because dual-write
mirrored their `parent=`/`project=` trees into edges.

- [ ] **Step 5: Commit**

```bash
git add core/models/projects.py core/tests/test_aggregation_dag.py
git commit -m "feat: DAG-correct part aggregation over composition edges (Phase 2)"
```

---

### Task 2: Hardware & document collectors on edges

**Files:**
- Modify: `core/models/projects.py`
- Test: `core/tests/test_aggregation_dag.py`

**Interfaces:**
- Produces: `_collect_hardware_with_multiplier(multiplier: int = 1)` and
  `_collect_documents()` traversing `child_links` instead of `subprojects`.

- [ ] **Step 1: Write failing tests**

Append to `core/tests/test_aggregation_dag.py`:

```python
from core.models import HardwarePart, ProjectHardware


class DagHardwareDocumentTests(TestCase):
    def test_hardware_shared_module_via_edges(self):
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
```

- [ ] **Step 2: Run to verify fail**

Run: `.venv/bin/python manage.py test core.tests.test_aggregation_dag.DagHardwareDocumentTests -v 2`
Expected: FAIL (collectors still read `subprojects`, which is empty for edge-only graphs).

- [ ] **Step 3: Rewrite both collectors**

In `core/models/projects.py`, change the recursion in `_collect_hardware_with_multiplier`
and `_collect_documents` from `self.subprojects.all()` to the edges, applying the same
path-local guard. For hardware (keeps a multiplier):

```python
    def _collect_hardware_with_multiplier(
        self, multiplier: int = 1, _path: set[int] | None = None
    ) -> list[tuple[ProjectHardware, int]]:
        """Recursively collect hardware assignments over the DAG with quantity multiplier."""
        if _path is None:
            _path = set()
        if self.pk in _path:
            return []
        next_path = _path | {self.pk}
        result = [(hw, multiplier) for hw in self.hardware_assignments.select_related("hardware_part").all()]
        for edge in self.child_links.all():
            result.extend(
                edge.child_project._collect_hardware_with_multiplier(multiplier * edge.quantity, next_path)
            )
        return result
```

For documents (no multiplier):

```python
    def _collect_documents(self, _path: set[int] | None = None) -> list[tuple[ProjectDocument, Project]]:
        """Recursively collect documents from this project and its child modules (DAG)."""
        if _path is None:
            _path = set()
        if self.pk in _path:
            return []
        next_path = _path | {self.pk}
        result = [(doc, self) for doc in self.documents.all()]
        for edge in self.child_links.all():
            result.extend(edge.child_project._collect_documents(next_path))
        return result
```

Note: hardware/documents keep the simpler path-local guard (no memo) — they are lower
frequency than parts and this keeps them readable; a shared module's hardware/docs counting
once per path is the intended behavior, matching parts.

- [ ] **Step 4: Run tests**

Run: `.venv/bin/python manage.py test core.tests.test_aggregation_dag core.tests.test_aggregation -v 2`
Expected: PASS (new + existing).

- [ ] **Step 5: Commit**

```bash
git add core/models/projects.py core/tests/test_aggregation_dag.py
git commit -m "feat: hardware/document aggregation over composition edges (Phase 2)"
```

---

### Task 3: `get_descendant_ids` on edges

**Files:**
- Modify: `core/models/projects.py`
- Test: `core/tests/test_aggregation_dag.py`

**Interfaces:**
- Produces: `get_descendant_ids(_path: set[int] | None = None) -> set[int]` traversing
  `child_links` (child projects reachable via composition edges).

- [ ] **Step 1: Write failing test**

Append:

```python
class DagDescendantTests(TestCase):
    def test_descendants_via_edges_including_shared(self):
        root = Project.objects.create(name="root")
        m1 = Project.objects.create(name="m1")
        shared = Project.objects.create(name="shared")
        ProjectComponent.objects.create(parent_project=root, child_project=m1, quantity=1)
        ProjectComponent.objects.create(parent_project=root, child_project=shared, quantity=1)
        ProjectComponent.objects.create(parent_project=m1, child_project=shared, quantity=1)
        self.assertEqual(root.get_descendant_ids(), {m1.pk, shared.pk})
```

- [ ] **Step 2: Run to verify fail**

Run: `.venv/bin/python manage.py test core.tests.test_aggregation_dag.DagDescendantTests -v 2`
Expected: FAIL (reads `self.subprojects`, empty for edge-only graph).

- [ ] **Step 3: Rewrite**

```python
    def get_descendant_ids(self, _path: set[int] | None = None) -> set[int]:
        """Return the set of PKs of all descendant projects over the composition DAG.

        Traverses ``child_links``; a path-local guard makes a corrupt persisted cycle
        terminate. Shared modules reached via multiple paths appear once in the set.
        """
        if _path is None:
            _path = set()
        if self.pk in _path:
            return set()
        next_path = _path | {self.pk}
        ids: set[int] = set()
        for edge in self.child_links.all():
            child = edge.child_project
            ids.add(child.pk)
            ids |= child.get_descendant_ids(next_path)
        return ids
```

- [ ] **Step 4: Run tests**

Run: `.venv/bin/python manage.py test core.tests.test_aggregation_dag core.tests.test_aggregation -v 2`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add core/models/projects.py core/tests/test_aggregation_dag.py
git commit -m "feat: get_descendant_ids over composition edges (Phase 2)"
```

---

### Task 4: Prefetch lookups on edges (N+1 stays flat)

**Files:**
- Modify: `core/models/projects.py`
- Modify: `core/tests/test_aggregation.py` (update the prefetch-internal references)
- Test: `core/tests/test_aggregation.py` (existing N+1 tests must pass)

**Interfaces:**
- Produces: `_AGGREGATE_PART_LEAF = "part_links__part__job_entries__print_job__plates"`;
  `aggregate_prefetch_lookups(depth: int = 3)` recursing via `child_links__child_project__`.

- [ ] **Step 1: Update the prefetch builder**

In `core/models/projects.py`:

```python
    #: Relation chain that carries everything the aggregate properties need for one node
    #: (its parts via edges, and each part's completed-plate job info).
    _AGGREGATE_PART_LEAF = "part_links__part__job_entries__print_job__plates"

    @classmethod
    def aggregate_prefetch_lookups(cls, depth: int = 3) -> list[str]:
        """Prefetch lookups that keep the DAG aggregate properties query-flat.

        Covers, up to ``depth`` levels of ``child_links`` nesting, the part-edge leaf and
        the child-edge relation so the recursive collectors traverse from cache instead of
        issuing a query per node/part. Levels below ``depth`` degrade to lazy queries.
        """
        lookups: list[str] = []
        prefix = ""
        for _ in range(depth + 1):
            lookups.append(f"{prefix}{cls._AGGREGATE_PART_LEAF}")
            lookups.append(f"{prefix}child_links__child_project")
            prefix += "child_links__child_project__"
        return lookups
```

Note: the collectors read `link.part` (needs `part_links__part`) and
`edge.child_project` (needs `child_links__child_project`); the leaf continues to
`__job_entries__print_job__plates` on the part. Confirm `Part.printed_quantity`'s prefetch
branch still matches (`job_entries` prefetched on the part) — it does, since the leaf ends
at the same `job_entries__print_job__plates` chain, now rooted at `part_links__part`.

- [ ] **Step 2: Update existing N+1 tests to edge-aware setup/expectations**

In `core/tests/test_aggregation.py`, the N+1 tests (`ProjectAggregatePrefetchTests`) build
trees with `parent=`/`project=` (dual-write mirrors edges) and call
`Project.aggregate_prefetch_lookups()` — keep that. Update only internals that name the old
relations: any assertion or comment referencing `subprojects` prefetch depth should refer to
`child_links`. The `test_prefetch_flat_at_full_depth` docstring/comment mentioning the
trailing `subprojects` lookup must be updated to `child_links__child_project`. Do not weaken
the assertions (`assertNumQueries(0)` / query-count-independent-of-node-count must still hold).

- [ ] **Step 3: Run the N+1 + full aggregation suite**

Run: `.venv/bin/python manage.py test core.tests.test_aggregation -v 2`
Expected: PASS, including `assertNumQueries(0)` — proving the edge prefetch keeps aggregation
flat. If a stray query appears, inspect which relation is not cached (likely a missing
`__part` or `__child_project` hop) and align the leaf/child lookup accordingly.

- [ ] **Step 4: Full suite + gates**

Run: `.venv/bin/python manage.py test core`
Expected: PASS (whole suite; dual-write keeps every FK-built fixture equivalent under edge
reads).
Run: `ruff check . && ruff format --check . && .venv/bin/python manage.py makemigrations --check --dry-run && .venv/bin/python manage.py check --fail-level WARNING`
Expected: all clean (no migration — read-path only).

- [ ] **Step 5: Commit**

```bash
git add core/models/projects.py core/tests/test_aggregation.py
git commit -m "feat: aggregate prefetch over composition edges (Phase 2, N+1 stays flat)"
```

---

## Self-Review

**Spec coverage (Phase 2 scope):**
- "rewrite `_collect_*` … to traverse the edges" → Tasks 1 (parts), 2 (hardware/docs). ✅
- "DAG cycle guard + memoization" → Task 1 (`_expand_parts_relative` memo + path guard);
  path guard also in Tasks 2/3. ✅
- "prefetch … rewritten against the through-relations; depth capped" → Task 4. ✅
- Multi-path/diamond correctness (the DAG's whole point) → Task 1 `test_diamond_counts_every_path`. ✅
- Out of scope (correctly deferred): navigation/`get_ancestors`/"Used in" & delete semantics
  (Phase 3); print attribution (Phase 4); field removal (Phase 6). `get_ancestors` still reads
  the `parent` FK — fine, dual-write keeps it valid; it is rewritten in Phase 3.

**Placeholder scan:** none — every step has concrete code/tests. ✅

**Type consistency:** relation names `part_links`/`child_links` and `.part`/`.child_project`/
`.quantity` used consistently across Tasks 1–4 and match the merged Phase-1 models. Helper
`_expand_parts_relative(_path, _memo)` signature is consistent between definition (Task 1
Step 3) and its caller. `_AGGREGATE_PART_LEAF` string and `aggregate_prefetch_lookups`
recursion prefix (`child_links__child_project__`) are consistent. ✅

**Regression guard:** existing `test_aggregation.py` tree tests are the safety net (dual-write
makes FK trees == edge graphs); they must stay green in every task, and Task 4 preserves the
`assertNumQueries(0)` N+1 guarantee.
