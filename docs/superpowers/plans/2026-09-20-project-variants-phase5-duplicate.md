# Project Variants — Phase 5 (Duplicate as variant)

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development (or executing-plans). Steps use checkbox (`- [ ]`).
>
> **CRITICAL Kontor rule:** run every test/verify command in the FOREGROUND (one blocking Bash call, generous timeout). NEVER `run_in_background`, `Monitor`, or `gh ... --watch` to wait — they don't reliably re-invoke you here and you will stall. After push + PR, send the parent ONE `status:info` message and STOP; the coordinator drives CI/Copilot/merge (and pokes you for a single foreground fix-turn if needed).

**Goal:** One-click **"Duplicate as variant"** on a project: create a new assembly that
**references the same** child modules, parts, and hardware (via new composition edges — the
nodes/parts are shared, not copied), so the user can then swap just the differing part (e.g. the
hood) to make Variant B. No migration.

**Architecture:** Convenience layer over Phase 1–3 edges. Duplicating copies the source project's
`ProjectComponent` edges, `ProjectPart` edges, and `ProjectHardware` assignments onto a fresh
`Project`; the referenced child projects / parts / hardware objects are shared (only the thin
assembly node and its edges are new). Documents are NOT copied (they are file attachments specific
to the source). Print history/attribution is per-assembly, so the new variant starts at 0 progress.

**Tech Stack:** Django 6.0, Python 3.13, SQLite, Bootstrap 5.3, ruff 0.15.20, Django test runner.

**Spec:** `docs/superpowers/specs/2026-09-20-project-variants-dag-composition-design.md`
(section "UI/UX flows" → "Duplicate as variant").

## Global Constraints

- Base: `main` at/after `66e5ebb` (Phase 4 merged). Branch from current main.
- Double quotes; f-strings; type hints; Google-style docstrings. CBV only; Django ORM.
- **RBAC:** the duplicate view is a write op → `ProjectManageMixin` (raise_exception 403).
- **No migration** (no model/schema change). `makemigrations --check` stays clean.
- **File ownership:** `core/models/projects.py` (helper), `core/views/projects.py`,
  `core/views/__init__.py`, `core/urls/projects.py`, `core/templates/core/project_detail.html`
  (+ a small confirm template if used), tests. Do NOT touch composition.py / migrations /
  print models.
- Tests: `.venv/bin/python manage.py test core` (coverage ≥ 55). Lint: `ruff check . && ruff format --check .`.
  Fresh worktree: `.venv` + `pip install -r requirements.txt coverage`; tests need `DEBUG=1` +
  `DJANGO_SECRET_KEY` + `collectstatic` first.

## Merged interfaces consumed

- `Project.child_links` (ProjectComponent: `.child_project`, `.quantity`, `.position`),
  `Project.part_links` (ProjectPart: `.part`, `.quantity`, `.position`),
  `Project.hardware_assignments` (ProjectHardware: `.hardware_part`, `.quantity`).
- `ProjectManageMixin` (core/mixins.py); existing write-view pattern (`ProjectAddComponentView` at
  projects.py:390, `ProjectCreateView` at :159). URL namespace `core:`.

---

### Task 1: `Project.duplicate_as_variant()` model helper

**Files:** `core/models/projects.py`, `core/tests/test_variant_duplicate.py` (new)

**Interfaces:** `Project.duplicate_as_variant(new_name: str, created_by=None) -> Project` —
creates and returns a new top-level `Project` with `name=new_name` (plus copied `description` and
`default_print_preset`), then copies this project's `ProjectComponent` edges (as parent),
`ProjectPart` edges, and `ProjectHardware` assignments onto it. Child projects / parts / hardware
are referenced (shared), not deep-copied. The new project has no incoming edge (top-level).

- [ ] **Step 1: Failing test** (`core/tests/test_variant_duplicate.py`):

```python
"""Duplicate-as-variant (Phase 5)."""

from django.test import TestCase

from core.models import (
    HardwarePart, Part, Project, ProjectComponent, ProjectHardware, ProjectPart,
)


class DuplicateAsVariantModelTests(TestCase):
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
        # component edge copied, pointing at the SAME cabin node
        ce = variant.child_links.get()
        self.assertEqual(ce.child_project_id, cabin.pk)
        self.assertEqual(ce.quantity, 2)
        # part edge copied, referencing the SAME part
        pe = variant.part_links.get()
        self.assertEqual(pe.part_id, bolt.pk)
        self.assertEqual(pe.quantity, 10)
        # hardware assignment copied
        self.assertEqual(variant.hardware_assignments.get().hardware_part_id, hp.pk)
        # source untouched
        self.assertEqual(truck.child_links.count(), 1)

    def test_variant_is_top_level(self):
        truck, *_ = self._source()
        variant = truck.duplicate_as_variant("Truck B")
        self.assertFalse(variant.is_subproject)  # no incoming component edge
```

- [ ] **Step 2: Run → fail** (`AttributeError: duplicate_as_variant`).
- [ ] **Step 3: Implement** on `Project`:

```python
    def duplicate_as_variant(self, new_name: str, created_by=None) -> "Project":
        """Create a new assembly that references the same building blocks as this one.

        Copies this project's composition edges (``ProjectComponent`` where it is the parent,
        ``ProjectPart``) and ``ProjectHardware`` assignments onto a fresh top-level project. The
        referenced child projects, parts, and hardware objects are shared (not deep-copied), so
        editing a shared block still affects both. Documents are not copied. The caller then
        re-points the edges that should differ (e.g. swap the hood module).

        Args:
            new_name: Name for the new variant project.
            created_by: Optional user to record as creator.

        Returns:
            The newly created variant :class:`Project`.
        """
        from core.models.composition import ProjectComponent, ProjectPart
        from core.models.hardware import ProjectHardware

        variant = Project.objects.create(
            name=new_name,
            description=self.description,
            default_print_preset=self.default_print_preset,
            created_by=created_by,
        )
        ProjectComponent.objects.bulk_create(
            [
                ProjectComponent(
                    parent_project=variant,
                    child_project_id=edge.child_project_id,
                    quantity=edge.quantity,
                    position=edge.position,
                )
                for edge in self.child_links.all()
            ]
        )
        ProjectPart.objects.bulk_create(
            [
                ProjectPart(
                    project=variant,
                    part_id=link.part_id,
                    quantity=link.quantity,
                    position=link.position,
                )
                for link in self.part_links.all()
            ]
        )
        ProjectHardware.objects.bulk_create(
            [
                ProjectHardware(
                    project=variant,
                    hardware_part_id=hw.hardware_part_id,
                    quantity=hw.quantity,
                )
                for hw in self.hardware_assignments.all()
            ]
        )
        return variant
```

Note: `bulk_create` bypasses `ProjectComponent.save()`'s cycle guard, which is fine here — copying
an already-acyclic assembly's edges onto a brand-new node cannot introduce a cycle (the new node
has no other relations). Confirm `ProjectHardware` field names via `core/models/hardware.py` before
coding (adjust if the FK/quantity names differ).

- [ ] **Step 4: Run → pass.** Commit.

---

### Task 2: `ProjectDuplicateAsVariantView` + URL

**Files:** `core/views/projects.py`, `core/views/__init__.py`, `core/urls/projects.py`, `core/tests/test_views_projects.py`

**Interfaces:** `ProjectDuplicateAsVariantView(ProjectManageMixin, View)` at
`projects/<int:pk>/duplicate/` name `core:project_duplicate`. GET renders a small confirm form
(pre-filled name = "<source name> (Variant)"); POST calls `duplicate_as_variant(name, created_by=
request.user)` and redirects to the new project's detail page.

- [ ] **Step 1: Failing test:**

```python
    def test_duplicate_as_variant_view_creates_variant(self):
        from core.models import Project, ProjectComponent
        truck = Project.objects.create(name="Truck A", created_by=self.user)
        cabin = Project.objects.create(name="Cabin", created_by=self.user)
        ProjectComponent.objects.create(parent_project=truck, child_project=cabin, quantity=1)
        self.client.force_login(self.user)  # Admin in TestDataMixin
        resp = self.client.post(reverse("core:project_duplicate", kwargs={"pk": truck.pk}), {"name": "Truck B"})
        self.assertEqual(resp.status_code, 302)
        variant = Project.objects.get(name="Truck B")
        self.assertEqual(variant.child_links.get().child_project_id, cabin.pk)
        self.assertIn(str(variant.pk), resp["Location"])  # redirects to the new project
```

- [ ] **Step 2: Run → fail.**
- [ ] **Step 3: Implement** the view (pattern like `ProjectAddComponentView`):

```python
class ProjectDuplicateAsVariantView(ProjectManageMixin, View):
    """Duplicate a project as a new variant (shares child blocks via new edges)."""

    def get(self, request, *args, **kwargs):
        source = get_object_or_404(Project, pk=kwargs["pk"])
        return render(request, "core/project_duplicate.html", {"source": source, "suggested_name": f"{source.name} (Variant)"})

    def post(self, request, *args, **kwargs):
        source = get_object_or_404(Project, pk=kwargs["pk"])
        name = (request.POST.get("name") or f"{source.name} (Variant)").strip()
        variant = source.duplicate_as_variant(name, created_by=request.user)
        messages.success(request, f"Created variant “{variant.name}”. Swap the parts that differ.")
        return redirect(reverse("core:project_detail", kwargs={"pk": variant.pk}))
```

Register in `core/urls/projects.py` (`path("projects/<int:pk>/duplicate/",
ProjectDuplicateAsVariantView.as_view(), name="project_duplicate")`) and export from
`core/views/__init__.py`. Create `core/templates/core/project_duplicate.html` (extends base; a
small form with a `name` field + submit; `{% csrf_token %}`).

- [ ] **Step 4: Run → pass.** Commit.

---

### Task 3: "Duplicate as variant" button on the project detail

**Files:** `core/templates/core/project_detail.html`, test in `test_views_projects.py`

- [ ] **Step 1: Failing render test:**

```python
    def test_detail_shows_duplicate_button_for_manager(self):
        proj = Project.objects.create(name="Truck A", created_by=self.user)
        self.client.force_login(self.user)
        resp = self.client.get(reverse("core:project_detail", kwargs={"pk": proj.pk}))
        self.assertContains(resp, "Duplicate as variant")
```

- [ ] **Step 2: Run → fail.**
- [ ] **Step 3:** add a "Duplicate as variant" action button (link to `project_duplicate`) in the
  project detail actions area, shown only when `can_edit_assembly`/`user.has_perm("core.can_manage_projects")`
  (the detail context already exposes manage state from Phase 3b — reuse it).
- [ ] **Step 4: Run → pass.** Commit.

---

### Task 4: Full suite + gates (foreground) + PR

- [ ] **Step 1 (foreground):**
```bash
cd <worktree> && DEBUG=1 DJANGO_SECRET_KEY=phase5 .venv/bin/python manage.py test core 2>&1 | tail -20
ruff check . && ruff format --check .
.venv/bin/python manage.py makemigrations --check --dry-run   # no new migration
.venv/bin/python manage.py check --fail-level WARNING
```
- [ ] **Step 2:** push + `gh pr create` (title "feat: duplicate as variant — Project Variants
  Phase 5"; body: model helper, view+url, detail button; shares nodes, copies edges+hardware, no
  migration). Write PR# to memory `feature/project-variants/phase5-progress`, send parent ONE
  `status:info`, STOP.

---

## Self-Review

**Spec coverage:** "Duplicate as variant" copies an assembly's edges into a new assembly,
referencing (not copying) nodes → Task 1 helper + Task 2 view + Task 3 button. Hardware copied,
documents excluded (documented). No migration. ✅

**Placeholder scan:** Task 1 note "confirm ProjectHardware field names in hardware.py" is an
instruction to verify, not a code placeholder. All steps concrete. ✅

**Type consistency:** `duplicate_as_variant(new_name, created_by=None)` used identically in helper,
view, and tests; URL name `project_duplicate`; relations `child_links`/`part_links`/
`hardware_assignments` match merged models. ✅

**Cycle safety:** copying edges onto a fresh node cannot cycle (new node has no other edges);
`bulk_create` is therefore safe and efficient. ✅

**Kontor discipline:** foreground waits; PR then hand off. ✅
