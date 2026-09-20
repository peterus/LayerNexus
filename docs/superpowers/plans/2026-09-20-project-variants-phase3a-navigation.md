# Project Variants — Phase 3a (Migrate: read/navigation onto edges) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
>
> **CRITICAL Kontor rule:** run every test/verification command in the FOREGROUND (a single blocking Bash call, generous timeout). NEVER use `run_in_background`, the `Monitor` tool, or `gh ... --watch` to wait — background/event waits do not reliably re-invoke you here and you will stall. Push your branch, open the PR, then send the parent ONE `status:info` message and STOP; the coordinator drives CI/Copilot/merge.

**Goal:** Switch the **display and navigation** layer (project list/detail views + templates, dashboard counts, breadcrumbs) from the legacy FK relations (`project.parts`, `project.subprojects`, `get_ancestors`, `parent__isnull`) onto the Phase-1/2 composition edges (`part_links`, `child_links`, `parent_links`), and replace the single-path ancestor breadcrumb with an edge-based **"Used in"** navigation. Read/navigation only — no editing, no delete-semantics change, no migration. (Editing = Phase 3b.)

**Architecture:** Migrate step of expand–migrate–contract. Phase 1 added edges + dual-write; Phase 2 moved aggregation onto edges. This phase moves the remaining **read/display** consumers onto edges. Dual-write keeps edges equivalent to the FK tree, so existing single-owner projects render identically; new capability is only that a module/part referenced by several assemblies now shows all of them under "Used in".

**Tech Stack:** Django 6.0, Python 3.13 (local `.venv`), SQLite, Django test runner (no pytest), Bootstrap 5.3 templates, ruff 0.15.20.

**Spec:** `docs/superpowers/specs/2026-09-20-project-variants-dag-composition-design.md`

## Global Constraints

- Base: `main` at/after `55411bb` (Phase 2 merged). Branch this work from current main.
- Double quotes only; f-strings; type hints on all signatures; Google-style docstrings.
- Class-based views only; RBAC mixins unchanged (this phase adds no write views).
- **No migration** (read/display only). `makemigrations --check --dry-run` stays clean.
- **File ownership:** `core/models/projects.py`, `core/models/parts.py` (read helpers only),
  `core/views/projects.py`, `core/views/parts.py`, `core/views/documents.py`,
  `core/views/hardware.py`, and the templates `core/templates/core/project_detail.html`,
  `project_list.html`, `dashboard.html`, plus the breadcrumb include if one exists, and
  tests under `core/tests/`. Do NOT add write/edit views, forms, or delete-semantics changes
  (those are Phase 3b). Do NOT touch `composition.py` or migrations.
- Tests: `.venv/bin/python manage.py test core` (coverage ≥ 55). Lint: `ruff check . &&
  ruff format --check .`. Fresh worktree: create `.venv` (`python3 -m venv .venv`,
  `pip install -r requirements.txt coverage`); tests need `DEBUG=1` + `DJANGO_SECRET_KEY`
  and a `collectstatic` run first (ManifestStaticFilesStorage).

## Merged interfaces this phase consumes (from Phase 1/2)

- `Project.part_links` (reverse of `ProjectPart.project`) → each `.part`, `.quantity`, `.position`.
- `Project.child_links` (reverse of `ProjectComponent.parent_project`) → each `.child_project`, `.quantity`, `.position`.
- `Project.parent_links` (reverse of `ProjectComponent.child_project`) → each `.parent_project` (the assemblies containing this project).
- `Part.project_links` (reverse of `ProjectPart.part`) → each `.project` (the modules containing this part).
- Aggregation already edge-based: `_collect_parts_with_multiplier`, `aggregate_prefetch_lookups` (uses `child_links__child_project` + `part_links__part__...`).

---

### Task 1: Edge-based navigation/display helpers on the models

**Files:**
- Modify: `core/models/projects.py`, `core/models/parts.py`
- Test: `core/tests/test_navigation_edges.py` (new)

**Interfaces:**
- Produces:
  - `Project.parent_assemblies() -> list[Project]` — distinct projects that directly contain
    this one via a `ProjectComponent` edge (its "used in" list).
  - `Project.child_modules() -> list[tuple[Project, int]]` — `(child_project, quantity)` from
    `child_links` (ordered by `position, pk`).
  - `Project.direct_parts() -> list[tuple[Part, int]]` — `(part, quantity)` from `part_links`
    (ordered).
  - `Project.direct_part_count() -> int` — number of distinct `part_links` (replaces
    `project.parts.count` on the detail/list "this node's own parts" display).
  - `Project.is_subproject` becomes edge-based: `parent_links.exists()`.
  - `Part.containing_projects() -> list[Project]` — distinct projects containing this part
    via `project_links` (the part's "used in").

- [ ] **Step 1: Write the failing test**

Create `core/tests/test_navigation_edges.py`:

```python
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
        home = Project.objects.create(name="home")
        bolt = Part.objects.create(project=home, name="bolt", quantity=1)
        ProjectPart.objects.create(project=truck, part=bolt, quantity=10, position=0)

        self.assertEqual([(c.name, q) for c, q in truck.child_modules()], [("cabin", 2)])
        self.assertEqual([(p.name, q) for p, q in truck.direct_parts()], [("bolt", 10)])
        self.assertEqual(truck.direct_part_count(), 1)

    def test_part_containing_projects(self):
        home = Project.objects.create(name="home")
        bolt = Part.objects.create(project=home, name="bolt", quantity=1)
        m1 = Project.objects.create(name="m1")
        m2 = Project.objects.create(name="m2")
        ProjectPart.objects.create(project=m1, part=bolt)
        ProjectPart.objects.create(project=m2, part=bolt)
        self.assertEqual({p.name for p in bolt.containing_projects()}, {"home", "m1", "m2"})
```

Note: `bolt` gets a `home` project (its FK is NOT NULL until Phase 6). Dual-write creates a
`ProjectPart(home, bolt)` edge, so `containing_projects()` includes `home` too — assert that.

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python manage.py test core.tests.test_navigation_edges -v 2`
Expected: FAIL — `AttributeError: 'Project' object has no attribute 'parent_assemblies'`.

- [ ] **Step 3: Implement the helpers**

In `core/models/projects.py` add (methods on `Project`):

```python
    def parent_assemblies(self) -> list["Project"]:
        """Return distinct assemblies that directly contain this project (its "used in")."""
        seen: dict[int, "Project"] = {}
        for edge in self.parent_links.select_related("parent_project").all():
            seen.setdefault(edge.parent_project_id, edge.parent_project)
        return list(seen.values())

    def child_modules(self) -> list[tuple["Project", int]]:
        """Return ``(child_project, quantity)`` pairs from composition edges, ordered."""
        return [(e.child_project, e.quantity) for e in self.child_links.select_related("child_project").all()]

    def direct_parts(self) -> list[tuple["Part", int]]:
        """Return ``(part, quantity)`` pairs directly attached to this project, ordered."""
        return [(link.part, link.quantity) for link in self.part_links.select_related("part").all()]

    def direct_part_count(self) -> int:
        """Number of distinct parts directly attached to this project (via edges)."""
        return self.part_links.count()
```

Replace the existing `is_subproject` property body:

```python
    @property
    def is_subproject(self) -> bool:
        """Return True if any assembly references this project via a composition edge."""
        return self.parent_links.exists()
```

In `core/models/parts.py` add (method on `Part`):

```python
    def containing_projects(self) -> list["Project"]:
        """Return distinct projects that include this part via a composition edge."""
        seen: dict[int, "Project"] = {}
        for link in self.project_links.select_related("project").all():
            seen.setdefault(link.project_id, link.project)
        return list(seen.values())
```

(Import `Project` under `TYPE_CHECKING` already present in parts.py; use string annotations.)

- [ ] **Step 4: Run test to verify pass**

Run: `.venv/bin/python manage.py test core.tests.test_navigation_edges -v 2`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add core/models/projects.py core/models/parts.py core/tests/test_navigation_edges.py
git commit -m "feat: edge-based navigation/display helpers (Phase 3a)"
```

---

### Task 2: Project list + detail views onto edges + "used in" context

**Files:**
- Modify: `core/views/projects.py`
- Test: `core/tests/test_views_projects.py` (extend)

**Interfaces:**
- Consumes Task 1 helpers.
- Produces: `ProjectListView` top-level filter uses `parent_links__isnull=True` (+ `.distinct()`);
  `ProjectDetailView` context provides `child_modules`, `direct_parts`, and `used_in`
  (parent assemblies) instead of `subprojects`/`ancestors`; `ProjectCostView`/re-estimate use
  edge parts.

- [ ] **Step 1: Write the failing test**

Add to `core/tests/test_views_projects.py` (import `ProjectComponent` if needed):

```python
    def test_detail_shows_used_in_for_shared_module(self):
        from core.models import ProjectComponent
        cabin = Project.objects.create(name="Cabin", created_by=self.user)
        truck = Project.objects.create(name="Truck A", created_by=self.user)
        ProjectComponent.objects.create(parent_project=truck, child_project=cabin)
        self.client.force_login(self.user)
        resp = self.client.get(reverse("core:project_detail", kwargs={"pk": cabin.pk}))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Truck A")  # used-in assembly shown

    def test_list_shows_only_top_level_edge_based(self):
        from core.models import ProjectComponent
        parent = Project.objects.create(name="TopTruck", created_by=self.user)
        child = Project.objects.create(name="ChildModule", created_by=self.user)
        ProjectComponent.objects.create(parent_project=parent, child_project=child)
        self.client.force_login(self.user)
        resp = self.client.get(reverse("core:project_list"))
        self.assertContains(resp, "TopTruck")
        self.assertNotContains(resp, "ChildModule")  # referenced module is not top-level
```

- [ ] **Step 2: Run to verify fail**

Run: `.venv/bin/python manage.py test core.tests.test_views_projects -v 2`
Expected: at least `test_detail_shows_used_in_for_shared_module` fails (no used_in context/render).

- [ ] **Step 3: Update the views**

In `core/views/projects.py`:
- Line ~54 `ProjectListView.get_queryset`: change `filter(parent__isnull=True)` to
  `filter(parent_links__isnull=True).distinct()` (keep the existing `prefetch_related(*Project.aggregate_prefetch_lookups())`).
- `ProjectDetailView.get_context_data` (~73-77): replace
  `context["subprojects"] = self.object.subprojects.all()` with
  `context["child_modules"] = self.object.child_modules()`; replace
  `context["ancestors"] = self.object.get_ancestors()` with
  `context["used_in"] = self.object.parent_assemblies()`; replace `parts = self.object.parts.all()`
  with `parts = [p for p, _q in self.object.direct_parts()]` (or expose `direct_parts` pairs
  if the template shows quantity — see Task 3).
- `ProjectCostView` (~256/258) and `ProjectReEstimateView` (~309): these already use
  `_collect_parts_with_multiplier()` where possible; where they use `project.parts.all()`,
  switch to `[p for p, _q in project.direct_parts()]`. Keep breadcrumb: replace
  `get_ancestors()` with `parent_assemblies()` into `used_in`.
- `ProjectUpdateView`/`ProjectDeleteView` breadcrumb (~190/229): replace
  `context["ancestors"] = self.object.get_ancestors()` with
  `context["used_in"] = self.object.parent_assemblies()`.
- `ProjectDeleteView` redirect (~234): `if self.object.is_subproject:` — keep behavior but make
  the redirect robust for multiple parents: redirect to the first parent assembly if any, else
  the project list:

```python
        parents = self.object.parent_assemblies()
        if parents:
            return reverse("core:project_detail", kwargs={"pk": parents[0].pk})
        return reverse("core:project_list")
```

- `SubProjectCreateView` (~147-170): leave the creation flow as-is (it sets `parent=` and
  dual-write mirrors the edge). Only replace its breadcrumb `get_ancestors()` usage (~156)
  with `parent.parent_assemblies() + [parent]` → `used_in`.

- [ ] **Step 4: Run to verify pass**

Run: `.venv/bin/python manage.py test core.tests.test_views_projects -v 2`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add core/views/projects.py core/tests/test_views_projects.py
git commit -m "feat: project list/detail views read edges + used-in (Phase 3a)"
```

---

### Task 3: Templates — edge-based counts/lists + "Used in" breadcrumb

**Files:**
- Modify: `core/templates/core/project_detail.html`, `project_list.html`, `dashboard.html`
- Test: covered by Task 2 view tests (`assertContains`); add one render assertion for counts.

**Interfaces:** consumes `child_modules`, `direct_parts`, `used_in`, and the model helpers.

- [ ] **Step 1: Update templates**

- `project_detail.html`:
  - Line ~58 `{{ project.parts.count }}` → `{{ project.direct_part_count }}`.
  - Line ~214 `{{ sub.parts.count }}` (inside the sub-projects table) → iterate
    `child_modules` as `(sub, qty)` and use `{{ sub.direct_part_count }}`; show the edge
    `qty`. Replace the `{% if subprojects %}`/`{% for sub in subprojects %}` block to loop
    `child_modules` (each is a `(project, quantity)` pair — use `{% for sub, qty in child_modules %}`).
  - Lines ~252/267 `{% if project.parts.all %}` / `{% for part in project.parts.all %}` →
    `{% if direct_parts %}` / `{% for part, qty in direct_parts %}` and render `qty`.
  - Breadcrumb: wherever `ancestors` were rendered, render a **"Used in"** section from
    `used_in` (list of assemblies as links) — e.g. a small `<nav>` of badges linking to each
    parent assembly; show nothing if empty (top-level).
- `project_list.html` line ~30 `{{ project.parts.count }} parts` → `{{ project.direct_part_count }} parts`
  (keep `total_parts_count` on line 31 — that already aggregates over edges from Phase 2).
- `dashboard.html` line ~128 `{{ project.parts.count }} parts` → `{{ project.direct_part_count }}`.
  (Line ~160 `job.parts.count` is a PrintJob relation, NOT project parts — leave it unchanged.)

- [ ] **Step 2: Add a render assertion**

Add to `core/tests/test_views_projects.py`:

```python
    def test_detail_part_count_uses_edges(self):
        from core.models import Part, ProjectPart
        proj = Project.objects.create(name="CountProj", created_by=self.user)
        home = Project.objects.create(name="home", created_by=self.user)
        p = Part.objects.create(project=home, name="X", quantity=1)
        ProjectPart.objects.create(project=proj, part=p)  # direct edge into proj
        self.client.force_login(self.user)
        resp = self.client.get(reverse("core:project_detail", kwargs={"pk": proj.pk}))
        self.assertContains(resp, "X")  # the referenced part is listed
```

- [ ] **Step 3: Run the view tests + full app**

Run: `.venv/bin/python manage.py test core.tests.test_views_projects core.tests.test_navigation_edges -v 2`
Expected: PASS.
Run (FOREGROUND, blocking): `.venv/bin/python manage.py test core`
Expected: PASS (whole suite; dual-write keeps FK-built fixtures equivalent under edge reads).

- [ ] **Step 4: Commit**

```bash
git add core/templates/core/project_detail.html core/templates/core/project_list.html core/templates/core/dashboard.html core/tests/test_views_projects.py
git commit -m "feat: templates render edge-based counts/lists + used-in (Phase 3a)"
```

---

### Task 4: Replace get_ancestors in part/document/hardware breadcrumbs

**Files:**
- Modify: `core/views/parts.py`, `core/views/documents.py`, `core/views/hardware.py`
- Modify: the templates these views render (breadcrumb blocks) if they iterate `ancestors`.
- Modify: `core/models/projects.py` — remove `get_ancestors` only after all callers are gone
  (grep to confirm), OR keep it if any non-migrated caller remains (do a final grep).
- Test: `core/tests/test_views_parts.py` (extend with a smoke assertion)

**Interfaces:** replaces `context["ancestors"] = <proj>.get_ancestors()` with
`context["used_in"] = <proj>.parent_assemblies()` (for a part detail, the part's own
"used in" is `part.containing_projects()`).

- [ ] **Step 1: Write a smoke test**

Add to `core/tests/test_views_parts.py`:

```python
    def test_part_detail_renders_with_edges(self):
        # Part detail must render without get_ancestors (edge-based used-in).
        self.client.force_login(self.user)
        resp = self.client.get(reverse("core:part_detail", kwargs={"pk": self.part.pk}))
        self.assertEqual(resp.status_code, 200)
```

- [ ] **Step 2: Run to verify current state**

Run: `.venv/bin/python manage.py test core.tests.test_views_parts.<class>::test_part_detail_renders_with_edges -v 2`
Expected: PASS today (get_ancestors still works) — this is a regression guard for the refactor.

- [ ] **Step 3: Replace get_ancestors usages**

In `core/views/parts.py` (~113, 202, 256, 272), `documents.py` (~43, 71), `hardware.py`
(~40, 85, 112): replace each `context["ancestors"] = <proj>.get_ancestors()` with
`context["used_in"] = <proj>.parent_assemblies()`. For `PartDetailView` (~113) use the part's
own containing projects: `context["used_in"] = part.containing_projects()`.

Update the corresponding templates' breadcrumb blocks: replace any `{% for a in ancestors %}`
loop with a "Used in" render from `used_in` (links to each assembly; hidden if empty). Grep
the templates for `ancestors` to find them:
`git grep -n "ancestors" core/templates`.

Then grep for remaining `get_ancestors` callers:
`git grep -n "get_ancestors" core/`. If NONE remain (outside its own definition and the
corrupt-cycle guard test), remove the `get_ancestors` method from `projects.py` and update/adjust
the one cycle-guard test that referenced it (`test_upward_walks_guarded_against_corrupt_cycle`
in `test_aggregation.py`) to drop the `get_ancestors` assertion (keep the preset-walk
assertions). If any caller remains, leave `get_ancestors` in place and note it in the PR.

- [ ] **Step 4: Run the affected suites + full suite (FOREGROUND)**

Run: `.venv/bin/python manage.py test core.tests.test_views_parts core.tests.test_views_documents core.tests.test_views_hardware core.tests.test_aggregation -v 2`
Expected: PASS.
Run: `.venv/bin/python manage.py test core`
Expected: PASS.
Run: `ruff check . && ruff format --check . && .venv/bin/python manage.py makemigrations --check --dry-run && .venv/bin/python manage.py check --fail-level WARNING`
Expected: all clean.

- [ ] **Step 5: Commit**

```bash
git add core/views/parts.py core/views/documents.py core/views/hardware.py core/templates core/models/projects.py core/tests
git commit -m "feat: part/document/hardware breadcrumbs use edge-based used-in (Phase 3a)"
```

---

## Self-Review

**Spec coverage (Phase 3a scope):**
- "edge-vs-node delete semantics, parts library, assembly editor" → **Phase 3b** (explicitly
  out of scope here). ✅
- "'Used in' navigation" → Tasks 1–4 (`parent_assemblies`/`containing_projects` + template
  render). ✅
- "switch views/templates … reads onto edges" → Tasks 2–4 (list top-level filter, detail
  counts/lists, dashboard count, breadcrumbs). ✅
- `get_ancestors` (single-path) removed/deprecated → Task 4. ✅

**Placeholder scan:** none — every step has concrete code/edits; template edits reference
exact line numbers from the merged `main`. ✅

**Type consistency:** helper names `parent_assemblies()`, `child_modules()`, `direct_parts()`,
`direct_part_count()`, `containing_projects()` used identically across model, views, tests,
and template contexts (`child_modules`, `direct_parts`, `used_in`). ✅

**Regression guard:** dual-write makes every existing FK-built fixture equivalent under edge
reads, so the full suite is the safety net; Task 3/4 run it in the foreground. No migration,
so `makemigrations --check` stays clean.

**Kontor discipline:** all waits are FOREGROUND; PR then hand off to coordinator (no
background/Monitor/--watch).
