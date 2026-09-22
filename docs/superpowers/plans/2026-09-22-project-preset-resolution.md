# Project Preset Resolution Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
>
> **CRITICAL runtime rule:** run every test/verify command in the FOREGROUND (one blocking Bash call, generous timeout). NEVER `run_in_background`, `Monitor`, or `gh ... --watch` to wait. After push + PR, send the parent ONE `status:info` message and STOP; the coordinator drives CI/Copilot/merge.

**Goal:** Make `Project.default_print_preset` mandatory and resolve each part's slicing/estimation preset **top-down, per build path** (`part.print_preset` override else the nearest containing project's default) so legacy parts with no preset stop sending `print_preset=None` to OrcaSlicer.

**Architecture:** Composition is edge-based (`ProjectPart`, `ProjectComponent`). A new per-path resolver folds each nearest project's `default_print_preset` into the existing DAG expansion (`_expand_parts_relative`). A new `PrintJob.print_preset` field pins the resolved preset at job creation; slicing reads it instead of re-deriving from the first part. Estimation uses the same resolver and refuses to guess when a background part has ambiguous (multiple, differing) containing-project presets. Mandatory-preset is enforced at form/serializer level only (no data migration, no hardcoded preset PK).

**Tech Stack:** Django 6.0, Python 3.13 (CI 3.14), SQLite, Django REST Framework, Bootstrap 5.3, ruff 0.15.20, Django test runner.

**Spec:** `docs/superpowers/specs/2026-09-22-project-preset-resolution-design.md`

## Global Constraints

- Base: branch from current `main` @ `9715158` (PR #52 — includes #44 estimation `select_related` fix, #45 iterative build/re-estimate REST API, #51 GUI shared-part dedup, #52 drf-spectacular OpenAPI docs).
- **Double quotes only** (strings, docstrings, messages) — never single quotes.
- **f-strings** for interpolation — never `+` or `%`.
- **Type hints** on every function/method signature and return type.
- **Google-style docstrings** (English) on public functions, classes, modules.
- **Class-based views only**; Django ORM only (never raw SQL).
- Config via `os.environ.get()` with defaults — never hardcoded.
- **Never hand-edit migrations** — generate with `.venv/bin/python manage.py makemigrations`. CI gate: `makemigrations --check --dry-run` must be clean.
- **No preset PK hardcoding anywhere** (preset 4 is deployment-specific data, already set 2026-09-21 — do NOT add a data migration or hardcode it).
- New fields are **schema-only** and nullable; do **not** add a `null=False` `AlterField`.
- **RBAC:** touched write views keep their existing role mixins (`RoleRequiredMixin`, `ProjectManageMixin`); never weaken them.
- Ruff pinned `0.15.20`, line-length 120, `core/migrations` excluded. Lint gate: `ruff check . && ruff format --check .`.
- Coverage gate: `fail_under = 55` (`pyproject.toml`).
- `manage.py check --fail-level WARNING` must pass.
- Fresh worktree bootstrap: create `.venv`, `pip install -r requirements.txt` (incl. `djangorestframework`), install `coverage`; export `DEBUG=1` + `DJANGO_SECRET_KEY=...` and run `collectstatic` before tests if needed.
- Full suite: `.venv/bin/python manage.py test core`. Single module: `.venv/bin/python manage.py test core.tests.<module>`.

## Existing interfaces consumed (verified against the tree)

- `core/models/composition.py`: `ProjectPart(project, part, quantity, position)` reverse rels `project.part_links` / `part.project_links`; `ProjectComponent(parent_project, child_project, quantity, position)` reverse rels `parent.child_links` / `child.parent_links`.
- `core/models/parts.py`: `Part.print_preset` (FK, nullable), `Part.effective_print_preset` / `effective_print_preset_id` (context-free properties), `Part.containing_projects() -> list[Project]`, estimation status constants (`ESTIMATION_NONE/PENDING/ESTIMATING/SUCCESS/ERROR`), `estimation_status`, `estimation_error`.
- `core/models/projects.py`: `Project.default_print_preset` (FK, nullable), `Project._expand_parts_relative(_path, _memo) -> list[tuple[Part, int]]`, `Project._collect_parts_with_multiplier(multiplier=1) -> list[tuple[Part, int]]`, `Project.child_links`, `Project.part_links`, `Project.variant_progress() -> dict`.
- `core/models/orca_profiles.py`: `OrcaPrintPreset` (`STATE_RESOLVED`, `instantiation`).
- `core/models/printing.py`: `PrintJob` (status constants, `machine_profile`, `job_parts`), `PrintJobPart(print_job, part, quantity, target_assembly)`.
- `core/services/slicing_worker.py`: `_slice_job_in_background(job_pk)`, `_estimate_part_in_background(part_pk)`, `_build_slicer_kwargs`, `_find_compatible_machine`.
- `core/views/print_jobs.py`: `CreateJobsFromProjectView`, `AddPartToJobView`, `PrintJobDetailView`.
- `core/views/parts.py`: `PartDetailView`, `PartReEstimateView` (GUI single-part re-estimate).
- `core/views/projects.py`: `ProjectReEstimateView` (GUI whole-project re-estimate — #51 dedup: `{p.pk: p for p, _mult in project._collect_parts_with_multiplier()}`); `ProjectDetailView` (uses the still-live `effective_default_print_preset` for a display label — leave it).
- `core/api/views.py` (#45 iterative build API): `PartViewSet.estimate` (`POST /api/v1/parts/{id}/estimate/`), `ProjectViewSet.re_estimate` (`POST /api/v1/projects/{id}/re-estimate/`, same #51 dedup). Both currently gate on `part.effective_print_preset` and carry `@extend_schema` (#52 drf-spectacular) descriptions that must stay accurate. The OpenAPI schema is auto-generated from serializers/`@extend_schema` — there is no hand-written schema file to edit.
- `core/forms/projects.py`: `ProjectForm`, `SubProjectForm`, `ProjectEditForm`.
- `core/api/serializers.py`: `ProjectSerializer`.
- Baseline note (#44, already on main): `_estimate_part_in_background` already reads `Part.objects.select_related("print_preset").get(pk=part_pk)` — do NOT "fix" a stale `select_related`; it is already correct.

---

### Task 1: Leaf resolver + per-path project resolver

**Files:**
- Modify: `core/models/parts.py` (add `resolve_part_preset` module function)
- Modify: `core/models/projects.py` (add `resolve_part_presets` method)
- Test: `core/tests/test_preset_resolution.py` (create)

**Interfaces:**
- Produces: `core.models.parts.resolve_part_preset(part: Part, nearest_project: "Project | None") -> "OrcaPrintPreset | None"` — returns `part.print_preset` if set, else `nearest_project.default_print_preset` if `nearest_project` is not None, else `None`.
- Produces: `Project.resolve_part_presets(self) -> list[tuple[Part, int, "OrcaPrintPreset | None"]]` — expands the composition DAG carrying the nearest directly-containing project's `default_print_preset` down each path; each returned tuple is `(part, effective_count, resolved_preset)` where `resolved_preset` applies the override-else-nearest rule.

- [ ] **Step 1: Write the failing test**

```python
"""Tests for top-down, per-build-path print preset resolution (Variant B)."""

from django.test import TestCase

from core.models import OrcaPrintPreset, Part, Project, ProjectComponent, ProjectPart
from core.models.parts import resolve_part_preset


def _preset(name: str) -> OrcaPrintPreset:
    return OrcaPrintPreset.objects.create(
        name=name,
        orca_name=name,
        state=OrcaPrintPreset.STATE_RESOLVED,
        instantiation=True,
    )


class ResolvePartPresetLeafTests(TestCase):
    def test_override_wins_over_nearest_project(self) -> None:
        override = _preset("Override")
        nearest = _preset("Nearest")
        project = Project.objects.create(name="M", default_print_preset=nearest)
        part = Part.objects.create(name="p", print_preset=override)
        self.assertEqual(resolve_part_preset(part, project), override)

    def test_falls_back_to_nearest_project_preset(self) -> None:
        nearest = _preset("Nearest")
        project = Project.objects.create(name="M", default_print_preset=nearest)
        part = Part.objects.create(name="p")
        self.assertEqual(resolve_part_preset(part, project), nearest)

    def test_no_override_no_project_returns_none(self) -> None:
        part = Part.objects.create(name="p")
        self.assertIsNone(resolve_part_preset(part, None))


class ResolvePartPresetsPerPathTests(TestCase):
    def test_nearest_project_preset_carried_per_path(self) -> None:
        cabin_preset = _preset("CabinPreset")
        frame_preset = _preset("FramePreset")
        truck = Project.objects.create(name="Truck", default_print_preset=_preset("TruckPreset"))
        cabin = Project.objects.create(name="Cabin", default_print_preset=cabin_preset)
        frame = Project.objects.create(name="Frame", default_print_preset=frame_preset)
        ProjectComponent.objects.create(parent_project=truck, child_project=cabin, quantity=1)
        ProjectComponent.objects.create(parent_project=truck, child_project=frame, quantity=1)
        bolt = Part.objects.create(name="bolt")  # no override
        ProjectPart.objects.create(project=cabin, part=bolt, quantity=4)
        ProjectPart.objects.create(project=frame, part=bolt, quantity=10)

        resolved = truck.resolve_part_presets()
        presets = sorted((mult, preset.name) for _p, mult, preset in resolved)
        self.assertEqual(presets, [(4, "CabinPreset"), (10, "FramePreset")])

    def test_part_override_beats_nearest_on_every_path(self) -> None:
        override = _preset("Override")
        cabin = Project.objects.create(name="Cabin", default_print_preset=_preset("CabinPreset"))
        bolt = Part.objects.create(name="bolt", print_preset=override)
        ProjectPart.objects.create(project=cabin, part=bolt, quantity=2)
        resolved = cabin.resolve_part_presets()
        self.assertEqual([(p.name, m, pr.name) for p, m, pr in resolved], [("bolt", 2, "Override")])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python manage.py test core.tests.test_preset_resolution -v 2`
Expected: FAIL with `ImportError: cannot import name 'resolve_part_preset'` (and `AttributeError` for `resolve_part_presets`).

- [ ] **Step 3: Add the leaf resolver to `core/models/parts.py`**

Add at module level (after the imports, before or after the `Part` class):

```python
def resolve_part_preset(part: "Part", nearest_project: "Project | None") -> "Optional[OrcaPrintPreset]":
    """Resolve a part's effective print preset given its nearest containing project.

    Applies the Variant-B precedence: an explicit ``part.print_preset`` override wins;
    otherwise the ``default_print_preset`` of the nearest directly-containing project on
    the current build path is used; otherwise ``None``.

    Args:
        part: The part whose print preset to resolve.
        nearest_project: The project directly containing the part on this build path,
            or ``None`` when there is no project context.

    Returns:
        The resolved :class:`~core.models.orca_profiles.OrcaPrintPreset`, or ``None``.
    """
    if part.print_preset_id is not None:
        return part.print_preset
    if nearest_project is not None and nearest_project.default_print_preset_id is not None:
        return nearest_project.default_print_preset
    return None
```

- [ ] **Step 4: Add the per-path resolver to `core/models/projects.py`**

Add this method on `Project` (near `_collect_parts_with_multiplier`):

```python
def resolve_part_presets(self) -> list[tuple[Part, int, Optional[OrcaPrintPreset]]]:
    """Expand the composition DAG, resolving each part's preset per build path.

    Traverses ``part_links``/``child_links`` like :meth:`_collect_parts_with_multiplier`
    but additionally carries the **nearest directly-containing project** down each path so
    the leaf resolution (:func:`core.models.parts.resolve_part_preset`) can apply the
    override-else-nearest-project rule (Variant B). A part shared via several paths appears
    once per path, each with the preset of the module that contains it on that path.

    Returns:
        List of ``(part, effective_count, resolved_preset)`` tuples, where
        ``effective_count`` matches :meth:`_collect_parts_with_multiplier` and
        ``resolved_preset`` is an :class:`OrcaPrintPreset` or ``None``.
    """
    from core.models.parts import resolve_part_preset

    results: list[tuple[Part, int, Optional[OrcaPrintPreset]]] = []
    self._resolve_presets_walk(nearest=self, multiplier=1, _path=set(), _out=results, _resolve=resolve_part_preset)
    return results

def _resolve_presets_walk(
    self,
    nearest: Project,
    multiplier: int,
    _path: set[int],
    _out: list[tuple[Part, int, Optional[OrcaPrintPreset]]],
    _resolve,
) -> None:
    """Depth-first helper for :meth:`resolve_part_presets` (nearest-project carrier).

    ``nearest`` is the project directly containing the parts at this node. Direct parts
    (``part_links``) resolve against ``nearest``; child modules recurse with the child as
    the new ``nearest`` and the edge quantity folded into ``multiplier``. ``_path`` guards
    a corrupt persisted cycle (a node on its own stack contributes nothing further).

    Args:
        nearest: Project directly containing the parts collected at this node.
        multiplier: Product of edge quantities on the path to this node.
        _path: PKs on the current recursion stack (path-local cycle guard).
        _out: Accumulator receiving ``(part, effective_count, resolved_preset)`` tuples.
        _resolve: The leaf resolver ``resolve_part_preset`` (injected to avoid re-import).
    """
    if self.pk in _path:
        return
    next_path = _path | {self.pk}
    for link in self.part_links.select_related("part").all():
        preset = _resolve(link.part, nearest)
        _out.append((link.part, multiplier * link.quantity, preset))
    for edge in self.child_links.select_related("child_project").all():
        edge.child_project._resolve_presets_walk(
            nearest=edge.child_project,
            multiplier=multiplier * edge.quantity,
            _path=next_path,
            _out=_out,
            _resolve=_resolve,
        )
```

Ensure `Optional` and `OrcaPrintPreset` are importable: `Optional` is already imported in `projects.py`; `OrcaPrintPreset` is already under its `TYPE_CHECKING` block, so the runtime `list[tuple[...]]` annotation is fine (it is a string via `from __future__ import annotations`).

- [ ] **Step 5: Run test to verify it passes**

Run: `.venv/bin/python manage.py test core.tests.test_preset_resolution -v 2`
Expected: PASS (all 5 tests).

- [ ] **Step 6: Lint + full suite**

Run: `ruff check . && ruff format --check . && .venv/bin/python manage.py test core`
Expected: PASS, coverage ≥ 55.

- [ ] **Step 7: Commit**

```bash
git add core/models/parts.py core/models/projects.py core/tests/test_preset_resolution.py
git commit -m "feat: per-build-path print preset resolver (Variant B)"
```

---

### Task 2: `PrintJob.print_preset` field + migration

**Files:**
- Modify: `core/models/printing.py` (add `print_preset` FK to `PrintJob`)
- Create: `core/migrations/000X_printjob_print_preset.py` (generated)
- Test: `core/tests/test_models.py` (extend)

**Interfaces:**
- Produces: `PrintJob.print_preset` — `ForeignKey("OrcaPrintPreset", on_delete=models.SET_NULL, null=True, blank=True, related_name="print_jobs")`, and the descriptor `PrintJob.print_preset_id`.

- [ ] **Step 1: Write the failing test** (append to `core/tests/test_models.py`)

```python
class PrintJobPresetFieldTests(TestCase):
    def test_print_job_stores_resolved_preset(self) -> None:
        from core.models import OrcaPrintPreset, PrintJob

        preset = OrcaPrintPreset.objects.create(
            name="P", orca_name="P", state=OrcaPrintPreset.STATE_RESOLVED, instantiation=True
        )
        job = PrintJob.objects.create(name="J", print_preset=preset)
        job.refresh_from_db()
        self.assertEqual(job.print_preset_id, preset.pk)

    def test_print_job_preset_defaults_to_none(self) -> None:
        from core.models import PrintJob

        job = PrintJob.objects.create(name="J")
        self.assertIsNone(job.print_preset_id)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python manage.py test core.tests.test_models.PrintJobPresetFieldTests -v 2`
Expected: FAIL with `TypeError: ... unexpected keyword argument 'print_preset'` / attribute missing.

- [ ] **Step 3: Add the field to `PrintJob` in `core/models/printing.py`**

Add after the `machine_profile` field:

```python
    print_preset = models.ForeignKey(
        "OrcaPrintPreset",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="print_jobs",
        help_text="Print preset resolved for this job at creation (one preset per job).",
    )
```

- [ ] **Step 4: Generate the migration**

Run: `.venv/bin/python manage.py makemigrations core`
Expected: creates `core/migrations/000X_printjob_print_preset.py` (AddField only). Do NOT hand-edit it.

- [ ] **Step 5: Run test + migration gate**

Run: `.venv/bin/python manage.py test core.tests.test_models.PrintJobPresetFieldTests -v 2 && .venv/bin/python manage.py makemigrations --check --dry-run`
Expected: tests PASS; `makemigrations --check` reports "No changes detected".

- [ ] **Step 6: Commit**

```bash
git add core/models/printing.py core/migrations/
git commit -m "feat: add PrintJob.print_preset field (schema only)"
```

---

### Task 3: `Part.estimated_with_preset` field + migration

**Files:**
- Modify: `core/models/parts.py` (add `estimated_with_preset` FK to `Part`)
- Create: `core/migrations/000X_part_estimated_with_preset.py` (generated)
- Test: `core/tests/test_models.py` (extend)

**Interfaces:**
- Produces: `Part.estimated_with_preset` — `ForeignKey("OrcaPrintPreset", on_delete=models.SET_NULL, null=True, blank=True, related_name="estimated_parts")`, plus `Part.estimated_with_preset_id`.

- [ ] **Step 1: Write the failing test** (append to `core/tests/test_models.py`)

```python
class PartEstimatedWithPresetFieldTests(TestCase):
    def test_part_records_estimated_with_preset(self) -> None:
        from core.models import OrcaPrintPreset, Part

        preset = OrcaPrintPreset.objects.create(
            name="P", orca_name="P", state=OrcaPrintPreset.STATE_RESOLVED, instantiation=True
        )
        part = Part.objects.create(name="p", estimated_with_preset=preset)
        part.refresh_from_db()
        self.assertEqual(part.estimated_with_preset_id, preset.pk)

    def test_part_estimated_with_preset_defaults_to_none(self) -> None:
        from core.models import Part

        part = Part.objects.create(name="p")
        self.assertIsNone(part.estimated_with_preset_id)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python manage.py test core.tests.test_models.PartEstimatedWithPresetFieldTests -v 2`
Expected: FAIL with unexpected-keyword / missing-attribute error.

- [ ] **Step 3: Add the field to `Part` in `core/models/parts.py`**

Add after the `print_preset` field:

```python
    estimated_with_preset = models.ForeignKey(
        "OrcaPrintPreset",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="estimated_parts",
        help_text="The print preset that produced the currently stored estimate.",
    )
```

- [ ] **Step 4: Generate the migration**

Run: `.venv/bin/python manage.py makemigrations core`
Expected: creates `core/migrations/000X_part_estimated_with_preset.py` (AddField only). Do NOT hand-edit.

- [ ] **Step 5: Run test + migration gate**

Run: `.venv/bin/python manage.py test core.tests.test_models.PartEstimatedWithPresetFieldTests -v 2 && .venv/bin/python manage.py makemigrations --check --dry-run`
Expected: tests PASS; "No changes detected".

- [ ] **Step 6: Commit**

```bash
git add core/models/parts.py core/migrations/
git commit -m "feat: add Part.estimated_with_preset field (schema only)"
```

---

### Task 4: Slice jobs with `job.print_preset`

**Files:**
- Modify: `core/services/slicing_worker.py:415-462` (`_slice_job_in_background`)
- Test: `core/tests/test_estimation.py` (extend) — mock `OrcaSlicerAPIClient` and `_build_slicer_kwargs`

**Interfaces:**
- Consumes: `PrintJob.print_preset` (Task 2).
- Produces: `_slice_job_in_background` uses `job.print_preset` for `_build_slicer_kwargs(print_preset=...)` instead of `first_part.effective_print_preset`.

- [ ] **Step 1: Write the failing test** (append to `core/tests/test_estimation.py`)

```python
class SliceJobUsesJobPresetTests(TestCase):
    def test_slice_job_passes_job_print_preset_to_kwargs(self) -> None:
        from unittest import mock

        from core.models import OrcaMachineProfile, OrcaPrintPreset, Part, PrintJob, PrintJobPart
        from core.services import slicing_worker

        job_preset = OrcaPrintPreset.objects.create(
            name="JobPreset", orca_name="JobPreset", state=OrcaPrintPreset.STATE_RESOLVED, instantiation=True
        )
        part_preset = OrcaPrintPreset.objects.create(
            name="PartPreset", orca_name="PartPreset", state=OrcaPrintPreset.STATE_RESOLVED, instantiation=True
        )
        machine = OrcaMachineProfile.objects.create(
            name="M", orca_name="M", state=OrcaMachineProfile.STATE_RESOLVED, instantiation=True
        )
        part = Part.objects.create(name="p", print_preset=part_preset)
        part.stl_file.name = "stl_files/x.stl"
        part.save(update_fields=["stl_file"])
        job = PrintJob.objects.create(name="J", machine_profile=machine, print_preset=job_preset)
        PrintJobPart.objects.create(print_job=job, part=part, quantity=1)

        captured: dict = {}

        def fake_build_kwargs(machine_profile, print_preset, filament_profile):
            captured["print_preset"] = print_preset
            return {}

        with (
            mock.patch.object(slicing_worker, "_build_slicer_kwargs", side_effect=fake_build_kwargs),
            mock.patch.object(slicing_worker, "create_3mf_bundle", return_value=b"3mf"),
            mock.patch.object(slicing_worker, "OrcaSlicerAPIClient") as client_cls,
        ):
            client_cls.return_value.slice_bundle.return_value = mock.Mock(
                plates=[], total_filament_grams=None, total_filament_mm=None, total_print_time_seconds=None
            )
            slicing_worker._slice_job_in_background(job.pk)

        self.assertEqual(captured["print_preset"], job_preset)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python manage.py test core.tests.test_estimation.SliceJobUsesJobPresetTests -v 2`
Expected: FAIL — `captured["print_preset"]` is `part_preset` (current code reads `first_part.effective_print_preset`), not `job_preset`.

- [ ] **Step 3: Change the preset source in `_slice_job_in_background`**

In `core/services/slicing_worker.py`, replace:

```python
        print_preset = first_part.effective_print_preset
```

with:

```python
        # The resolved preset is pinned on the job at creation (Variant B); slice with it.
        print_preset = job.print_preset
```

(`first_part` is still used for the filament lookup above it — leave that untouched.)

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python manage.py test core.tests.test_estimation.SliceJobUsesJobPresetTests -v 2`
Expected: PASS.

- [ ] **Step 5: Lint + full suite**

Run: `ruff check . && ruff format --check . && .venv/bin/python manage.py test core`
Expected: PASS, coverage ≥ 55.

- [ ] **Step 6: Commit**

```bash
git add core/services/slicing_worker.py core/tests/test_estimation.py
git commit -m "feat: slice jobs with pinned PrintJob.print_preset"
```

---

### Task 5: Project-path job creation groups on resolved preset + pins it

**Files:**
- Modify: `core/views/print_jobs.py:298-383` (`CreateJobsFromProjectView.post`)
- Test: `core/tests/test_views_jobs.py` (extend)

**Interfaces:**
- Consumes: `Project.resolve_part_presets()` (Task 1), `PrintJob.print_preset` (Task 2), `Project.variant_progress()` (existing).
- Produces: draft jobs whose `print_preset` is the resolved group preset; grouping key is `(resolved_preset_id, spoolman_filament_id)`.

- [ ] **Step 1: Write the failing test** (append to `core/tests/test_views_jobs.py`)

```python
class CreateJobsResolvedPresetTests(TestCase):
    def setUp(self) -> None:
        from django.contrib.auth.models import User

        from core.models import OrcaPrintPreset, Part, Project, ProjectPart

        self.user = User.objects.create_user("op", password="pw")
        self._grant_add_printjob(self.user)  # existing helper pattern in this module
        self.client.force_login(self.user)

        self.project_preset = OrcaPrintPreset.objects.create(
            name="ProjPreset", orca_name="ProjPreset", state=OrcaPrintPreset.STATE_RESOLVED, instantiation=True
        )
        self.project = Project.objects.create(name="Proj", default_print_preset=self.project_preset)
        # legacy part: NO own preset — must inherit the project's default via resolution
        part = Part.objects.create(name="legacy")
        part.stl_file.name = "stl_files/legacy.stl"
        part.save(update_fields=["stl_file"])
        ProjectPart.objects.create(project=self.project, part=part, quantity=1)

    def test_created_job_pins_resolved_project_preset(self) -> None:
        from django.urls import reverse

        from core.models import PrintJob

        resp = self.client.post(reverse("core:project_create_jobs", kwargs={"pk": self.project.pk}))
        self.assertIn(resp.status_code, (302, 200))
        job = PrintJob.objects.latest("created_at")
        self.assertEqual(job.print_preset_id, self.project_preset.pk)
```

Note: reuse this module's existing role-granting helper (grep `add_printjob` / permission setup in `test_views_jobs.py`) instead of `_grant_add_printjob` if the name differs; and confirm the URL name with `grep -n "create.jobs\|create_jobs" core/urls/*.py`.

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python manage.py test core.tests.test_views_jobs.CreateJobsResolvedPresetTests -v 2`
Expected: FAIL — created job has `print_preset_id is None` (current view never sets it and groups on bare `effective_print_preset_id`, which is `None` for the legacy part).

- [ ] **Step 3: Resolve+group+pin in `CreateJobsFromProjectView.post`**

Replace the eligibility+grouping block (currently reading `row["part"].effective_print_preset_id`) so it uses the per-path resolver. Concretely, after `rows = project.variant_progress()["parts"]`:

```python
        # Resolve each part's preset in THIS project context (Variant B) and index by part pk.
        resolved_preset_by_pk: dict[int, "OrcaPrintPreset | None"] = {
            part.pk: preset for part, _mult, preset in project.resolve_part_presets()
        }

        rows = project.variant_progress()["parts"]
        eligible = [(row["part"], row["remaining"]) for row in rows if row["part"].stl_file and row["remaining"] > 0]

        if not eligible:
            messages.warning(request, "No eligible parts found (all printed or missing STL).")
            return redirect("core:project_detail", pk=project.pk)

        # Group by (resolved_preset_id, spoolman_filament_id) — one bundle = one preset.
        groups: dict[tuple[int | None, int | None], list[tuple[Part, int]]] = defaultdict(list)
        for part, remaining in eligible:
            resolved = resolved_preset_by_pk.get(part.pk)
            key = (resolved.pk if resolved is not None else None, part.spoolman_filament_id)
            groups[key].append((part, remaining))
```

Then in the per-group loop, unpack the preset id from the key and set it on the job. Change the loop header and the `PrintJob.objects.create(...)` call:

```python
        for (preset_id, filament_id), group_parts in groups.items():
            ...  # existing label-building code unchanged
            job = PrintJob.objects.create(
                name=job_name,
                status=PrintJob.STATUS_DRAFT,
                created_by=request.user,
                print_preset_id=preset_id,
            )
```

Add `OrcaPrintPreset` to the existing `from core.models import (...)` block for the type annotation if not already present (it is under quotes so a `TYPE_CHECKING`-free import is optional; simplest is to add `OrcaPrintPreset` to the import list).

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python manage.py test core.tests.test_views_jobs.CreateJobsResolvedPresetTests -v 2`
Expected: PASS.

- [ ] **Step 5: Lint + full suite**

Run: `ruff check . && ruff format --check . && .venv/bin/python manage.py test core`
Expected: PASS, coverage ≥ 55.

- [ ] **Step 6: Commit**

```bash
git add core/views/print_jobs.py core/tests/test_views_jobs.py
git commit -m "feat: project-path job creation resolves + pins preset (Variant B)"
```

---

### Task 6: Estimation resolves preset; refuses to guess when ambiguous

**Files:**
- Modify: `core/models/parts.py` (add `Part.resolve_estimation_preset`)
- Modify: `core/services/slicing_worker.py:291-397` (`_estimate_part_in_background`)
- Test: `core/tests/test_estimation.py` (extend)

**Interfaces:**
- Consumes: `resolve_part_preset` (Task 1), `Part.containing_projects()` (existing), `Part.estimated_with_preset` (Task 3).
- Produces: `Part.resolve_estimation_preset(self) -> tuple["OrcaPrintPreset | None", bool]` — returns `(preset, ambiguous)`. `ambiguous` is `True` only when there is no override AND the distinct non-null `default_print_preset`s of the containing projects number more than one. When unambiguous, `preset` is the override, or the single/agreed containing-project preset, or `None`.

- [ ] **Step 1: Write the failing test** (append to `core/tests/test_estimation.py`)

```python
class EstimationPresetResolutionTests(TestCase):
    def _preset(self, name):
        from core.models import OrcaPrintPreset

        return OrcaPrintPreset.objects.create(
            name=name, orca_name=name, state=OrcaPrintPreset.STATE_RESOLVED, instantiation=True
        )

    def test_single_project_preset_is_unambiguous(self) -> None:
        from core.models import Part, Project, ProjectPart

        preset = self._preset("Proj")
        project = Project.objects.create(name="Proj", default_print_preset=preset)
        part = Part.objects.create(name="p")
        ProjectPart.objects.create(project=project, part=part, quantity=1)
        resolved, ambiguous = part.resolve_estimation_preset()
        self.assertEqual((resolved, ambiguous), (preset, False))

    def test_override_is_unambiguous_even_with_many_projects(self) -> None:
        from core.models import Part, Project, ProjectPart

        override = self._preset("Override")
        a = Project.objects.create(name="A", default_print_preset=self._preset("PA"))
        b = Project.objects.create(name="B", default_print_preset=self._preset("PB"))
        part = Part.objects.create(name="p", print_preset=override)
        ProjectPart.objects.create(project=a, part=part, quantity=1)
        ProjectPart.objects.create(project=b, part=part, quantity=1)
        self.assertEqual(part.resolve_estimation_preset(), (override, False))

    def test_multiple_different_project_presets_is_ambiguous(self) -> None:
        from core.models import Part, Project, ProjectPart

        a = Project.objects.create(name="A", default_print_preset=self._preset("PA"))
        b = Project.objects.create(name="B", default_print_preset=self._preset("PB"))
        part = Part.objects.create(name="p")
        ProjectPart.objects.create(project=a, part=part, quantity=1)
        ProjectPart.objects.create(project=b, part=part, quantity=1)
        resolved, ambiguous = part.resolve_estimation_preset()
        self.assertTrue(ambiguous)
        self.assertIsNone(resolved)

    def test_ambiguous_part_estimation_sets_error_status(self) -> None:
        from unittest import mock

        from core.models import Part, Project, ProjectPart
        from core.services import slicing_worker

        a = Project.objects.create(name="A", default_print_preset=self._preset("PA"))
        b = Project.objects.create(name="B", default_print_preset=self._preset("PB"))
        part = Part.objects.create(name="p", estimation_status=Part.ESTIMATION_ESTIMATING)
        part.stl_file.name = "stl_files/p.stl"
        part.save(update_fields=["stl_file"])
        ProjectPart.objects.create(project=a, part=part, quantity=1)
        ProjectPart.objects.create(project=b, part=part, quantity=1)

        with mock.patch.object(slicing_worker, "OrcaSlicerAPIClient") as client_cls:
            slicing_worker._estimate_part_in_background(part.pk)
            client_cls.assert_not_called()

        part.refresh_from_db()
        self.assertEqual(part.estimation_status, Part.ESTIMATION_ERROR)
        self.assertIn("ambiguous", part.estimation_error.lower())
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python manage.py test core.tests.test_estimation.EstimationPresetResolutionTests -v 2`
Expected: FAIL — `resolve_estimation_preset` does not exist; the worker still slices with `part.effective_print_preset`.

- [ ] **Step 3: Add `Part.resolve_estimation_preset` in `core/models/parts.py`**

```python
    def resolve_estimation_preset(self) -> tuple[Optional[OrcaPrintPreset], bool]:
        """Resolve the preset to estimate this part with, flagging ambiguity.

        Variant B without a build context: an explicit override wins; otherwise the
        ``default_print_preset`` of the containing projects is used when they all agree
        (or there is exactly one). When two or more containing projects disagree and there
        is no override, the preset is **ambiguous** — estimation must not guess.

        Returns:
            ``(preset, ambiguous)``. When ``ambiguous`` is ``True`` the preset is ``None``
            and the caller should mark the estimate as needing an override rather than
            picking one. When ``ambiguous`` is ``False`` the preset may still be ``None``
            (no override and no containing-project default).
        """
        if self.print_preset_id is not None:
            return self.print_preset, False
        distinct: dict[int, OrcaPrintPreset] = {}
        for project in self.containing_projects():
            if project.default_print_preset_id is not None:
                distinct[project.default_print_preset_id] = project.default_print_preset
        if len(distinct) > 1:
            return None, True
        if len(distinct) == 1:
            return next(iter(distinct.values())), False
        return None, False
```

- [ ] **Step 4: Use it in `_estimate_part_in_background`**

In `core/services/slicing_worker.py`, replace the preset-resolution block (current code, verified on main ~line 316):

```python
        # Resolve the part's own print preset (composition is edge-based post Phase-6)
        print_preset = part.effective_print_preset
        if not print_preset:
            logger.debug("estimate_part(%s): no print preset, skipping", part_pk)
            Part.objects.filter(pk=part_pk).update(
                estimation_status=Part.ESTIMATION_NONE,
            )
            return
```

with:

```python
        # Variant B: resolve against containing projects; refuse to guess when ambiguous.
        print_preset, ambiguous = part.resolve_estimation_preset()
        if ambiguous:
            logger.info("estimate_part(%s): preset ambiguous across projects", part_pk)
            Part.objects.filter(pk=part_pk).update(
                estimation_status=Part.ESTIMATION_ERROR,
                estimation_error="Preset ambiguous across projects — set an override on the part.",
            )
            return
        if not print_preset:
            logger.debug("estimate_part(%s): no print preset, skipping", part_pk)
            Part.objects.filter(pk=part_pk).update(
                estimation_status=Part.ESTIMATION_NONE,
            )
            return
```

Then record which preset produced the estimate: in the success branch where `updated` is saved, also set `estimated_with_preset`. Change the success block:

```python
        if updated:
            part.estimation_status = Part.ESTIMATION_SUCCESS
            part.estimation_error = ""
            part.estimated_with_preset = print_preset
            updated.extend(["estimation_status", "estimation_error", "estimated_with_preset"])
            part.save(update_fields=updated)
```

Leave the `Part.objects.select_related("print_preset").get(pk=part_pk)` line at the top of `_estimate_part_in_background` **unchanged** — #44 already fixed it on `main` (it no longer references any removed FK). Do NOT touch it.

- [ ] **Step 5: Run test to verify it passes**

Run: `.venv/bin/python manage.py test core.tests.test_estimation.EstimationPresetResolutionTests -v 2`
Expected: PASS.

- [ ] **Step 6: Lint + full suite**

Run: `ruff check . && ruff format --check . && .venv/bin/python manage.py test core`
Expected: PASS, coverage ≥ 55.

- [ ] **Step 7: Commit**

```bash
git add core/models/parts.py core/services/slicing_worker.py core/tests/test_estimation.py
git commit -m "feat: estimation resolves preset per Variant B, refuses to guess when ambiguous"
```

---

### Task 7: Re-estimate entry points (GUI + API) resolve preset in context

Four re-estimate entry points added by #45 (API) and #51 (GUI dedup) currently skip a part when
`part.effective_print_preset` is falsy. Under Variant B a legacy part with no override but a
containing-project default is now estimable, so their eligibility check must use the resolver.
The two **whole-project** paths already deduplicate shared parts by pk (#51) — that dedup MUST be
preserved — and resolve each part in the **project context** (the project is the nearest
container, so it is unambiguous). The two **single-part** paths skip only when there is genuinely
no resolvable preset; an *ambiguous* part is still queued so the background worker (Task 6)
records the "ambiguous" status.

**Files:**
- Modify: `core/views/projects.py` (`ProjectReEstimateView.post`, ~line 385-403)
- Modify: `core/views/parts.py` (`PartReEstimateView.post`, ~line 396-408)
- Modify: `core/api/views.py` (`ProjectViewSet.re_estimate` ~line 122-138; `PartViewSet.estimate` ~line 309-333; plus their `@extend_schema` description text)
- Modify: `core/models/parts.py` (add `Part.is_estimable` helper)
- Test: `core/tests/test_estimation.py` and `core/tests/test_api_requirements.py` (extend — confirm the API module name with `grep -rln "re-estimate\|re_estimate\|/estimate" core/tests`)

**Interfaces:**
- Consumes: `Project.resolve_part_presets()` (Task 1), `Part.resolve_estimation_preset()` (Task 6).
- Produces: `Part.is_estimable(self) -> bool` — `True` when the part has an STL file AND resolving its estimation preset yields either a preset or the ambiguous flag (i.e. it is worth queuing so the worker can either estimate or record ambiguity). Precisely: `bool(self.stl_file) and (preset is not None or ambiguous)` where `(preset, ambiguous) = self.resolve_estimation_preset()`.

- [ ] **Step 1: Write the failing test** (append to `core/tests/test_estimation.py`)

```python
class ReEstimateEligibilityTests(TestCase):
    def _preset(self, name):
        from core.models import OrcaPrintPreset

        return OrcaPrintPreset.objects.create(
            name=name, orca_name=name, state=OrcaPrintPreset.STATE_RESOLVED, instantiation=True
        )

    def test_is_estimable_true_for_legacy_part_with_project_default(self) -> None:
        from core.models import Part, Project, ProjectPart

        project = Project.objects.create(name="Proj", default_print_preset=self._preset("P"))
        part = Part.objects.create(name="p")  # no override
        part.stl_file.name = "stl_files/p.stl"
        part.save(update_fields=["stl_file"])
        ProjectPart.objects.create(project=project, part=part, quantity=1)
        self.assertTrue(part.is_estimable())

    def test_is_estimable_false_without_any_preset(self) -> None:
        from core.models import Part

        part = Part.objects.create(name="p")
        part.stl_file.name = "stl_files/p.stl"
        part.save(update_fields=["stl_file"])
        self.assertFalse(part.is_estimable())

    def test_is_estimable_true_when_ambiguous(self) -> None:
        from core.models import Part, Project, ProjectPart

        a = Project.objects.create(name="A", default_print_preset=self._preset("PA"))
        b = Project.objects.create(name="B", default_print_preset=self._preset("PB"))
        part = Part.objects.create(name="p")
        part.stl_file.name = "stl_files/p.stl"
        part.save(update_fields=["stl_file"])
        ProjectPart.objects.create(project=a, part=part, quantity=1)
        ProjectPart.objects.create(project=b, part=part, quantity=1)
        self.assertTrue(part.is_estimable())  # queued so the worker records ambiguity

    def test_gui_project_re_estimate_queues_legacy_part(self) -> None:
        from django.contrib.auth.models import User
        from django.contrib.auth.models import Group, Permission
        from django.urls import reverse

        from core.models import Part, Project, ProjectPart

        project = Project.objects.create(name="Proj", default_print_preset=self._preset("P"))
        part = Part.objects.create(name="p")  # no override, legacy
        part.stl_file.name = "stl_files/p.stl"
        part.save(update_fields=["stl_file"])
        ProjectPart.objects.create(project=project, part=part, quantity=1)

        user = User.objects.create_user("designer", password="pw")
        perm = Permission.objects.get(codename="can_manage_projects")
        group, _ = Group.objects.get_or_create(name="Designer")
        group.permissions.add(perm)
        user.groups.add(group)
        self.client.force_login(user)

        resp = self.client.post(reverse("core:project_re_estimate", kwargs={"pk": project.pk}))
        self.assertEqual(resp.status_code, 302)
        part.refresh_from_db()
        # Previously this part was skipped (no own preset); now it is queued.
        self.assertEqual(part.estimation_status, Part.ESTIMATION_PENDING)
```

Confirm the GUI URL name (`core:project_re_estimate`) via `grep -n "re_estimate\|re-estimate" core/urls/*.py`. If the `Designer` group / permission setup differs from other tests in the module, reuse that module's existing helper instead of the inline group setup above.

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python manage.py test core.tests.test_estimation.ReEstimateEligibilityTests -v 2`
Expected: FAIL — `is_estimable` missing; the GUI project re-estimate currently skips the legacy part (`part.effective_print_preset` is `None`) so `estimation_status` stays `none`.

- [ ] **Step 3: Add `Part.is_estimable` in `core/models/parts.py`**

```python
    def is_estimable(self) -> bool:
        """Return whether this part is worth queuing for background estimation.

        Variant B: a part is estimable when it has an STL file and its estimation preset
        either resolves to a concrete preset OR is ambiguous. Ambiguous parts are still
        queued so the background worker records the "ambiguous" status rather than the
        caller silently swallowing it (see :meth:`resolve_estimation_preset`).

        Returns:
            ``True`` when the part should be queued, else ``False``.
        """
        if not self.stl_file:
            return False
        preset, ambiguous = self.resolve_estimation_preset()
        return preset is not None or ambiguous
```

- [ ] **Step 4: Route the GUI whole-project path through the resolver (preserve #51 dedup)**

In `core/views/projects.py`, `ProjectReEstimateView.post`, keep the dedup line
`parts = {p.pk: p for p, _mult in project._collect_parts_with_multiplier()}` unchanged, and
replace the per-part eligibility block:

```python
        for part in parts.values():
            if not part.stl_file:
                continue
            preset = part.effective_print_preset
            if not preset:
                continue

            Part.objects.filter(pk=part.pk).update(
```

with:

```python
        for part in parts.values():
            # Variant B: estimable if the preset resolves (or is ambiguous) in this
            # project context. Preserves the #51 shared-part dedup above.
            if not part.is_estimable():
                continue

            Part.objects.filter(pk=part.pk).update(
```

- [ ] **Step 5: Route the GUI single-part path through the resolver**

In `core/views/parts.py`, `PartReEstimateView.post`, replace:

```python
        preset = part.effective_print_preset
        if not preset:
            messages.warning(request, "No print preset configured — cannot estimate.")
            return redirect("core:part_detail", pk=part.pk)
```

with:

```python
        if not part.is_estimable():
            messages.warning(request, "No print preset configured — cannot estimate.")
            return redirect("core:part_detail", pk=part.pk)
```

(The STL-file check above this block stays as-is.)

- [ ] **Step 6: Route both API paths through the resolver + fix `@extend_schema` text**

In `core/api/views.py`, `ProjectViewSet.re_estimate`, keep the dedup
`parts = {p.pk: p for p, _mult in project._collect_parts_with_multiplier()}` and replace:

```python
        for part in parts.values():
            if not part.stl_file:
                continue
            if not part.effective_print_preset:
                continue
            Part.objects.filter(pk=part.pk).update(
```

with:

```python
        for part in parts.values():
            if not part.is_estimable():
                continue
            Part.objects.filter(pk=part.pk).update(
```

In `PartViewSet.estimate`, replace:

```python
        if not part.effective_print_preset:
            return Response(
                {"detail": "Part has no print preset configured."},
                status=status.HTTP_400_BAD_REQUEST,
            )
```

with:

```python
        preset, ambiguous = part.resolve_estimation_preset()
        if preset is None and not ambiguous:
            return Response(
                {"detail": "Part has no resolvable print preset (no override and no project default)."},
                status=status.HTTP_400_BAD_REQUEST,
            )
```

(Leave the STL 400-check above unchanged. An ambiguous part now returns 202 and the worker records the ambiguous status — that matches "queue, don't guess".)

Update the two `@extend_schema` descriptions so the auto-generated OpenAPI (drf-spectacular, #52) stays accurate:
- `ProjectViewSet.re_estimate` description: change "Parts without an STL file or a print preset are silently skipped." to "Parts without an STL file, or with no resolvable preset (no override and no project default), are silently skipped."
- `PartViewSet.estimate` description: change "Validates prerequisites (STL file + print preset present)" to "Validates prerequisites (STL file present and a resolvable — or ambiguous — preset)" and the 400 line to "Returns 400 only if there is no STL file or no resolvable preset."

- [ ] **Step 7: Run test to verify it passes**

Run: `.venv/bin/python manage.py test core.tests.test_estimation.ReEstimateEligibilityTests -v 2`
Expected: PASS.

- [ ] **Step 8: Lint + full suite (OpenAPI schema regenerates from code — no manual file)**

Run: `ruff check . && ruff format --check . && .venv/bin/python manage.py test core`
Expected: PASS, coverage ≥ 55. (drf-spectacular builds the schema from the serializers/`@extend_schema` at request time; there is no checked-in schema file to update.)

- [ ] **Step 9: Commit**

```bash
git add core/models/parts.py core/views/projects.py core/views/parts.py core/api/views.py core/tests/test_estimation.py
git commit -m "feat: re-estimate entry points (GUI + API) resolve preset in context"
```

---

### Task 8: Mandatory `default_print_preset` on project forms

**Files:**
- Modify: `core/forms/projects.py` (`ProjectForm`, `SubProjectForm`, `ProjectEditForm`)
- Test: `core/tests/test_forms.py` (extend)

**Interfaces:**
- Produces: the three project forms reject a blank `default_print_preset` with a field error.

- [ ] **Step 1: Write the failing test** (append to `core/tests/test_forms.py`)

```python
class ProjectFormPresetRequiredTests(TestCase):
    def _valid_preset(self):
        from core.models import OrcaPrintPreset

        return OrcaPrintPreset.objects.create(
            name="P", orca_name="P", state=OrcaPrintPreset.STATE_RESOLVED, instantiation=True
        )

    def test_project_form_requires_preset(self) -> None:
        from core.forms import ProjectForm

        form = ProjectForm(data={"name": "P", "description": ""})
        self.assertFalse(form.is_valid())
        self.assertIn("default_print_preset", form.errors)

    def test_subproject_form_requires_preset(self) -> None:
        from core.forms import SubProjectForm

        form = SubProjectForm(data={"name": "P", "description": "", "quantity": 1})
        self.assertFalse(form.is_valid())
        self.assertIn("default_print_preset", form.errors)

    def test_project_edit_form_requires_preset(self) -> None:
        from core.forms import ProjectEditForm

        form = ProjectEditForm(data={"name": "P", "description": "", "quantity": 1})
        self.assertFalse(form.is_valid())
        self.assertIn("default_print_preset", form.errors)

    def test_project_form_valid_with_preset(self) -> None:
        from core.forms import ProjectForm

        preset = self._valid_preset()
        form = ProjectForm(data={"name": "P", "description": "", "default_print_preset": preset.pk})
        self.assertTrue(form.is_valid(), form.errors)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python manage.py test core.tests.test_forms.ProjectFormPresetRequiredTests -v 2`
Expected: FAIL — the field is currently optional (model FK is `blank=True`), so the required-error tests fail.

- [ ] **Step 3: Make the field required in all three forms**

In `core/forms/projects.py`, add an `__init__` to each of `ProjectForm`, `SubProjectForm`, `ProjectEditForm` that flips the field required. For `ProjectForm`:

```python
    def __init__(self, *args, **kwargs) -> None:
        """Make ``default_print_preset`` mandatory (Variant B: preset resolution needs it)."""
        super().__init__(*args, **kwargs)
        self.fields["default_print_preset"].required = True
```

Add the identical `__init__` to `SubProjectForm`. For `ProjectEditForm`, extend its existing `__init__` if present, else add the same one (it currently has no `__init__`, only `clean`). Keep `ProjectEditForm.clean` as-is.

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python manage.py test core.tests.test_forms.ProjectFormPresetRequiredTests -v 2`
Expected: PASS.

- [ ] **Step 5: Lint + full suite (watch for fixture regressions)**

Run: `ruff check . && ruff format --check . && .venv/bin/python manage.py test core`
Expected: PASS. If existing view tests that submit a project form without a preset now fail, update those tests to include a valid `default_print_preset` PK (the mandatory rule is intended). Do NOT relax the requirement to keep them green.

- [ ] **Step 6: Commit**

```bash
git add core/forms/projects.py core/tests/test_forms.py
git commit -m "feat: require default_print_preset on project forms"
```

---

### Task 9: Mandatory `default_print_preset` on `ProjectSerializer`

**Files:**
- Modify: `core/api/serializers.py:31-48` (`ProjectSerializer`)
- Test: `core/tests/test_api_projects.py` (extend)

**Interfaces:**
- Produces: `ProjectSerializer` rejects create/update with a missing/blank `default_print_preset` (`400`).

- [ ] **Step 1: Write the failing test** (append to `core/tests/test_api_projects.py`, following that module's existing auth/token setup pattern)

```python
class ProjectSerializerPresetRequiredTests(TestCase):
    def test_create_project_without_preset_is_rejected(self) -> None:
        from core.api.serializers import ProjectSerializer

        serializer = ProjectSerializer(data={"name": "P", "description": ""})
        self.assertFalse(serializer.is_valid())
        self.assertIn("default_print_preset", serializer.errors)

    def test_create_project_with_preset_is_valid(self) -> None:
        from core.api.serializers import ProjectSerializer
        from core.models import OrcaPrintPreset

        preset = OrcaPrintPreset.objects.create(
            name="P", orca_name="P", state=OrcaPrintPreset.STATE_RESOLVED, instantiation=True
        )
        serializer = ProjectSerializer(data={"name": "P", "description": "", "default_print_preset": preset.pk})
        self.assertTrue(serializer.is_valid(), serializer.errors)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python manage.py test core.tests.test_api_projects.ProjectSerializerPresetRequiredTests -v 2`
Expected: FAIL — serializer accepts a missing preset (model FK is `blank=True`, so DRF infers `required=False`).

- [ ] **Step 3: Make the serializer field required**

In `core/api/serializers.py`, add an explicit field on `ProjectSerializer`:

```python
class ProjectSerializer(serializers.ModelSerializer):
    """Serialize a project's scalar fields.

    Sub-project composition is managed via the ``components`` edge endpoint, not the
    legacy ``parent``/``quantity`` fields, so those are deliberately not writable here.
    ``default_print_preset`` is required (Variant B preset resolution depends on it).
    """

    default_print_preset = serializers.PrimaryKeyRelatedField(
        queryset=OrcaPrintPreset.objects.all(),
        required=True,
    )

    class Meta:
        model = Project
        fields = [
            "id",
            "name",
            "description",
            "default_print_preset",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "created_at", "updated_at"]
```

`OrcaPrintPreset` is already imported in this module.

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python manage.py test core.tests.test_api_projects.ProjectSerializerPresetRequiredTests -v 2`
Expected: PASS.

- [ ] **Step 5: Lint + full suite**

Run: `ruff check . && ruff format --check . && .venv/bin/python manage.py test core`
Expected: PASS. Update any existing API-project test that POSTs a project without a preset to include a valid `default_print_preset` PK.

- [ ] **Step 6: Commit**

```bash
git add core/api/serializers.py core/tests/test_api_projects.py
git commit -m "feat: require default_print_preset on ProjectSerializer"
```

---

### Task 10: Part-path job creation — preset resolution + dropdown

**Files:**
- Modify: `core/models/parts.py` (add `Part.resolve_job_preset_candidates`)
- Modify: `core/views/parts.py:133-210` (`PartDetailView.get_context_data`)
- Modify: `core/views/print_jobs.py:178-276` (`AddPartToJobView.post`)
- Modify: `core/templates/core/part_detail.html:215-245` (add preset dropdown)
- Test: `core/tests/test_views_jobs.py` (extend)

**Interfaces:**
- Consumes: `resolve_part_preset` (Task 1), `Part.containing_projects()`, `PrintJob.print_preset` (Task 2).
- Produces: `Part.resolve_job_preset_candidates(self) -> tuple["OrcaPrintPreset | None", list[OrcaPrintPreset]]` — returns `(auto_preset, choices)`. When the preset is unambiguous (override, one project, or all-equal), `auto_preset` is that preset and `choices` is empty. When multiple containing projects disagree and there is no override, `auto_preset` is `None` and `choices` is the distinct containing-project presets (for the dropdown).
- Produces: `AddPartToJobView` reads a `print_preset` POST param when `choices` is non-empty; pins `PrintJob.print_preset` on a newly created job.

- [ ] **Step 1: Write the failing test** (append to `core/tests/test_views_jobs.py`)

```python
class AddPartToJobPresetTests(TestCase):
    def _preset(self, name):
        from core.models import OrcaPrintPreset

        return OrcaPrintPreset.objects.create(
            name=name, orca_name=name, state=OrcaPrintPreset.STATE_RESOLVED, instantiation=True
        )

    def test_candidates_single_project_auto(self) -> None:
        from core.models import Part, Project, ProjectPart

        preset = self._preset("P")
        project = Project.objects.create(name="Proj", default_print_preset=preset)
        part = Part.objects.create(name="p")
        ProjectPart.objects.create(project=project, part=part, quantity=1)
        auto, choices = part.resolve_job_preset_candidates()
        self.assertEqual(auto, preset)
        self.assertEqual(choices, [])

    def test_candidates_multiple_different_offers_choices(self) -> None:
        from core.models import Part, Project, ProjectPart

        a = Project.objects.create(name="A", default_print_preset=self._preset("PA"))
        b = Project.objects.create(name="B", default_print_preset=self._preset("PB"))
        part = Part.objects.create(name="p")
        ProjectPart.objects.create(project=a, part=part, quantity=1)
        ProjectPart.objects.create(project=b, part=part, quantity=1)
        auto, choices = part.resolve_job_preset_candidates()
        self.assertIsNone(auto)
        self.assertEqual({c.name for c in choices}, {"PA", "PB"})

    def test_add_part_new_job_pins_chosen_preset(self) -> None:
        from django.contrib.auth.models import User
        from django.urls import reverse

        from core.models import Part, PrintJob, Project, ProjectPart

        pa = self._preset("PA")
        pb = self._preset("PB")
        a = Project.objects.create(name="A", default_print_preset=pa)
        b = Project.objects.create(name="B", default_print_preset=pb)
        part = Part.objects.create(name="p")
        part.stl_file.name = "stl_files/p.stl"
        part.save(update_fields=["stl_file"])
        ProjectPart.objects.create(project=a, part=part, quantity=1)
        ProjectPart.objects.create(project=b, part=part, quantity=1)

        user = User.objects.create_user("op2", password="pw")
        self._grant_add_printjob(user)  # reuse this module's permission helper
        self.client.force_login(user)

        resp = self.client.post(
            reverse("core:add_part_to_job", kwargs={"part_pk": part.pk}),
            {"job": "", "quantity": 1, "print_preset": pb.pk},
        )
        self.assertEqual(resp.status_code, 302)
        job = PrintJob.objects.latest("created_at")
        self.assertEqual(job.print_preset_id, pb.pk)
```

Confirm the URL kwarg name (`part_pk`) and the permission-granting helper name via `grep -n "add_part_to_job\|add_printjob" core/urls/*.py core/tests/test_views_jobs.py`.

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python manage.py test core.tests.test_views_jobs.AddPartToJobPresetTests -v 2`
Expected: FAIL — `resolve_job_preset_candidates` missing; `AddPartToJobView` does not set `print_preset`.

- [ ] **Step 3: Add `Part.resolve_job_preset_candidates` in `core/models/parts.py`**

```python
    def resolve_job_preset_candidates(self) -> tuple[Optional[OrcaPrintPreset], list[OrcaPrintPreset]]:
        """Resolve the preset for the standalone (part-path) "Add to Job" flow.

        Variant B without a project build context, with a UI escape hatch: an explicit
        override, a single containing project, or several projects that all agree yield an
        unambiguous ``auto`` preset (and no choices). Several containing projects that
        disagree and no override yield ``auto=None`` and the distinct containing-project
        presets as ``choices`` so the UI can offer a dropdown.

        Returns:
            ``(auto_preset, choices)``. When ``choices`` is non-empty the caller must ask
            the user to pick one; otherwise ``auto_preset`` (possibly ``None``) is used.
        """
        if self.print_preset_id is not None:
            return self.print_preset, []
        distinct: dict[int, OrcaPrintPreset] = {}
        for project in self.containing_projects():
            if project.default_print_preset_id is not None:
                distinct[project.default_print_preset_id] = project.default_print_preset
        if len(distinct) > 1:
            return None, list(distinct.values())
        if len(distinct) == 1:
            return next(iter(distinct.values())), []
        return None, []
```

- [ ] **Step 4: Expose candidates in `PartDetailView.get_context_data`**

In `core/views/parts.py`, inside `PartDetailView.get_context_data`, add before `return context`:

```python
        auto_preset, preset_choices = part.resolve_job_preset_candidates()
        context["job_preset_auto"] = auto_preset
        context["job_preset_choices"] = preset_choices
```

- [ ] **Step 5: Set the preset in `AddPartToJobView.post`**

In `core/views/print_jobs.py`, in `AddPartToJobView.post`, resolve the preset for the new-job branch. After `quantity = form.cleaned_data["quantity"]` and before the `if not job:` block, add:

```python
        # Resolve the preset for a *new* job (Variant B part path). An explicit dropdown
        # choice (multi-project, ambiguous case) overrides the auto resolution.
        auto_preset, preset_choices = part.resolve_job_preset_candidates()
        chosen_preset_id = request.POST.get("print_preset") or None
        if preset_choices and chosen_preset_id is None:
            messages.error(request, "This part is in projects with different presets — choose a preset.")
            return redirect("core:part_detail", pk=part.pk)
        new_job_preset_id = chosen_preset_id if chosen_preset_id is not None else (
            auto_preset.pk if auto_preset is not None else None
        )
```

Then in the `if not job:` block, pass the preset:

```python
        if not job:
            job = PrintJob.objects.create(
                name=f"Job with {part.name}",
                status=PrintJob.STATUS_DRAFT,
                created_by=request.user,
                print_preset_id=new_job_preset_id,
            )
            messages.info(request, f"New draft job '{job}' created.")
```

Leave the existing preset/filament compatibility check unchanged.

- [ ] **Step 6: Add the dropdown to `core/templates/core/part_detail.html`**

Inside the "Add to Print Job" `<form>` (after the Quantity block, before the Build target block), add:

```html
          {% if job_preset_choices %}
          <div class="mb-2">
            <label for="id_print_preset" class="form-label form-label-sm">Print preset</label>
            <select name="print_preset" id="id_print_preset" class="form-select form-select-sm" required>
              <option value="">— Choose a preset —</option>
              {% for preset in job_preset_choices %}
              <option value="{{ preset.pk }}">{{ preset.name }}</option>
              {% endfor %}
            </select>
            <div class="form-text">This part is used in projects with different presets — pick one.</div>
          </div>
          {% endif %}
```

- [ ] **Step 7: Run test to verify it passes**

Run: `.venv/bin/python manage.py test core.tests.test_views_jobs.AddPartToJobPresetTests -v 2`
Expected: PASS.

- [ ] **Step 8: Lint + full suite**

Run: `ruff check . && ruff format --check . && .venv/bin/python manage.py test core`
Expected: PASS, coverage ≥ 55.

- [ ] **Step 9: Commit**

```bash
git add core/models/parts.py core/views/parts.py core/views/print_jobs.py core/templates/core/part_detail.html core/tests/test_views_jobs.py
git commit -m "feat: part-path job creation resolves preset with dropdown fallback"
```

---

### Task 11: Job-detail effective-preset display reads `job.print_preset`

**Files:**
- Modify: `core/views/print_jobs.py:87-129` (`PrintJobDetailView.get_context_data`)
- Test: `core/tests/test_views_jobs.py` (extend)

**Interfaces:**
- Consumes: `PrintJob.print_preset` (Task 2).
- Produces: `context["effective_print_preset"]` is `job.print_preset` when set, falling back to the first part's `effective_print_preset` only when the job has no pinned preset (legacy jobs).

- [ ] **Step 1: Write the failing test** (append to `core/tests/test_views_jobs.py`)

```python
class JobDetailEffectivePresetTests(TestCase):
    def test_detail_uses_job_pinned_preset(self) -> None:
        from django.contrib.auth.models import User
        from django.urls import reverse

        from core.models import OrcaPrintPreset, Part, PrintJob, PrintJobPart

        job_preset = OrcaPrintPreset.objects.create(
            name="JobPreset", orca_name="JobPreset", state=OrcaPrintPreset.STATE_RESOLVED, instantiation=True
        )
        part_preset = OrcaPrintPreset.objects.create(
            name="PartPreset", orca_name="PartPreset", state=OrcaPrintPreset.STATE_RESOLVED, instantiation=True
        )
        part = Part.objects.create(name="p", print_preset=part_preset)
        job = PrintJob.objects.create(name="J", print_preset=job_preset)
        PrintJobPart.objects.create(print_job=job, part=part, quantity=1)

        user = User.objects.create_user("viewer", password="pw")
        self.client.force_login(user)
        resp = self.client.get(reverse("core:printjob_detail", kwargs={"pk": job.pk}))
        self.assertEqual(resp.context["effective_print_preset"], job_preset)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python manage.py test core.tests.test_views_jobs.JobDetailEffectivePresetTests -v 2`
Expected: FAIL — context shows `part_preset` (view reads `first_jp.part.effective_print_preset`).

- [ ] **Step 3: Prefer the pinned preset in `PrintJobDetailView.get_context_data`**

In `core/views/print_jobs.py`, replace:

```python
        first_jp = job.job_parts.select_related("part__print_preset").first()
        if first_jp:
            context["effective_print_preset"] = first_jp.part.effective_print_preset
```

with:

```python
        first_jp = job.job_parts.select_related("part__print_preset").first()
        if first_jp:
            # Prefer the preset pinned on the job (Variant B); fall back to the first
            # part's own preset only for legacy jobs created before pinning existed.
            context["effective_print_preset"] = job.print_preset or first_jp.part.effective_print_preset
```

Leave the filament-resolution block below it unchanged.

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python manage.py test core.tests.test_views_jobs.JobDetailEffectivePresetTests -v 2`
Expected: PASS.

- [ ] **Step 5: Lint + full suite**

Run: `ruff check . && ruff format --check . && .venv/bin/python manage.py test core`
Expected: PASS, coverage ≥ 55.

- [ ] **Step 6: Commit**

```bash
git add core/views/print_jobs.py core/tests/test_views_jobs.py
git commit -m "feat: job detail shows pinned print preset"
```

---

### Task 12: Docs — record the shipped behavior

**Files:**
- Modify: `docs/user-guide/` (the print-job / project page that documents preset behavior — locate with `grep -rl "print preset\|default_print_preset" docs/`)
- Modify: `docs/superpowers/specs/2026-09-22-project-preset-resolution-design.md` (flip the status line to "implemented")

**Interfaces:** none (documentation only).

- [ ] **Step 1: Locate the doc page**

Run: `grep -rln "preset" docs/user-guide docs/index.md docs/quick-start.md`
Pick the page that covers projects/print jobs (create it under `docs/user-guide/` only if none exists).

- [ ] **Step 2: Add a short "Print presets" subsection**

Document, in prose (no code): every project now requires a default print preset; a part uses its own preset override if set, otherwise the preset of the project it is built under; a project build produces one job per distinct preset+filament; and adding a shared part (used in projects with different presets) to a job asks you to pick the preset. Mention that estimation of such an ambiguous part reports "Preset ambiguous — set an override". Note that the REST API's `POST /projects/{id}/re-estimate/` and `POST /parts/{id}/estimate/` follow the same resolution, and that the OpenAPI schema (served by drf-spectacular at the existing schema/Swagger endpoints, #52) is regenerated automatically from the updated serializer/`@extend_schema` — there is no hand-written API reference file to edit here.

- [ ] **Step 3: Flip the spec status line**

In `docs/superpowers/specs/2026-09-22-project-preset-resolution-design.md`, change the top status note from "design locked" to note it is now implemented (leave the design body unchanged).

- [ ] **Step 4: Verify docs build/lint is unaffected**

Run: `ruff format --check .`
Expected: PASS (ruff 0.15.20 formats Markdown code blocks — none were added here, so this stays clean).

- [ ] **Step 5: Commit**

```bash
git add docs/
git commit -m "docs: document mandatory project preset + Variant B resolution"
```

---

## Self-Review

**1. Spec coverage** (each spec section → task):

- Problem (None-preset slicing/estimation) → Tasks 4, 6 (slicing/estimation now use resolved preset).
- No project→project fallback → Task 1 (resolver uses nearest-on-path, not ancestor walk).
- Top-down per-build-path resolution (Variant B) → Task 1 (`resolve_part_presets` + `resolve_part_preset`).
- `default_print_preset` mandatory at form/serializer level → Tasks 8, 9.
- No migration hardcoding preset 4 / already-done data step omitted → honored in Global Constraints; Tasks 2 & 3 are schema-only AddFields, no data step. ✓
- Slicing: group by resolved (preset, filament), new `PrintJob.print_preset`, slice with it → Tasks 2, 5, 4.
- Job entry points: project path (Task 5), part path with dropdown (Task 10). ✓
- Estimation Variant b, ambiguous → status message, `Part.estimated_with_preset` → Tasks 3, 6.
- **All five estimation entry points** (background worker, GUI single, GUI project, API single, API project) → background worker Task 6; the four re-estimate views (#45 API + #51 GUI dedup) Task 7 — the #51 shared-part dedup is explicitly preserved, per-project paths resolve in project context, single-part paths queue-not-guess on ambiguity.
- Per-project requirements/build-progress resolve in context → `resolve_part_presets` (Task 1) available; re-estimate project paths use in-context eligibility (Task 7); existing `variant_progress` part counts unaffected.
- Affected places (models/services/views/forms/serializers/templates/api/migrations/tests) → Tasks 1–11; docs → Task 12.
- Job-detail effective display → Task 11.

No spec requirement is left without a task.

**2. Placeholder scan:** No "TBD/TODO/handle edge cases/similar to Task N/write tests for the above". Every code step shows the actual code; every test step shows real assertions. Several steps ask the executor to `grep` to confirm an existing URL name / permission-helper name / test-module name — these are verification actions with the exact grep command given, not placeholders for missing content.

**3. Type consistency:**

- `resolve_part_preset(part, nearest_project) -> Optional[OrcaPrintPreset]` — defined Task 1, consumed by `resolve_part_presets` (Task 1). Name stable.
- `Project.resolve_part_presets() -> list[tuple[Part, int, Optional[OrcaPrintPreset]]]` — defined Task 1, consumed Task 5. Tuple shape `(part, effective_count, resolved_preset)` used consistently.
- `PrintJob.print_preset` / `.print_preset_id` — defined Task 2, consumed Tasks 4, 5, 10, 11. ✓
- `Part.estimated_with_preset` / `.estimated_with_preset_id` — defined Task 3, consumed Task 6. ✓
- `Part.resolve_estimation_preset() -> tuple[Optional[OrcaPrintPreset], bool]` — defined Task 6, consumed Task 6 (worker), Task 7 (`is_estimable` + API single-part). ✓
- `Part.is_estimable() -> bool` — defined Task 7, consumed Task 7 (all four re-estimate views). ✓
- `Part.resolve_job_preset_candidates() -> tuple[Optional[OrcaPrintPreset], list[OrcaPrintPreset]]` — defined Task 10, consumed Task 10 (view + context). ✓
- `OrcaPrintPreset` constructor fixture args (`name`, `orca_name`, `state=STATE_RESOLVED`, `instantiation=True`) used identically across all test tasks. ✓

**Revision notes vs. the first draft (stale baseline):**
- Removed the "fold the stale `select_related` fix into Task 6" step — #44 already fixed it on `main` (`_estimate_part_in_background` already reads `select_related("print_preset")`); Task 6 now only adds the resolver + `estimated_with_preset` write and explicitly says to leave that line alone.
- Added **Task 7** to cover the #45 API re-estimate endpoints (`ProjectViewSet.re_estimate`, `PartViewSet.estimate`) and the #51 GUI dedup path (`ProjectReEstimateView`) plus the GUI single-part path (`PartReEstimateView`) — all four now route through the resolver, the #51 dedup is preserved, and the drf-spectacular (#52) `@extend_schema` descriptions are updated to match (schema is auto-generated; no hand-written file).
- Renumbered old Tasks 7–11 → 8–12; docs task (12) now notes the OpenAPI schema is auto-generated.
- Re-verified all file paths/line ranges against `main` @ `9715158`.

Gap found + fixed during this revision: the estimation-preset write (`estimated_with_preset`) is retained in Task 6 so Task 3's field is consumed; and `Part.is_estimable` (Task 7) is defined in terms of `resolve_estimation_preset` (Task 6) so the ambiguous-but-queue behavior is consistent across the worker and all four re-estimate entry points (no path silently drops an ambiguous part).
