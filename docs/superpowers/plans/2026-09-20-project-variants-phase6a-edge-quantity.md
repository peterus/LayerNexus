# Project Variants — Phase 6a (Contract prep: edge quantity authoritative + per-assembly remaining)

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development (or executing-plans). Steps use checkbox (`- [ ]`).
>
> **CRITICAL Kontor rule:** run every test/verify command in the FOREGROUND (one blocking Bash call, generous timeout). NEVER `run_in_background`, `Monitor`, or `gh ... --watch` to wait — they don't reliably re-invoke you here and you will stall. After push + PR, send the parent ONE `status:info` message and STOP; the coordinator drives CI/Copilot/merge (and pokes you for a single foreground fix-turn if needed).

**Goal:** Make the **composition-edge quantity authoritative** for "how many of a part in a
module" and make "remaining / what's still missing" **per assembly (variant)** — so a shared part
can have different counts in different modules and progress is tracked per variant. This migrates
the SEMANTICS off `Part.quantity`'s global meaning while KEEPING the old fields + dual-write in
place (so nothing breaks). The actual field removal is Phase 6b.

**Architecture:** Contract-prep step. Today `_expand_parts_relative` deliberately does NOT fold the
`ProjectPart.quantity` edge (it uses `Part.quantity` as the leaf count, applied by callers). This
phase flips that: fold the edge quantity into the multiplier and drop the `* part.quantity` in the
callers. Because dual-write keeps `ProjectPart.quantity == Part.quantity`, the flip is
**behavior-preserving** for all existing single-count fixtures (existing tests stay green); the new
capability is that different edge quantities now count independently. "Remaining" moves to
per-assembly using Phase-4 `printed_quantity_for`.

**Tech Stack:** Django 6.0, Python 3.13, SQLite, Bootstrap 5.3, ruff 0.15.20, Django test runner.

**Spec:** `docs/superpowers/specs/2026-09-20-project-variants-dag-composition-design.md`

## Global Constraints

- Base: `main` at/after `365daff` (Phase 5 + Parts-search + Build-API merged). Branch from current main.
- Double quotes; f-strings; type hints; Google-style docstrings. CBV only; Django ORM.
- **No migration** in 6a (fields stay; dual-write stays). `makemigrations --check` clean.
- **RBAC:** touched write views keep their existing mixins.
- **File ownership:** `core/models/projects.py`, `core/models/parts.py`, `core/views/print_jobs.py`,
  `core/views/dashboard.py` (only if it uses part-level remaining), templates
  `core/templates/core/part_detail.html`, `project_detail.html`, and tests. Do NOT remove any model
  field, the dual-write, or add a migration (that's 6b). Do NOT touch `composition.py`.
- Tests: `.venv/bin/python manage.py test core` (coverage ≥ 55). Lint: `ruff check . && ruff format --check .`.
  Fresh worktree: `.venv` + `pip install -r requirements.txt` (incl. drf) + coverage; DEBUG=1 +
  DJANGO_SECRET_KEY + collectstatic first.

## Merged interfaces consumed

- `_expand_parts_relative(_path, _memo)` / `_collect_parts_with_multiplier(multiplier=1)` (projects.py).
- `ProjectPart.quantity` (edge), `Part.printed_quantity` (global, job-based), `Part.printed_quantity_for(assembly)` (Phase 4).
- `Project.variant_progress()` (Phase 4) — already computes per-part needed/printed/remaining for THIS assembly.

---

### Task 1: Fold edge quantity into aggregation (edge quantity authoritative)

**Files:** `core/models/projects.py`, `core/tests/test_aggregation_dag.py` (extend)

**Interfaces:**
- `_expand_parts_relative` folds `ProjectPart.quantity` for direct parts.
- `_collect_parts_with_multiplier` now returns `(part, effective_count)` where
  `effective_count = ProjectPart.quantity × product(ProjectComponent edge quantities)`.
- Callers stop multiplying by `part.quantity`.

- [ ] **Step 1: Failing test** — the new capability: same part, different edge quantities in two
  modules of one assembly, must sum by edge (not by a single `part.quantity`).

```python
class EdgeQuantityAuthoritativeTests(TestCase):
    def test_same_part_different_edge_quantities_sum_independently(self):
        from core.models import Part, Project, ProjectComponent, ProjectPart
        truck = Project.objects.create(name="Truck")
        cabin = Project.objects.create(name="Cabin")
        frame = Project.objects.create(name="Frame")
        home = Project.objects.create(name="home")
        bolt = Part.objects.create(project=home, name="bolt", quantity=1)  # Part.quantity now ignored by aggregation
        ProjectComponent.objects.create(parent_project=truck, child_project=cabin, quantity=1)
        ProjectComponent.objects.create(parent_project=truck, child_project=frame, quantity=1)
        ProjectPart.objects.create(project=cabin, part=bolt, quantity=4)   # 4 in cabin
        ProjectPart.objects.create(project=frame, part=bolt, quantity=10)  # 10 in frame
        # total bolts in truck = 4 + 10 = 14 (edge-authoritative)
        self.assertEqual(truck.total_parts_count, 14)
```

- [ ] **Step 2: Run → fail** (current code yields `1×1 + 1×1 = 2` because it ignores edge qty and
  uses part.quantity=1).

- [ ] **Step 3: Implement the flip.** In `_expand_parts_relative`, change the direct-parts line:

```python
        rel: list[tuple[Part, int]] = [(link.part, link.quantity) for link in self.part_links.all()]
```

(from `, 1)` to `, link.quantity)`). Then update ALL callers in `projects.py` that multiply by
`part.quantity` to stop doing so (the multiplier is now the full effective count):
- `total_parts_count` (~442): `sum(mult for _p, mult in self._collect_parts_with_multiplier())`.
- `total_filament_grams` (~586) / `total_filament_meters`: `sum((p.filament_used_grams or 0) * mult ...)`.
- `variant_progress` (~480): `needed_by_part[part.pk] += mult` (drop `* part.quantity`).
- `filament_requirements`: total uses `* mult`; **remaining** must switch to per-assembly (Task 2) —
  for now compute remaining via `min(printed_for, needed)` per Task 2's helper (do not use
  `part.remaining_quantity`).
Update the docstrings that say "Part.quantity is the leaf count" to "edge quantity is the leaf count".

- [ ] **Step 4: Run the DAG + full aggregation tests.** Existing tests stay green (dual-write keeps
  edge==part.quantity for single-count fixtures); the new test passes. If an existing test set a
  `ProjectPart.quantity` different from `part.quantity`, update its expectation to the edge value.
- [ ] **Step 5: Commit.**

---

### Task 2: Per-assembly "remaining" (replace global Part.remaining_quantity in aggregation)

**Files:** `core/models/projects.py`, `core/models/parts.py`, `core/tests/test_print_attribution.py` (extend)

**Interfaces:**
- `Project.part_requirements()` (or extend `variant_progress`) already yields per-part
  `needed`/`printed`/`remaining` for THIS assembly (Phase 4). Ensure `needed` uses the Task-1
  effective count and `printed` uses `printed_quantity_for(self)`.
- `filament_requirements` remaining fields use this per-assembly remaining instead of
  `part.remaining_quantity`.
- `aggregated_status` "complete/in_progress" logic: replace `part.is_complete` (global) with a
  per-assembly notion: a part is "complete for THIS project" when `printed_for(self) >= needed`.
  Iterate `variant_progress()["parts"]` rows instead of raw parts for the printed/complete signals
  (keep the estimation-status signals from the parts themselves).

- [ ] **Step 1: Failing test** — a project whose only part is fully printed *for it* is `complete`;
  if printed was attributed to a DIFFERENT assembly, it is NOT complete.
- [ ] **Step 2: Run → fail.**
- [ ] **Step 3: Implement.** Add/extend a `variant_progress()`-based path; rewrite `aggregated_status`
  and `filament_requirements` remaining to use per-assembly needed/printed. Keep `total_parts_count`
  (Task 1) and `printed_parts_count` (global job-based) as they are unless a test shows a conflict.
- [ ] **Step 4: Run → pass. Commit.**

---

### Task 3: Print-job "create from project" uses per-assembly remaining + target_assembly

**Files:** `core/views/print_jobs.py`, `core/tests/test_views_print_jobs.py` (or the existing print-job test module)

**Interfaces:** `CreateJobsFromProjectView` (print_jobs.py ~303) is project-scoped — use per-project
remaining and attribute the created `PrintJobPart`s to that project.

- [ ] **Step 1: Failing test** — creating jobs from project P: a part needed 10 in P with 3 printed
  *for P* yields a job-part with `quantity == 7` and `target_assembly == P`; a part already fully
  printed for P is skipped.
- [ ] **Step 2: Run → fail.**
- [ ] **Step 3: Implement.** Replace `project.parts.select_related("project")` +
  `part.remaining_quantity` with the per-project requirements: iterate the Task-2 per-part rows
  (`{part, needed, printed, remaining}`) for `project`; `eligible = rows where part.stl_file and
  remaining > 0`; create `PrintJobPart(print_job=job, part=part, quantity=remaining,
  target_assembly=project)`. Grouping by `(effective_print_preset_id, spoolman_filament_id)` stays.
- [ ] **Step 4: Run → pass. Commit.**

---

### Task 4: Templates — per-variant quantity/remaining display

**Files:** `core/templates/core/part_detail.html`, `core/templates/core/project_detail.html`, tests via view render assertions

- [ ] **Step 1:** `project_detail.html`: the per-part rows already have a project context — show
  `needed`/`printed`/`remaining` from the Task-2 per-part rows (the project passes them in context).
  `part_detail.html`: replace the global `part.quantity` / `part.remaining_quantity` / `is_complete`
  display with a **"Used in"** breakdown (each assembly with its needed/printed) — the part's
  `containing_projects()` each expose `variant_progress()` rows filtered to this part. Keep the
  "quantity to print" input defaulting to a sensible value (e.g. 1) rather than global remaining.
- [ ] **Step 2:** Add view render assertions (project detail shows per-part remaining; part detail
  shows a used-in section) in the existing view tests.
- [ ] **Step 3: Run affected + FULL suite (foreground).**
```bash
.venv/bin/python manage.py test core 2>&1 | tail -25
ruff check . && ruff format --check . && .venv/bin/python manage.py makemigrations --check --dry-run && .venv/bin/python manage.py check --fail-level WARNING
```
- [ ] **Step 4: Commit.**

---

### Task 5: PR

- [ ] push + `gh pr create` (title "refactor: edge quantity authoritative + per-assembly remaining
  — Project Variants Phase 6a"; body: aggregation folds edge quantity, remaining/status/print-flow
  per-assembly, templates per-variant; fields + dual-write UNCHANGED (removed in 6b); no migration).
  Write PR# to memory `feature/project-variants/phase6a-progress`, send parent ONE `status:info`, STOP.

---

## Self-Review

**Scope:** edge quantity authoritative (Task 1); per-assembly remaining/status (Task 2); print flow
per-assembly + target_assembly (Task 3); templates per-variant (Task 4). Fields/dual-write/migration
NOT touched → 6b. ✅

**Behavior preservation:** dual-write keeps `ProjectPart.quantity == Part.quantity`, so the Task-1
flip is a no-op for all single-count fixtures → existing aggregation tests stay green; only the new
different-edge-quantity test exercises the new behavior. Per-assembly remaining DOES change behavior
for prints not attributed to an assembly (old jobs with `target_assembly=null` no longer count toward
a project's remaining) — this is the intended Phase-4 attribution semantics; update affected
print/aggregation tests accordingly and note it in the PR body. ✅

**Placeholder scan:** none — the aggregation flip has exact code; Tasks 2–4 give precise directives
against real methods/templates the implementer reads. ✅

**Kontor discipline:** foreground waits; PR then hand off. ✅
