# Tree Rollback Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the n:m project/part composition graph with a strict tree (one project per part, one parent per sub-project), with reuse through deep copy and subtree deletion.

**Architecture:** `Part.project` / `Part.quantity` and `Project.parent` / `Project.quantity` become the only structure again. One migration set backfills them from the edge tables and then drops the edge tables. All aggregation walks `subprojects` with a quantity multiplier. Print presets resolve bottom-up. `Project.deep_copy` / `Part.deep_copy` copy content (including new files) but never history. Deleting a project deletes its subtree, and is blocked while unfinished print jobs touch it.

**Tech Stack:** Django 6.1, DRF + drf-spectacular, SQLite, Bootstrap 5.3 templates, uv, ruff 0.15.20, Django test runner.

**Spec:** `docs/superpowers/specs/2026-10-28-tree-rollback-design.md`

## Global Constraints

- RBAC: every write view uses a role mixin from `core/mixins.py`. Every copy/add/delete project view uses `ProjectManageMixin`. Print-job views keep `RoleRequiredMixin` + `permission_required`.
- Code style: double quotes only, f-strings, type hints on every signature, Google-style docstrings, CBVs only, ORM only (no raw SQL), no inline CSS/JS in new template code.
- Migrations: generate them only with `uv run python manage.py makemigrations --noinput` (headless: there is no TTY for the questioner). The one exception is the single RunPython data step, created with `makemigrations --empty`. Never hand-edit generated schema operations.
- Ruff is pinned to `0.15.20`. Always run it as `uv run ruff`.
- Coverage gate: `fail_under = 55`.
- Tests: `export DEBUG=1`, plus `uv run python manage.py collectstatic --noinput` once per fresh worktree.
- Build API `/api/v1/` may break. No backwards compatibility is needed.
- Copy rule: **copy content, never history.** Do not copy `PrintJobPart`/jobs, `HardwareShoppingItem`, or `FileVersion`. The `HardwarePart` catalog is shared on purpose.
- "Unfinished" job = `PrintJob.status` not in `{completed, failed, cancelled}`.
- Copy-name default: `f"{name} (copy)"` (the UI language is English).
- Prod is in active use. Every PR must leave CI green and a fully migrated schema.
- Full CI-equivalent gate, run before opening each PR:
  ```bash
  export DEBUG=1
  uv lock --check && uv run ruff check . && uv run ruff format --check .
  uv run python manage.py makemigrations --check --dry-run
  uv run python manage.py check --fail-level WARNING
  uv run coverage run manage.py test core && uv run coverage report
  ```

## Review Focus

1. **Copy into own subtree.** Copying project P into P or any descendant of P must be rejected. This applies to the model call, a forged UI POST, and the API. Otherwise the recursive copy would walk into the copy it just created and recurse without end. Tests: Task 12 `test_deep_copy_into_own_subtree_raises`, Task 13 `test_copy_post_with_target_in_subtree_rejected`, Task 15 `test_api_copy_into_own_subtree_400`.
2. **Copy when a file is missing on disk.** A part's STL, a document or a project image can be missing from disk (prod has orphaned and missing media). The copy must still succeed: that field is left empty and a warning is logged. If the transaction fails, no new files may be left behind. Tests: Task 12 `test_deep_copy_part_missing_stl_on_disk`, `test_deep_copy_failure_removes_new_files`.
3. **Delete versus job status edge cases.** Jobs in `pending`, `slicing`, `sliced`, `uploading` and `uploaded` block a delete, not only `draft`/`printing`. Jobs in `completed`, `failed` and `cancelled` never block. A job that mixes subtree and outside parts still blocks. The POST and the API `DELETE` re-check inside the transaction. Tests: Task 6 `test_each_unfinished_status_blocks_delete`, `test_post_rechecks_block`, Task 10 `test_api_delete_project_with_unfinished_job_409`.
4. **Root without a preset after re-parenting through the API.** `PATCH {"parent": null}` on a sub-project that has no own preset must return 400. The same goes for `PATCH {"default_print_preset": null}` on a root. A re-parent that would create a cycle must return 400, not 500. Tests: Task 10 `test_patch_subproject_to_root_without_preset_400`, `test_patch_root_clear_preset_400`, `test_patch_parent_cycle_400`.
5. **Remembered print-job filter pointing at a deleted or non-root project.** The session filter must be dropped silently: no 500, and the user is not trapped on an empty list. Test: Task 8 `test_saved_project_filter_dropped_when_not_root_or_missing`.

---

## PR split (deviates from the suggested 3-PR split, for these reasons)

- **PR 1 — Prune DAG-only UI (no schema change).** This PR only removes features that do not need the schema: the parts library, the "Used in" UI and duplicate-as-variant. Doing this first makes PR 2 smaller, and each removal is easy to review on its own. Duplicate-as-variant is unavailable until PR 3 adds copy. That gap is acceptable because the owner is the only user.
- **PR 2 — Strict tree.** This PR holds the model, the migrations and **every** consumer of the edge models: views, forms, templates, API serializers and endpoints, worker, dashboard and admin. Once `ProjectPart`/`ProjectComponent` are dropped, nothing that references them can import. The **subtree delete guard also lives here**, not in the copy PR: `Project.parent` and `Part.project` become `CASCADE` in this PR. Without the guard, a prod deploy between PR 2 and PR 3 could silently cascade-delete parts in queued or printing jobs. Inside the PR, tasks follow expand → switch consumers → contract, so each commit keeps the touched test modules green.
- **PR 3 — Deep copy.** Model copy methods, the copy UI for projects and parts, "add existing" done as a copy, and the API copy endpoints.
- **PR 4 — Move and file cleanup** (added 2026-10-28, spec Goals 8/9). Re-parenting in the web UI, deleting files on row delete, and the `cleanup_orphan_media` command.
- **PR 5 — Docs and leftovers.** README, user guide, `CLAUDE.md`, superseded plans and specs, and a stale-comment sweep.

## File Structure

| File | Responsibility | PR |
|---|---|---|
| `core/models/parts.py` | `Part.project`/`quantity`, bottom-up `effective_print_preset`, `deep_copy` | 2, 3 |
| `core/models/projects.py` | tree aggregation, `ancestors`/`root`/`path_label`, `subtree_ids`, `blocking_jobs`, `deletion_summary`, `build_progress`, `deep_copy` | 2, 3 |
| `core/models/file_copy.py` (new) | `duplicate_field_file()` + `CopiedFiles` cleanup tracker | 3 |
| `core/models/composition.py` | **deleted** | 2 |
| `core/models/printing.py` | drop `target_assembly`; add `PrintJob.TERMINAL_STATUSES` | 2 |
| `core/migrations/0029_part_project_quantity.py` | generated: add nullable `Part.project` + `Part.quantity` | 2 |
| `core/migrations/0030_backfill_tree.py` | the one RunPython data step | 2 |
| `core/migrations/0031_strict_tree.py` | generated: non-null, CASCADE, drop edges/fields | 2 |
| `core/forms/projects.py`, `core/forms/parts.py` | tree forms; new `ProjectCopyForm`, `PartCopyForm`, `AddExistingForm` | 2, 3 |
| `core/views/projects.py`, `core/views/parts.py`, `core/views/print_jobs.py`, `core/views/dashboard.py`, `core/views/helpers.py` | tree consumers, delete guard, copy views | 1–3 |
| `core/api/serializers.py`, `core/api/views.py`, `core/api/urls.py` | tree contract, copy endpoints | 2, 3 |
| `core/services/slicing_worker.py` | preset from `part.effective_print_preset` | 2 |
| `core/templates/core/*.html` | removals; `project_copy.html`, `part_copy.html` (new) | 1–3 |
| `core/tests/test_migration_tree.py`, `test_project_delete.py`, `test_deep_copy.py`, `test_views_copy.py`, `test_api_copy.py` (new) | new coverage | 2, 3 |

---

## PR 1 — Prune DAG-only UI

Branch: `feat/tree-rollback-1-prune`. No model changes. `makemigrations --check` must stay clean.

### Task 1: Remove the parts library

**Files:**
- Modify: `core/views/parts.py` (delete `PartLibraryListView`, change `PartDeleteView.get_success_url`), `core/views/__init__.py` (drop export), `core/urls/parts.py` (drop `part_library` path + import), `core/templates/base.html:39` (nav link), `core/templates/core/part_detail.html:10`, `part_form.html:12`, `part_confirm_delete.html:9` (breadcrumb)
- Delete: `core/templates/core/parts_library.html`
- Test: `core/tests/test_views_parts.py`, `core/tests/test_urls.py`

**Interfaces:**
- Produces: the URL name `core:part_library` no longer exists. `PartDeleteView` redirects to `core:project_list`. PR 2 Task 7 changes that redirect to the part's project.

- [ ] **Step 1: Write failing tests.** In `test_views_parts.py`: delete the class `PartLibraryViewTests`. Add `PartViewTests.test_part_delete_redirects_to_project_list` (`assertRedirects(resp, reverse("core:project_list"))`) and `test_nav_has_no_parts_library_link` (`assertNotContains(self.client.get(reverse("core:project_list")), "Parts Library")`). In `test_urls.py` add `test_part_library_url_removed`, which asserts `NoReverseMatch` on `reverse("core:part_library")`. Remove any existing `part_library` entry from that module.
- [ ] **Step 2: Run them and confirm they fail.** `DEBUG=1 uv run python manage.py test core.tests.test_views_parts core.tests.test_urls`. Expected: FAIL, because the redirect still targets the library and the URL still resolves.
- [ ] **Step 3: Delete the view, URL, template and nav link.** Point the three breadcrumbs at `{% url 'core:project_list' %}` with the label "Projects".
- [ ] **Step 4: Run the same command.** Expected: PASS.
- [ ] **Step 5: Commit** `git commit -m "refactor(parts): remove parts library (tree rollback)"`.

### Task 2: Remove the "Used in" UI

**Files:**
- Delete: `core/templates/core/includes/_used_in.html`
- Modify, removing the `{% include "core/includes/_used_in.html" %}` line from each of these 12 templates: `part_detail.html`, `project_cost.html`, `project_confirm_delete.html`, `projectdocument_confirm_delete.html`, `projectdocument_form.html`, `part_form.html`, `projecthardware_form.html`, `project_form.html`, `projecthardware_confirm_delete.html`, `subproject_form.html`, `part_confirm_delete.html`, `project_detail.html`
- Modify: `project_confirm_delete.html:14-21` and `part_confirm_delete.html:15-22` (delete the "used in N assemblies" alert blocks), `part_detail.html:19` (drop the "— used in N assemblies" subtitle suffix), `part_detail.html` `{% if part_usage %}` card (delete the whole block), `project_form.html:58-59` (Cancel goes to `project_detail` of `form.instance` when it has a pk, else `project_list`)
- Modify, removing every `context["used_in"] = …`: `core/views/projects.py` (`ProjectDetailView`, `SubProjectCreateView`, `ProjectUpdateView`, `ProjectDeleteView`, `ProjectCostView`), `core/views/parts.py` (`PartDetailView` + its `part_usage` loop, `PartCreateView`, `PartUpdateView`, `PartDeleteView.get_context_data`), `core/views/documents.py:43,71`, `core/views/hardware.py:168,213,240`
- Keep: the hardware-catalog `used_in_count` / `used_in_projects` in `core/views/hardware.py:43,126-127` and `hardware_library.html` / `hardwarepart_confirm_delete.html`. These count catalog assignments, which is not DAG behaviour.
- Keep for now (PR 2 removes them with their last callers): `Project.parent_assemblies`, `Part.containing_projects`, `PartDetailView._candidate_assemblies`.
- Test: `core/tests/test_views_parts.py`, `core/tests/test_views_projects.py`, `core/tests/test_views_documents.py`, `core/tests/test_views_hardware.py`

**Interfaces:**
- Produces: no view puts `used_in` or `part_usage` in its context.

- [ ] **Step 1: Write failing tests.** Delete `PartDeleteUsedInTests` and `PartViewTests.test_part_detail_shows_used_in_breakdown`. Add `test_no_used_in_context_anywhere` to `test_views_projects.py`. It GETs `project_detail`, `project_update`, `project_delete`, `project_cost`, `subproject_create`, `part_detail`, `part_update` and `part_delete` for the fixture objects, and for each one asserts `"used_in" not in resp.context` and `assertNotContains(resp, "Used in:")`. Add `test_part_detail_has_no_usage_table`, which asserts `"part_usage" not in resp.context`.
- [ ] **Step 2: Run and confirm failure.** `DEBUG=1 uv run python manage.py test core.tests.test_views_projects core.tests.test_views_parts`. Expected: FAIL.
- [ ] **Step 3: Remove the include, the template blocks and the context keys listed above.**
- [ ] **Step 4: Run the full suite.** `DEBUG=1 uv run python manage.py test core`. Expected: PASS. Delete any other test that asserted "Used in" text: `grep -rn "Used in\|used_in\b" core/tests` must only show hardware-catalog tests.
- [ ] **Step 5: Commit** `git commit -m "refactor(ui): remove 'Used in' navigation (tree rollback)"`.

### Task 3: Remove duplicate-as-variant

**Files:**
- Modify: `core/models/projects.py` (delete `Project.duplicate_as_variant`), `core/views/projects.py` (delete `ProjectDuplicateAsVariantView` + `__all__` entry), `core/views/__init__.py`, `core/urls/projects.py` (drop the `project_duplicate` path), `core/templates/core/project_detail.html:46` (drop the button), `core/api/views.py` (delete the `ProjectViewSet.duplicate` action)
- Delete: `core/templates/core/project_duplicate.html`, `core/tests/test_variant_duplicate.py`
- Test: `core/tests/test_api_search_duplicate.py` (delete `ApiDuplicateTests`), `core/tests/test_urls.py`

**Interfaces:**
- Produces: `core:project_duplicate` and `POST /api/v1/projects/{id}/duplicate/` are gone. PR 3 adds `core:project_copy` and `/copy/`.

- [ ] **Step 1: Write failing tests.** In `test_urls.py`: `test_project_duplicate_url_removed` (`NoReverseMatch`). In `test_api_search_duplicate.py`: `test_duplicate_endpoint_gone`, where `POST /api/v1/projects/{id}/duplicate/` by an Admin token returns 404. Also assert `not hasattr(Project, "duplicate_as_variant")`.
- [ ] **Step 2: Run** `DEBUG=1 uv run python manage.py test core.tests.test_urls core.tests.test_api_search_duplicate`. Expected: FAIL.
- [ ] **Step 3: Delete the listed code, template and test module.**
- [ ] **Step 4: Run the full CI gate (Global Constraints).** Expected: all green.
- [ ] **Step 5: Commit** `git commit -m "refactor(projects): remove duplicate-as-variant (replaced by copy later)"`, then open PR 1 against `main`.

---

## PR 2 — Strict tree

Branch: `feat/tree-rollback-2-tree`, based on `main` after PR 1 is merged. Order inside the PR: expand (Task 4) → switch consumers (Tasks 5–10) → contract (Task 11).

### Task 4: Tree fields + backfill migration (expand)

**Files:**
- Modify: `core/models/parts.py` (add fields), `core/tests/mixins.py` (dual-write fixtures)
- Create: `core/migrations/0029_part_project_quantity.py` (generated), `core/migrations/0030_backfill_tree.py` (`--empty`, then filled in)
- Test: `core/tests/test_migration_tree.py` (new)

**Interfaces:**
- Produces:
  - `Part.project = ForeignKey("core.Project", on_delete=CASCADE, related_name="parts", null=True, blank=True)`. It stays nullable until Task 11.
  - `Part.quantity = PositiveIntegerField(default=1, validators=[MinValueValidator(1)])` plus `CheckConstraint(condition=Q(quantity__gte=1), name="part_quantity_gte_1")`. That constraint name was freed by 0021.
  - Migration `0030_backfill_tree` exposes `collect_tree_conflicts(part_model, part_link_model, component_model) -> list[str]` and `forwards(apps, schema_editor) -> None`. The reverse is `migrations.RunPython.noop`.
  - Fixtures in `TestDataMixin` / `_RBACTestBase` create parts with `project=…, quantity=…` **and** still create the matching `ProjectPart`. This dual-write is removed in Task 11.

- [ ] **Step 1: Write the failing migration tests** in `core/tests/test_migration_tree.py`. The tests use `django.db.migrations.executor.MigrationExecutor` in a `TransactionTestCase`: django-test-migrations is not installed. In `setUp`, migrate to `[("core", "0029_part_project_quantity")]`, and build the historical models with `executor.loader.project_state(...).apps`. In `tearDown`, migrate to the leaf nodes (`executor.loader.graph.leaf_nodes()`) so later tests see the current schema.
  - `test_backfill_clean_data`: root R (with preset), sub S under R by edge qty 2, while S's legacy `parent` deliberately points at an unrelated project X; part P linked to S by edge qty 3. After migrating to 0030: `P.project_id == S.pk`, `P.quantity == 3`, `S.parent_id == R.pk`, `S.quantity == 2`, `R.parent_id is None`. Also: a project with no parent edge whose stale legacy `parent` is set ends with `parent_id is None`.
  - `test_abort_on_part_in_two_projects`: a part with 2 `ProjectPart` rows. Migrating raises `RuntimeError`, and the message contains `"Tree migration aborted"` and the part pk.
  - `test_abort_on_project_with_two_parents`: a child with 2 `ProjectComponent` parent edges. `RuntimeError` containing the child pk.
  - `test_abort_on_part_without_edge`: a part with no `ProjectPart`. `RuntimeError` containing the part pk.
  - `test_reports_root_without_preset`: run with `stdout` captured (`contextlib.redirect_stdout`). The migration succeeds, and the output contains `"has no default_print_preset"` and the root pk.
  - `test_print_history_untouched`: a `PrintJob` + `PrintJobPart` for P exists. After 0030 the `PrintJobPart` count and its `part_id` are unchanged.
- [ ] **Step 2: Run** `DEBUG=1 uv run python manage.py test core.tests.test_migration_tree`. Expected: FAIL (migration 0029 does not exist).
- [ ] **Step 3: Add the two fields + constraint to `Part`.** Run `uv run python manage.py makemigrations core --noinput --name part_project_quantity`, then `uv run python manage.py makemigrations core --empty --name backfill_tree`. Fill in 0030 with exactly this algorithm. It is self-contained and uses historical models only, never runtime helpers (same rule as 0019):

```python
def collect_tree_conflicts(part_model, part_link_model, component_model) -> list[str]:
    problems: list[str] = []
    for row in part_link_model.objects.values("part_id").annotate(n=Count("id")).filter(n__gt=1):
        ids = list(part_link_model.objects.filter(part_id=row["part_id"]).values_list("project_id", flat=True))
        problems.append(f"Part {row['part_id']} is in {row['n']} projects: {ids}")
    for row in component_model.objects.values("child_project_id").annotate(n=Count("id")).filter(n__gt=1):
        ids = list(component_model.objects.filter(child_project_id=row["child_project_id"]).values_list("parent_project_id", flat=True))
        problems.append(f"Project {row['child_project_id']} has {row['n']} parents: {ids}")
    linked = part_link_model.objects.values("part_id")
    for part in part_model.objects.exclude(pk__in=linked).only("pk", "name"):
        problems.append(f"Part {part.pk} '{part.name}' belongs to no project")
    return problems


def forwards(apps, schema_editor) -> None:
    Project, Part = apps.get_model("core", "Project"), apps.get_model("core", "Part")
    ProjectPart, ProjectComponent = apps.get_model("core", "ProjectPart"), apps.get_model("core", "ProjectComponent")
    problems = collect_tree_conflicts(Part, ProjectPart, ProjectComponent)
    if problems:
        details = "\n".join(problems)
        raise RuntimeError(f"Tree migration aborted — shared data must be resolved first:\n{details}")
    for link in ProjectPart.objects.all().iterator():
        Part.objects.filter(pk=link.part_id).update(project_id=link.project_id, quantity=link.quantity)
    edges = {e.child_project_id: e for e in ProjectComponent.objects.all()}
    for project_id in Project.objects.values_list("pk", flat=True):
        edge = edges.get(project_id)
        if edge is None:
            Project.objects.filter(pk=project_id).update(parent_id=None)  # overwrite unreliable legacy parent
        else:
            Project.objects.filter(pk=project_id).update(parent_id=edge.parent_project_id, quantity=edge.quantity)
    for root in Project.objects.filter(parent__isnull=True, default_print_preset__isnull=True):
        sys.stdout.write(f"WARNING: root project {root.pk} '{root.name}' has no default_print_preset\n")
```

  Because it uses `.update()`, the runtime `Project.save()` edge seeding never runs. The edge-based cycle guard means a single-parent edge set is acyclic, so the resulting chain is acyclic too.
- [ ] **Step 4: Update `core/tests/mixins.py`** so every `Part.objects.create(...)` there also passes `project=<owning project>, quantity=<the edge qty>`. Keep the `ProjectPart.objects.create(...)` lines for now.
- [ ] **Step 5: Run** `DEBUG=1 uv run python manage.py test core.tests.test_migration_tree`, then the full `DEBUG=1 uv run python manage.py test core`. Expected: PASS. Also `uv run python manage.py makemigrations --check --dry-run` reports no changes.
- [ ] **Step 6: Commit** `git commit -m "feat(models): add Part.project/quantity and backfill tree from edges"`.

### Task 5: Tree aggregation + bottom-up preset resolution (models)

**Files:**
- Modify: `core/models/projects.py`, `core/models/parts.py`, `core/models/printing.py`
- Test: `core/tests/test_aggregation.py` (adapt + extend), `core/tests/test_preset_resolution.py` (rewrite)

**Interfaces:**
- Consumes: `Part.project`, `Part.quantity` (Task 4).
- Produces, in `core/models/printing.py`:
  - `PrintJob.TERMINAL_STATUSES: tuple[str, ...] = (STATUS_COMPLETED, STATUS_FAILED, STATUS_CANCELLED)`
- Produces, in `core/models/projects.py` (`Project`):
  - `ancestors(self) -> list[Project]`: root first, excluding self, following `parent`
  - `root` (property) `-> Project`
  - `path_label` (property) `-> str`: names joined by `" › "`, root first, including self
  - `get_descendant_ids(self) -> set[int]`: walks `subprojects`; no `_path` argument
  - `subtree_ids(self) -> set[int]`: `{self.pk} | get_descendant_ids()`
  - `_collect_parts_with_multiplier(self, multiplier: int = 1) -> list[tuple[Part, int]]`: yields `(part, multiplier * part.quantity)` for `self.parts`, then recurses into `subprojects` with `multiplier * sub.quantity`. Each part appears exactly once.
  - `_collect_hardware_with_multiplier(self, multiplier: int = 1) -> list[tuple[ProjectHardware, int]]`
  - `_collect_documents(self) -> list[tuple[ProjectDocument, Project]]`
  - `aggregate_prefetch_lookups(cls, depth: int = 3) -> list[str]`: leaf `"parts__job_entries__print_job__plates"` per level, plus `"subprojects"` chains (`"subprojects__subprojects…"`)
  - `hardware_prefetch_lookups(cls, depth: int = 3) -> list[str]`: `"subprojects" + "__subprojects" * level`
  - `build_progress(self) -> dict`: keys `percent`, `needed`, `printed`, `parts` (rows `{part, needed, printed, remaining}`), where `printed = part.printed_quantity` (all completed jobs, no attribution) and per-part capping is at `needed`
  - `total_parts_count`, `printed_parts_count`, `progress_percent`, `aggregated_status`, `filament_requirements()`, `total_filament_*`, `hardware_requirements()`: same names, tree semantics. `remaining_*` comes from `build_progress`. There is no `distinct_parts` dedup.
  - `hardware_requirements()["projects"]`: `sorted({hw.project.path_label …})`. These are the tree path names that feed `ShoppingRow.projects`.
  - `effective_default_print_preset`, `effective_default_print_preset_id`: kept; walk `parent` (keep the visited guard; it costs nothing).
  - `direct_part_count(self) -> int`: `self.parts.count()`
- Produces, in `core/models/parts.py` (`Part`):
  - `effective_print_preset` (property) `-> Optional[OrcaPrintPreset]`: own `print_preset`, else `self.project.effective_default_print_preset`, else `None`
  - `effective_print_preset_id` (property) `-> Optional[int]`: same rule using ids
  - `is_estimable(self) -> bool`: `bool(self.stl_file) and self.effective_print_preset_id is not None`
  - `printed_quantity`: unchanged. `_fully_printed_entry_pks(self) -> QuerySet` loses its `**filters` argument.
- Removed in this task (no non-model caller depends on them after this task, or the caller is switched in the same commit): `Project._expand_parts_relative`, every `_memo` / `_path` parameter. `Project.is_subproject` is kept, since `project_form.html` uses it, and is rewritten as `return self.parent_id is not None`. Delete `Project.variant_progress` (last callers: `ProjectDetailView` in Task 6, API `requirements` in Task 10; `aggregated_status` and `filament_requirements` switch to `build_progress` here), `resolve_part_preset`, `resolve_part_presets`, `resolve_estimation_preset_map`, `_resolve_presets_walk`, `Part.resolve_estimation_preset`, `Part.resolve_job_preset_candidates`, `Part.printed_quantity_for` and `Part.printed_quantity_for_by_preset` in the task that switches their last caller (Tasks 6–10). Task 11 checks with grep that none remain.

- [ ] **Step 1: Write failing tests.** Rewrite `test_preset_resolution.py` with the class `BottomUpPresetResolutionTests`:
  - `test_part_override_wins`
  - `test_inherits_from_own_project`
  - `test_inherits_from_nearest_ancestor_skipping_empty_levels` (root R preset A, mid M empty, leaf L empty, part in L → A; set M preset B → B)
  - `test_none_when_no_preset_anywhere`
  - `test_is_estimable_requires_stl_and_resolved_preset`

  In `test_aggregation.py`, adapt fixtures to `Part(project=…, quantity=…)` / `Project(parent=…, quantity=…)` and add:
  - `test_needed_is_part_qty_times_chain` (R → S qty 2 → T qty 3, part qty 4 in T → `total_parts_count == 24`)
  - `test_printed_progress_counts_all_completed_jobs_without_attribution` (one completed job with qty 10 for that part → `build_progress()["printed"] == 10`, percent 41)
  - `test_printed_capped_at_needed` (prints 30 of a need of 24 → `printed_parts_count == 24`, `progress_percent == 100`)
  - `test_aggregate_prefetch_keeps_queries_flat` (adapt the existing `ProjectAggregatePrefetchTests` to the new lookups)
  - `test_hardware_requirements_projects_are_path_labels` (`["R › S"]`)
  - `test_path_label_and_root`
  - `test_subtree_ids`
- [ ] **Step 2: Run** `DEBUG=1 uv run python manage.py test core.tests.test_preset_resolution core.tests.test_aggregation`. Expected: FAIL.
- [ ] **Step 3: Implement the produced interfaces.** Keep `Project.save()` calling `_assert_parent_acyclic()`. Leave its edge seeding in place until Task 11.
- [ ] **Step 4: Run the same command, plus `core.tests.test_models`.** Expected: PASS.
- [ ] **Step 5: Commit** `git commit -m "feat(models): tree aggregation and bottom-up preset resolution"`.

### Task 6: Project views + forms on the tree, subtree delete with guard

**Files:**
- Modify: `core/views/projects.py`, `core/forms/projects.py`, `core/forms/__init__.py`, `core/urls/projects.py`, `core/views/__init__.py`, `core/models/projects.py`, `core/admin.py`, `core/templates/core/project_detail.html`, `project_confirm_delete.html`, `subproject_form.html`, `project_form.html`, `project_list.html`, `project_cost.html`
- Test: `core/tests/test_views_projects.py`, `core/tests/test_forms.py`, `core/tests/test_project_delete.py` (new)

**Interfaces:**
- Consumes: Task 5 interfaces, `PrintJob.TERMINAL_STATUSES`.
- Produces:
  - `Project.blocking_jobs(self) -> QuerySet[PrintJob]`: `PrintJob.objects.filter(job_parts__part__project_id__in=self.subtree_ids()).exclude(status__in=PrintJob.TERMINAL_STATUSES).distinct()`
  - `Project.deletion_summary(self) -> dict[str, int]`: keys `subprojects`, `parts`, `documents`, `hardware`, `print_history`. `print_history` = the number of `PrintJobPart` rows of subtree parts.
  - `ProjectEditForm.__init__`: when `instance.parent_id` is set, adds a `quantity` field and makes `default_print_preset` optional with help text `"Inherited from parent if empty."`. On roots, the preset is required.
  - `SubProjectForm`: `default_print_preset.required = False` with the same help text.
  - `ProjectForm`: unchanged (preset required).
- Remove, in this task:
  - Views: `ProjectAddComponentView`, `ProjectAddPartView`, `ProjectComponentDeleteView`, `ProjectPartDeleteView`, `ProjectComponentUpdateView`, `ProjectPartUpdateView`, plus their `__all__` entries and the exports in `core/views/__init__.py`
  - URL names `project_add_component`, `project_add_part`, `project_component_remove`, `project_component_quantity`, `project_part_remove`, `project_part_quantity`
  - Forms `AddComponentForm`, `ProjectComponentQuantityForm` (`core/forms/projects.py`), and `AddPartToProjectForm`, `ProjectPartQuantityForm` (`core/forms/parts.py`), plus the `core/forms/__init__.py` exports
  - Template `project_detail.html`: the "Add existing module" and "Add existing part" buttons, the inline quantity `<form>`s and the "Remove from assembly" `<form>`s in both tables, and the two modals `#addComponentModal` / `#addPartModal` (the `{% if can_edit_assembly %}` block near line 621)
  - Context: `component_edges`, `part_edges`, `child_modules`, `direct_parts`, `add_component_form`, `add_part_form`, `can_edit_assembly`, `variant_progress`, `variant_rows_by_pk`
  - Model methods `Project.parent_assemblies`, `child_modules`, `direct_parts`, `resolve_estimation_preset_map` (its last caller, `ProjectReEstimateView`, is switched here)
  - `ProjectAdmin.exclude` (`core/admin.py`). Also add `"parent"` to `list_display`/`list_filter`.
  - `ProjectListView`'s `.distinct()`

**Re-wiring:**
- `ProjectListView.get_queryset`: `Project.objects.filter(parent__isnull=True).prefetch_related(*Project.aggregate_prefetch_lookups())`.
- `ProjectDetailView`: the tables iterate `project.subprojects.all()` (showing `sub.quantity`) and `project.parts.all()` (showing `part.quantity` and the row from `build_progress`). The context gets `build_progress`, `build_rows_by_pk` and `ancestors`. `build_progress_expanded = build_progress["printed"] > 0 or PrintJobPart.objects.filter(part__project_id__in=project.subtree_ids()).exclude(print_job__status__in=PrintJob.TERMINAL_STATUSES).exists()`. The breadcrumb is Projects › ancestors › project.
- `SubProjectCreateView.form_valid`: sets `parent` (plus `quantity` from the form).
- `ProjectCostView`: iterates `project.parts.all()` with `part.quantity`.
- `ProjectReEstimateView`: for each `part, _ in project._collect_parts_with_multiplier()` with an STL and `effective_print_preset_id`, clear the estimates (keep the update dict but drop `estimation_requested_preset`) and call `_trigger_part_estimation`. There is no dedup.
- `ProjectDeleteView`:
  - `get_context_data` adds `summary = self.object.deletion_summary()` and `blocking_jobs = self.object.blocking_jobs()`.
  - `form_valid` runs inside `transaction.atomic()`. It re-evaluates `blocking_jobs().exists()`. If true, it does `messages.error(...)` and redirects back to `core:project_delete`. Otherwise it remembers `parent_id`, deletes, and redirects to `core:project_detail` of the parent, or to `core:project_list` when there is none.
  - `project_confirm_delete.html` renders the four counts. When `summary.print_history` is greater than zero, it shows a warning that print history will be deleted. When `blocking_jobs` exist, it lists them (links to `core:printjob_detail`) and hides the submit button.

- [ ] **Step 1: Write failing tests.** New `core/tests/test_project_delete.py`:
  - `test_confirm_page_shows_counts`: R with 2 subs, 3 parts, 1 doc, 1 hardware → context `summary == {"subprojects": 2, "parts": 3, "documents": 1, "hardware": 1, "print_history": 0}`
  - `test_delete_cascades_subtree`: after POST, `Project`, `Part`, `ProjectDocument` and `ProjectHardware` rows of the subtree are gone; a sibling root is untouched
  - `test_each_unfinished_status_blocks_delete`: a `subTest` per status in `draft, pending, slicing, sliced, uploading, uploaded, printing`; a job with a part of a grandchild; POST → project still exists, response redirects to the confirm page, error message
  - `test_terminal_statuses_do_not_block`: completed, failed, cancelled → deleted; the `PrintJob` row and its plates still exist; its `job_parts` for deleted parts are gone
  - `test_mixed_job_with_outside_part_still_blocks`
  - `test_post_rechecks_block`: GET shows no blockers; create a draft job; POST → not deleted
  - `test_confirm_page_lists_blocking_job_links_and_hides_submit`
  - `test_redirects_to_parent_after_sub_delete` and `test_redirects_to_list_after_root_delete`
  - `test_delete_forbidden_for_operator`: 403, with the RBAC base in `core/tests/mixins.py`

  In `test_views_projects.py`: delete `AssemblyEditorViewTests` and adapt `BuildProgressExpandedTests` to the job-in-subtree rule. Add:
  - `test_project_list_shows_only_roots`
  - `test_detail_lists_subprojects_and_parts_with_quantity`
  - `test_subproject_create_sets_parent_and_quantity`
  - `test_subproject_create_preset_optional`
  - `test_edit_subproject_quantity`
  - `test_edit_root_requires_preset`
  - `test_re_estimate_queues_each_part_once_with_inherited_preset`
  - `test_edge_urls_removed` (`NoReverseMatch` for the 6 names)

  In `test_forms.py`: delete `AddComponentFormTests` and `AddPartToProjectFormTests`. Adapt `ProjectEditFormTests` and `ProjectFormPresetRequiredTests` (the preset is required for roots only). `ProjectAdminTests` asserts that `parent` and `quantity` are on the admin change form.
- [ ] **Step 2: Run** `DEBUG=1 uv run python manage.py test core.tests.test_project_delete core.tests.test_views_projects core.tests.test_forms`. Expected: FAIL.
- [ ] **Step 3: Implement the removals and re-wiring above.**
- [ ] **Step 4: Run the same command.** Expected: PASS.
- [ ] **Step 5: Commit** `git commit -m "feat(projects): tree views/forms, subtree delete blocked by unfinished jobs"`.

### Task 7: Part views on the tree

**Files:**
- Modify: `core/views/parts.py`, `core/forms/parts.py`, `core/models/parts.py`, `core/templates/core/part_detail.html`, `part_form.html`, `part_confirm_delete.html`
- Test: `core/tests/test_views_parts.py`, `core/tests/test_forms.py`

**Interfaces:**
- Consumes: `Part.effective_print_preset_id`, `Project.ancestors()`.
- Produces: `PartForm.Meta.fields` now includes `"quantity"`, placed after `"name"`.
- Remove:
  - `PartDetailView._candidate_assemblies` and the `candidate_assemblies`, `job_preset_auto` and `job_preset_choices` context keys
  - From `part_detail.html`: the `job_preset_choices` select block and the `target_assembly` "Build target" select block
  - `Part.containing_projects` and `Part.resolve_job_preset_candidates`
  - All `estimation_requested_preset=None` entries in the update dicts of `PartUpdateView.form_valid` and `PartReEstimateView.post`

**Re-wiring:**
- `PartCreateView.form_valid`: set `form.instance.project = self.project` before saving, and drop the `ProjectPart.objects.create`.
- `PartDetailView` draft-job compatibility: `allowed_preset_ids = {part.effective_print_preset_id} - {None}`.
- `PartDeleteView.get_success_url`: `core:project_detail` of `self.object.project_id`.
- Breadcrumbs: Projects › `part.project.ancestors` › `part.project` › part.

- [ ] **Step 1: Write failing tests** in `test_views_parts.py`:
  - `test_part_create_sets_project_and_quantity` (POST with `quantity=5` → `Part.project == project`, `quantity == 5`, no `ProjectPart` created)
  - `test_part_update_quantity`
  - `test_part_delete_redirects_to_project`
  - `test_part_detail_has_no_build_target_or_preset_choice` (`assertNotContains(resp, 'name="target_assembly"')`, `assertNotContains(resp, 'name="print_preset"')` inside the add-to-job form)
  - `test_part_detail_breadcrumb_shows_ancestors`
  - `test_draft_jobs_filtered_by_inherited_preset`

  In `test_forms.py`: `PartFormTests.test_quantity_min_1`.
- [ ] **Step 2: Run** `DEBUG=1 uv run python manage.py test core.tests.test_views_parts core.tests.test_forms`. Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run the same command.** Expected: PASS.
- [ ] **Step 5: Commit** `git commit -m "feat(parts): part views on the tree, quantity back on PartForm"`.

### Task 8: Print jobs without attribution

**Files:**
- Modify: `core/views/print_jobs.py`, `core/templates/core/printjob_detail.html:75`, `core/templates/core/printjob_list.html` (only if the project label changes), `core/models/parts.py`
- Test: `core/tests/test_views_jobs.py`, `core/tests/test_printjob_list_filament.py`

**Interfaces:**
- Consumes: `Part.effective_print_preset_id`, `Project._collect_parts_with_multiplier`, `Project.subtree_ids`, `Project.path_label`, `Part.printed_quantity`.
- Produces: `PrintJobListView._existing_roots(pks: list[str]) -> list[str]` (static). It keeps only pks of existing projects with `parent__isnull=True`.
- Remove:
  - `AddPartToJobView._resolve_target_assembly`, the `print_preset` POST choice handling, and every `target_assembly` default or update in `AddPartToJobView.post`
  - `target_assembly=project` in `CreateJobsFromProjectView`, the whole per-preset / legacy-greedy remainder block, and `Part.printed_quantity_for_by_preset` / `printed_quantity_for`
  - `Project.resolve_part_presets` (its last caller)

**Re-wiring:**
- `AddPartToJobView`: `incoming_preset_id = part.effective_print_preset_id`. When it is `None`, keep the existing "no resolvable print preset" error message.
- `CreateJobsFromProjectView`: for each `(part, needed)` from `project._collect_parts_with_multiplier()` with an STL, compute `remaining = max(0, needed - part.printed_quantity)`. When `part.effective_print_preset_id` is `None`, increment `skipped_no_preset`. Otherwise group by `(part.effective_print_preset_id, part.spoolman_filament_id)`. Keep the job naming and the messages.
- `PrintJobListView`:
  - `get_queryset` with a `project` filter: the union of `Project.objects.get(pk=pk).subtree_ids()` over the selected roots → `queryset.filter(job_parts__part__project_id__in=ids)`.
  - `get_context_data["projects"]`: `Project.objects.filter(parent__isnull=True).order_by("name")`.
  - `_saved_filters` and `get_queryset` both pass `filters["project"]` through `_existing_roots`.
- `printjob_detail.html` "Project" cell: `jp.part.project.path_label`, linked to `core:project_detail` of `jp.part.project`. Add `select_related("part__project")` in `PrintJobDetailView.get_queryset`.

- [ ] **Step 1: Write failing tests** in `test_views_jobs.py`. Replace the `target_assembly=` kwargs in fixtures and delete `CreateJobsPerPathPresetTests` and `CreateJobsPrintedByPresetTests`. Add:
  - `test_create_jobs_groups_by_inherited_preset_and_filament` (parts in R and in sub S with a different preset → 2 jobs, each with `print_preset_id` set)
  - `test_create_jobs_subtracts_global_printed`
  - `test_create_jobs_skips_part_without_resolvable_preset`
  - `test_add_part_to_job_uses_inherited_preset`
  - `test_add_part_to_job_ignores_target_assembly_param` (POST with `target_assembly=1` still works, and `PrintJobPart` has no such attribute)
  - `test_job_detail_shows_root_path` (`assertContains(resp, "R › S")`)
  - `test_filter_project_matches_subtree` (a job with a part in a grandchild of R matches `project=R`; a job in another root does not)
  - `test_project_filter_choices_are_roots_only`
  - `test_saved_project_filter_dropped_when_not_root_or_missing`. Save the session filter `{"project": [str(S.pk)]}` (S is a sub-project); a bare GET → 200 with no redirect loop, and `filters_active` is False. Repeat with a deleted pk.
- [ ] **Step 2: Run** `DEBUG=1 uv run python manage.py test core.tests.test_views_jobs core.tests.test_printjob_list_filament`. Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run the same command.** Expected: PASS.
- [ ] **Step 5: Commit** `git commit -m "feat(jobs): drop target_assembly attribution; project filter by subtree"`.

### Task 9: Worker, dashboard, shopping, documents and hardware views

**Files:**
- Modify: `core/services/slicing_worker.py` (`_estimate_part_in_background`, `_slice_job_in_background`), `core/views/dashboard.py`, `core/templates/core/admin_dashboard.html:399`, `core/templates/core/project_shopping_list.html` (only if it renders `row.projects`), `core/views/shopping.py` (check its prefetch)
- Test: `core/tests/test_estimation.py`, `core/tests/test_views_dashboard.py`, `core/tests/test_views_shopping.py`, `core/tests/test_hardware_shopping.py`

**Interfaces:**
- Consumes: `Part.effective_print_preset`, `Project.hardware_prefetch_lookups`.
- Removed:
  - In `_estimate_part_in_background`: the requested-preset consume block and the "Preset ambiguous across projects" branch. It now uses `select_related("print_preset", "project")` and `print_preset = part.effective_print_preset`. When that is `None`, it sets status `none` as today.
  - `Part.resolve_estimation_preset`, whose last caller is the worker.

**Re-wiring:**
- `core/views/dashboard.py`:
  - `total_parts` = `Part.objects.aggregate(total=Sum("quantity"))`
  - filament total = `Part.objects.filter(filament_used_grams__isnull=False).aggregate(...)`
  - `material_breakdown` = `Part.objects.values("material").annotate(count=Count("pk"), total_qty=Sum("quantity")).order_by("-total_qty")`
  - every `prefetch_related("project_links__project")` / `part.project_links.first()` becomes `select_related("project")` / `part.project`
- `admin_dashboard.html:399` shows `part.project.name`.

- [ ] **Step 1: Write failing tests.** In `test_estimation.py`: delete `ProjectContextEstimationTests` and `PartEditClearsRequestChannelTests`, and adapt `EstimationPresetResolutionTests`. Add:
  - `test_worker_uses_inherited_preset` (part without own preset in sub S, root R preset A → `_find_compatible_machine` is called with A; mock the slicer client)
  - `test_worker_no_preset_sets_none`

  In `test_views_dashboard.py`: `test_total_parts_sums_part_quantities` and `test_material_breakdown_from_parts`. In `test_views_shopping.py`: `test_used_in_column_shows_tree_paths`.
- [ ] **Step 2: Run** `DEBUG=1 uv run python manage.py test core.tests.test_estimation core.tests.test_views_dashboard core.tests.test_views_shopping core.tests.test_hardware_shopping`. Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run the same command.** Expected: PASS.
- [ ] **Step 5: Commit** `git commit -m "feat(worker,dashboard): read the tree; drop preset request channel"`.

### Task 10: Build API tree contract

**Files:**
- Modify: `core/api/serializers.py`, `core/api/views.py`, `core/api/urls.py`, `core/api/__init__.py` (docstring only)
- Test: `core/tests/test_api_projects.py`, `core/tests/test_api_requirements.py`, `core/tests/test_api_estimation.py`, `core/tests/test_api_schema.py`, `core/tests/test_api_edges.py` (deleted in Task 11)

**Interfaces:**
- Consumes: `Project.subtree_ids`, `Project.blocking_jobs`, `Project._collect_parts_with_multiplier`, `Part.effective_print_preset_id`.
- Produces:
  - `ProjectSerializer.Meta.fields = ["id", "name", "description", "parent", "quantity", "default_print_preset", "created_at", "updated_at"]`. `default_print_preset` uses `required=False, allow_null=True`. `parent` uses `allow_null=True, required=False`.
  - `ProjectSerializer.validate(self, attrs: dict[str, Any]) -> dict[str, Any]`:
    1. Compute the effective parent and preset from `attrs`, falling back to `self.instance`.
    2. If the parent is `None` and the preset is `None`, raise `{"default_print_preset": ["Required for top-level projects."]}`.
    3. If `self.instance` exists and the parent pk is in `self.instance.subtree_ids()`, raise `{"parent": ["A project cannot be a sub-project of itself or one of its descendants."]}`.
  - `PartSerializer.Meta.fields`: adds `"project"` (required) and `"quantity"`.
  - `ProjectTreeSerializer.Meta.fields = ["id", "name", "description", "quantity", "parts", "subprojects", "hardware"]`. `get_parts` reads `obj.parts.all()`. `get_subprojects` returns `[ProjectTreeSerializer(sub, context=self.context).data for sub in obj.subprojects.all()]`. The `_path` guard goes away.
  - `ProjectViewSet.destroy(self, request, *args, **kwargs) -> Response`: `transaction.atomic()`. If `blocking_jobs()` exist, return 409 with `{"detail": …, "blocking_jobs": [ids]}`; otherwise delete and return 204.
- Remove:
  - `ProjectComponentSerializer`, `ProjectPartEdgeSerializer`, `ProjectComponentListCreate`, `ProjectComponentDetail`, `ProjectPartListCreate`, `ProjectPartDetail`
  - The URL names `project-components`, `project-component-detail`, `project-parts`, `project-part-detail`
  - `_jsonify_variant_progress` and the `variant_progress` key and schema field in `requirements`
  - The dedup dicts in `re_estimate` and `validate`
  - `estimation_requested_preset` in `re_estimate`, `estimate` and `stl`
  - `component_would_create_cycle` imports
- Keep: `_ProjectScopedMixin`. The documents and hardware endpoints still use it, so the spec's "if it becomes unused" removal does not apply.
- `estimate` action: return 400 when `part.effective_print_preset_id is None`.

- [ ] **Step 1: Write failing tests** in `test_api_projects.py`. Replace `ApiProjectPartTests` with `ApiTreeContractTests`:
  - `test_create_part_requires_project` (400 without `project`)
  - `test_create_part_with_project_and_quantity`
  - `test_create_root_requires_preset`
  - `test_create_subproject_without_preset_ok`
  - `test_patch_subproject_to_root_without_preset_400`
  - `test_patch_root_clear_preset_400`
  - `test_patch_parent_cycle_400` (parent = own grandchild → 400 with key `parent`, not 500)
  - `test_tree_is_nested_subprojects` (R → S → T: `data["subprojects"][0]["subprojects"][0]["id"] == T.pk`, quantities present)
  - `test_edge_endpoints_gone` (`/projects/{id}/components/` and `/projects/{id}/parts/` → 404)
  - `test_api_delete_project_with_unfinished_job_409`
  - `test_api_delete_project_cascades_subtree_204`

  In `test_api_requirements.py`: `test_requirements_has_no_variant_progress` and `test_totals_use_tree_multiplier`. In `test_api_estimation.py`: adapt `ReEstimateResolverEligibilityTests`, and add `test_re_estimate_counts_each_part_once` and `test_estimate_400_without_resolvable_preset`. In `test_api_schema.py`: the schema still generates (`spectacular --validate` style, as the existing test already does).
- [ ] **Step 2: Run** `DEBUG=1 uv run python manage.py test core.tests.test_api_projects core.tests.test_api_requirements core.tests.test_api_estimation core.tests.test_api_schema`. Expected: FAIL.
- [ ] **Step 3: Implement.** Update the module docstrings of `serializers.py`, `views.py` and `urls.py` so they describe the tree.
- [ ] **Step 4: Run the same command.** Expected: PASS.
- [ ] **Step 5: Commit** `git commit -m "feat(api)!: tree contract for projects/parts; drop edge endpoints"`.

### Task 11: Contract — drop edges, strict schema, test sweep

**Files:**
- Delete: `core/models/composition.py`, `core/tests/test_composition.py`, `test_aggregation_dag.py`, `test_api_edges.py`, `test_navigation_edges.py`, `test_print_attribution.py`
- Modify: `core/models/__init__.py` (drop the `ProjectComponent` and `ProjectPart` imports and `__all__` entries), `core/models/parts.py` (`project` non-null; remove `estimation_requested_preset`), `core/models/projects.py` (`parent` `on_delete=CASCADE`; `save()` keeps only `_assert_parent_acyclic()`, no edge seeding; the docstrings for `save`/`clean`/`_assert_parent_acyclic` no longer mention edges or the legacy FK), `core/models/printing.py` (remove `PrintJobPart.target_assembly`), `core/tests/mixins.py` and every remaining test module (remove all `ProjectPart`/`ProjectComponent` usage, and pass `project=` on every `Part.objects.create`)
- Create: `core/migrations/0031_strict_tree.py` (generated)
- Test: whole suite

**Interfaces:**
- Produces: the final schema. `Part.project` is a non-null FK with CASCADE and `related_name="parts"`. `Project.parent` uses CASCADE. The `ProjectPart` and `ProjectComponent` tables are gone, as are `PrintJobPart.target_assembly` and `Part.estimation_requested_preset`.

- [ ] **Step 1: Write failing tests.** Add to `test_models.py`:
  - `test_part_requires_project` (`Part.objects.create(name="x")` raises `IntegrityError`)
  - `test_no_edge_models_exported` (`not hasattr(core.models, "ProjectPart")` and `not hasattr(core.models, "ProjectComponent")`)
  - `test_parent_on_delete_is_cascade` (`Project._meta.get_field("parent").remote_field.on_delete is models.CASCADE`)
  - `test_project_save_rejects_cycle`

  Add to `test_migration_tree.py`: `test_full_forward_to_leaf_keeps_history`, which builds clean data at 0029, migrates to the leaf, and asserts the tree fields, the `PrintJobPart` count and that the edge tables are gone (`"core_projectpart" not in connection.introspection.table_names()`).
- [ ] **Step 2: Run** `DEBUG=1 uv run python manage.py test core.tests.test_models core.tests.test_migration_tree`. Expected: FAIL.
- [ ] **Step 3: Apply the model edits.** Run `uv run python manage.py makemigrations core --noinput --name strict_tree`. The non-interactive questioner logs "given a default of NOT PROVIDED" for `Part.project`. That is expected: 0030 has already filled every row. Check that 0031 contains `AlterField(part.project)`, `AlterField(project.parent)`, `RemoveField(printjobpart.target_assembly)`, `RemoveField(part.estimation_requested_preset)` and `DeleteModel` for both edge models, and nothing else.
- [ ] **Step 4: Sweep the tests.** Delete the five DAG-only modules. Fix every remaining module. Then run this grep gate, which must print nothing:
  ```bash
  grep -rnE "ProjectPart\b|ProjectComponent|part_links|child_links|parent_links|project_links|target_assembly|attributed_job_parts|estimation_requested_preset|containing_projects|parent_assemblies|child_modules|direct_parts\b|variant_progress|printed_quantity_for|resolve_part_preset|resolve_estimation_preset|resolve_job_preset_candidates|_resolve_presets_walk|rebuild_composition_edges|component_would_create_cycle|_memo|duplicate_as_variant|used_in\b|part_library" core layernexus --include='*.py' --include='*.html' | grep -v "core/migrations/" | grep -v "used_in_count\|used_in_projects"
  ```
- [ ] **Step 5: Run the full CI gate (Global Constraints).** Expected: all green and coverage of at least 55.
- [ ] **Step 6: Commit** `git commit -m "feat(models)!: strict project tree — drop composition edges"`, then open PR 2.

### Task 11a (hand-off, coordinator only — not an implementer step)

- [ ] Before merging PR 2, the **coordinator** takes a fresh prod DB snapshot. They run `DATABASE_PATH=<snapshot> uv run python manage.py migrate` on the PR 2 branch, then sanity-check the result:
  - 19 projects and 200 parts
  - 5 roots (24, 21, 20, 18, 11) with `parent_id IS NULL`
  - 3, 4, 5, 6, 10 and 26 have `parent_id = 24`
  - no part with `project_id IS NULL`
  - the `PrintJob` / `PrintJobPart` / `PrintJobPlate` counts are equal before and after
  - no "has no default_print_preset" warnings

  The implementer reports in the PR description that this step is pending, and does not merge.

---

## PR 3 — Deep copy

Branch: `feat/tree-rollback-3-copy`, based on `main` after PR 2 is merged.

### Task 12: `Part.deep_copy` / `Project.deep_copy`

**Files:**
- Create: `core/models/file_copy.py`
- Modify: `core/models/parts.py`, `core/models/projects.py`
- Test: `core/tests/test_deep_copy.py` (new; `@override_settings(MEDIA_ROOT=tempfile.mkdtemp())`, cleaned up in `tearDownClass`)

**Interfaces:**
- Produces:
  - `class CopiedFiles` in `core/models/file_copy.py`. It records the `(storage, name)` of every new file. `delete_all(self) -> None` removes them, ignoring `OSError`.
  - `duplicate_field_file(field_file: FieldFile, tracker: CopiedFiles) -> str | None`. It returns the new storage name, or `None` when the source is empty or missing on disk. On `FileNotFoundError`/`OSError` it logs `logger.warning("deep_copy: missing file %s", name)`. Mechanics: `with field_file.storage.open(field_file.name, "rb") as fh: new_name = field_file.storage.save(field_file.field.generate_filename(None, PurePosixPath(field_file.name).name), File(fh))`. `Storage.save` → `get_available_name` guarantees a new, unique name, so the copy never shares a path with the original.
  - `Part.deep_copy(self, *, project: Project, _files: CopiedFiles | None = None) -> Part`. It copies `name`, `quantity`, `color`, `material`, `spoolman_filament_id`, `print_preset`, `notes`, `filament_used_grams`, `filament_used_meters`, `estimated_print_time`, `estimation_status`, `estimation_error` and `estimated_with_preset`, plus `PrintTimeEstimate` rows (`printer`, `estimated_time`, `actual_time`). The STL/3MF becomes a new file. If the source `estimation_status` is `pending` or `estimating`, the copy gets `none` and `estimated_with_preset=None`, so it is never stuck. It does not copy `FileVersion` or `PrintJobPart`. A missing STL leaves `stl_file` empty, with status `none`.
  - `Project.deep_copy(self, *, parent: Project | None, name: str | None = None, created_by: User | None = None) -> Project`:
    - Raises `ValidationError({"parent": …})` when `parent.pk in self.subtree_ids()`.
    - Wraps the work in `transaction.atomic()`. On any exception it calls `tracker.delete_all()` and re-raises.
    - Takes a snapshot of `list(self.subprojects.all())` and `list(self.parts.all())` **before** creating anything.
    - Copies `description`, a new `image` file, `quantity` and `default_print_preset`. When `parent is None` and the source has no own preset, it writes `self.effective_default_print_preset` onto the new root.
    - Copies `ProjectDocument`s with new files (`name`, `uploaded_by`), and `ProjectHardware` rows (same `hardware_part`, `quantity`, `notes`).
    - Copies the parts via `Part.deep_copy(project=new, _files=tracker)`, and the subprojects recursively with `name=None` and the shared tracker. Use a private `_deep_copy_into(self, parent, name, created_by, tracker) -> Project` to carry the tracker.
    - Does not copy `HardwareShoppingItem`.
  - The name defaults to `f"{self.name} (copy)"` for the top node only. Child nodes keep their names.
- Field audit: before implementing, list `Part._meta.get_fields()` and `Project._meta.get_fields()` in the test module docstring, together with each field's copy decision. `test_all_part_fields_have_copy_decision` asserts that the set of concrete field names equals `COPIED | NOT_COPIED`, both defined in the test. A field added later then fails the test until someone decides how to copy it.

- [ ] **Step 1: Write failing tests** in `test_deep_copy.py`:
  - `test_deep_copy_recursion_depth_2_independent`: R → S → T, with parts in each; copy R as a new root. Assert the new pks differ, the structure and quantities match, editing a copied part's name leaves the original unchanged, and `Part.objects.filter(project__in=copy.subtree_ids()).count() == 3`.
  - `test_deep_copy_duplicates_files`: the STL, document and image of the copy have names that differ from the original's, both files exist, and the bytes are equal. Deleting the original's file leaves the copy's file in place.
  - `test_deep_copy_shares_hardware_catalog_not_rows`: same `hardware_part_id`, a different `ProjectHardware` pk.
  - `test_deep_copy_does_not_copy_history`: the original has a completed `PrintJobPart`, a `FileVersion` and a `HardwareShoppingItem`. The copy has 0 `job_entries`, 0 `file_versions` and 0 shopping items, and `build_progress()["printed"] == 0`.
  - `test_deep_copy_copies_estimates_and_time_estimates`.
  - `test_deep_copy_in_flight_estimation_reset_to_none`.
  - `test_subproject_copied_as_root_gets_effective_preset`: S has no own preset and R has A. `S.deep_copy(parent=None).default_print_preset == A`.
  - `test_deep_copy_into_own_subtree_raises`: `R.deep_copy(parent=T)` and `R.deep_copy(parent=R)` both raise `ValidationError`, and `Project.objects.count()` is unchanged.
  - `test_deep_copy_part_missing_stl_on_disk`: delete the file from storage first. The copy succeeds with `not copy.stl_file` and `estimation_status == "none"`. Use `assertLogs("core.models.file_copy", "WARNING")`.
  - `test_deep_copy_failure_removes_new_files`: patch `ProjectHardware.objects.create` (or `bulk_create`) to raise. The call raises, no new `Project` rows exist, and the media dir holds only the original files.
  - `test_default_copy_name` and `test_part_deep_copy_into_other_project`.
  - `test_all_part_fields_have_copy_decision`.
- [ ] **Step 2: Run** `DEBUG=1 uv run python manage.py test core.tests.test_deep_copy`. Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run the same command.** Expected: PASS.
- [ ] **Step 5: Commit** `git commit -m "feat(models): deep copy for projects and parts (content, never history)"`.

### Task 13: Copy UI for projects and parts

**Files:**
- Modify: `core/forms/projects.py`, `core/forms/parts.py`, `core/forms/__init__.py`, `core/views/projects.py`, `core/views/parts.py`, `core/views/helpers.py`, `core/views/__init__.py`, `core/urls/projects.py`, `core/urls/parts.py`, `core/templates/core/project_detail.html` (header "Copy" button), `part_detail.html` ("Copy" button)
- Create: `core/templates/core/project_copy.html`, `core/templates/core/part_copy.html`
- Test: `core/tests/test_views_copy.py` (new)

**Interfaces:**
- Consumes: `Project.deep_copy`, `Part.deep_copy`, `Project.subtree_ids`, `Project.path_label`.
- Produces:
  - `ProjectCopyForm(forms.Form)` with `__init__(self, *args, source: Project, **kwargs)`. Fields: `name: CharField(max_length=255)`, initial `f"{source.name} (copy)"`; `target: ModelChoiceField(required=False, empty_label="As new top-level project")`, with the queryset `Project.objects.exclude(pk__in=source.subtree_ids()).order_by("name")` and the label `path_label`.
  - `PartCopyForm(forms.Form)`: `project: ModelChoiceField(queryset=Project.objects.order_by("name"))`, initial = the source project.
  - `_requeue_stale_estimates(parts: Iterable[Part]) -> int` in `core/views/helpers.py`. For each part where `part.effective_print_preset_id != part.estimated_with_preset_id` and `part.stl_file` is set, it clears the estimate fields and calls `_trigger_part_estimation`. It returns the count.
  - `ProjectCopyView(ProjectManageMixin, FormView)`: URL `projects/<int:pk>/copy/`, name `project_copy`, template `project_copy.html`. On a valid POST it calls `source.deep_copy(parent=target, name=name, created_by=request.user)`, then `_requeue_stale_estimates(Part.objects.filter(project_id__in=new.subtree_ids()))`, posts a success message, and redirects to the new project's detail page.
  - `PartCopyView(ProjectManageMixin, FormView)`: URL `parts/<int:pk>/copy/`, name `part_copy`. It calls `source.deep_copy(project=target)`, then `_requeue_stale_estimates([new])`, then redirects to the new part's `part_detail`.

- [ ] **Step 1: Write failing tests** in `test_views_copy.py`:
  - `test_copy_get_prefills_name_and_excludes_subtree` (the context form's `target` queryset excludes R, S and T, and includes the other root)
  - `test_copy_as_new_root`
  - `test_copy_into_other_project_as_subproject`
  - `test_copy_post_with_target_in_subtree_rejected` (POST `target=T.pk` → 200 with a form error, and the project count is unchanged)
  - `test_copy_requeues_only_parts_with_changed_preset` (patch `core.views.helpers._start_orcaslicer_worker`; S has no preset, and copying it into root B with preset Y → its parts become `pending`; copying it into a root with the same preset → statuses unchanged)
  - `test_part_copy_into_other_project`
  - `test_copy_forbidden_for_operator` (403 for both URLs)
  - `test_copy_button_visible_for_designer_only`
- [ ] **Step 2: Run** `DEBUG=1 uv run python manage.py test core.tests.test_views_copy`. Expected: FAIL.
- [ ] **Step 3: Implement.** The templates extend `base.html`, use Bootstrap form markup and contain no inline JS or CSS.
- [ ] **Step 4: Run the same command.** Expected: PASS.
- [ ] **Step 5: Commit** `git commit -m "feat(ui): copy project/sub-project/part"`.

### Task 14: "Add existing" as copy

**Files:**
- Modify: `core/forms/projects.py`, `core/views/projects.py`, `core/urls/projects.py`, `core/templates/core/project_detail.html` (two "Add existing …" buttons + two modals)
- Create: `static/js/picker-filter.js` (a small vanilla client-side filter over `<option>` text, wired up through `data-` attributes and loaded in `{% block extra_js %}`; same pattern as `static/js/printer-filter.js`)
- Test: `core/tests/test_views_copy.py`

**Interfaces:**
- Consumes: Task 12 and Task 13 (`_requeue_stale_estimates`).
- Produces:
  - `AddExistingPartForm(forms.Form)`: `part: ModelChoiceField(queryset=Part.objects.select_related("project").order_by("project__name", "name"))`. The label is `f"{part.project.path_label} › {part.name}"`.
  - `AddExistingSubprojectForm(forms.Form)` with `__init__(self, *args, project: Project, **kwargs)`: `source: ModelChoiceField(queryset=Project.objects.exclude(pk__in=project.subtree_ids()).order_by("name"))`, labelled by `path_label`. The subtree excluded here is the current project's own.
  - `ProjectAddExistingPartView(ProjectManageMixin, View)`: POST only, URL `projects/<int:pk>/parts/add-existing/`, name `project_add_existing_part`. It calls `part.deep_copy(project=project)` and then `_requeue_stale_estimates`.
  - `ProjectAddExistingSubprojectView(ProjectManageMixin, View)`: POST only, URL `projects/<int:pk>/subprojects/add-existing/`, name `project_add_existing_subproject`. It calls `source.deep_copy(parent=project, name=source.name)` and then requeues. A `ValidationError` from `deep_copy` becomes `messages.error` plus a redirect.

- [ ] **Step 1: Write failing tests:**
  - `test_add_existing_part_copies_and_leaves_original` (a new part in the target, the original's `project_id` unchanged, `Part.objects.count()` + 1)
  - `test_add_existing_subproject_copies_subtree` (the original stays under its old parent)
  - `test_add_existing_subproject_choices_exclude_own_subtree`
  - `test_add_existing_subproject_forged_ancestor_rejected` (POST `source` = an ancestor of the current project → this is a subtree-into-itself case → error message, no new rows)
  - `test_add_existing_forbidden_for_operator`
- [ ] **Step 2: Run** `DEBUG=1 uv run python manage.py test core.tests.test_views_copy`. Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run the same command.** Expected: PASS.
- [ ] **Step 5: Commit** `git commit -m "feat(ui): add existing part/sub-project as a deep copy"`.

### Task 15: API copy endpoints

**Files:**
- Modify: `core/api/views.py`
- Test: `core/tests/test_api_copy.py` (new)

**Interfaces:**
- Consumes: `Project.deep_copy`, `Part.deep_copy`, `_requeue_stale_estimates`.
- Produces:
  - `ProjectViewSet.copy` (`@action(detail=True, methods=["post"])`). Body `{name?: str, parent?: int|null}`. Returns 201 with `ProjectSerializer`. Returns 400 for an unknown parent or a parent in its own subtree.
  - `PartViewSet.copy`. Body `{project: int}` (required). Returns 201 with `PartSerializer` and 400 when the project is missing or unknown.
  - Both have `@extend_schema` with `inline_serializer` request schemas named `ProjectCopyRequest` / `PartCopyRequest`. Both require `can_manage_projects` through `ReadOrProjectManage`.

- [ ] **Step 1: Write failing tests:**
  - `test_api_copy_project_as_root_default_name` (`"(copy)"` suffix)
  - `test_api_copy_project_into_parent`
  - `test_api_copy_into_own_subtree_400`
  - `test_api_copy_part_requires_project_400`
  - `test_api_copy_part_201`
  - `test_api_copy_forbidden_read_only_token` (403)
  - `test_schema_lists_copy_endpoints`
- [ ] **Step 2: Run** `DEBUG=1 uv run python manage.py test core.tests.test_api_copy`. Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run the full CI gate.** Expected: green.
- [ ] **Step 5: Commit** `git commit -m "feat(api): copy endpoints for projects and parts"`, then open PR 3.

---

## PR 4 — Move and file cleanup

Base: main after PR 3 has merged. Spec sections: "Move" and "File cleanup".

### Task 16: Move parts and sub-projects in the web UI

**Files:**
- Modify: `core/forms/parts.py` (`PartForm`), `core/forms/projects.py` (`SubProjectForm`, `ProjectEditForm`), `core/views/parts.py` (`PartUpdateView`), `core/views/projects.py` (`ProjectUpdateView`), `part_form.html`, `project_form.html`
- Test: `core/tests/test_views_move.py` (new)

**Interfaces:**
- Consumes: `Project.subtree_ids`, `Project.path_label`, `Project.effective_default_print_preset`, `_requeue_stale_estimates` (Task 13), and the cycle check used by `ProjectSerializer.validate` (Task 10).
- Produces:
  - `PartForm` gets a `project` field, but only on update: `ModelChoiceField(queryset=Project.objects.order_by("name"))`, labelled with `path_label`. On create, the project still comes from the URL.
  - `ProjectEditForm` gets a `parent` field: `ModelChoiceField(required=False, empty_label="(top-level project)")`. Its queryset excludes `instance.subtree_ids()`.
  - `ProjectEditForm.clean()` raises a `ValidationError` on `default_print_preset` when `parent` is empty and neither the form nor the instance provides a preset.

- [ ] **Step 1: Write failing tests** in `test_views_move.py`:
  - `test_move_part_to_other_project_keeps_print_history` (the `PrintJobPart` count for the part is unchanged and `part.project` is the new project)
  - `test_move_subproject_to_other_parent`
  - `test_move_into_own_subtree_rejected` (the form error is on `parent`; the DB is unchanged)
  - `test_move_subproject_to_root_requires_preset` (no own preset and an empty preset field → form error; with a preset → OK)
  - `test_move_root_under_other_project` (it becomes a sub-project and its preset may stay)
  - `test_move_requeues_parts_with_changed_preset` (patch `core.views.helpers._start_orcaslicer_worker`)
  - `test_move_forbidden_for_operator` (403)
- [ ] **Step 2: Run** `DEBUG=1 uv run python manage.py test core.tests.test_views_move`. Expected: FAIL.
- [ ] **Step 3: Implement.** After a successful save in `form_valid`, if `project`/`parent` changed, call `_requeue_stale_estimates` on the moved part, or on `Part.objects.filter(project_id__in=obj.subtree_ids())` for a project.
- [ ] **Step 4: Run** the same command. Expected: PASS. Then run the full suite: `DEBUG=1 uv run python manage.py test core`.
- [ ] **Step 5: Commit** `git commit -m "feat(ui): move parts and sub-projects between projects"`.

### Task 17: Delete media files with their rows + `cleanup_orphan_media`

**Files:**
- Create: `core/signals.py` (or extend the existing signal module if `core/apps.py` already wires one), `core/management/commands/cleanup_orphan_media.py`
- Modify: `core/apps.py` (`ready()` imports the signals), `core/views/parts.py` / `core/views/projects.py` only if file replacement on edit is not covered by signals
- Test: `core/tests/test_media_cleanup.py` (new)

**Interfaces:**
- Produces:
  - `MEDIA_FIELDS: list[tuple[type[Model], str]] = [(Part, "stl_file"), (FileVersion, "file"), (ProjectDocument, "file"), (Project, "image")]` in `core/signals.py`.
  - `file_is_referenced(name: str) -> bool`: True if any row in any `MEDIA_FIELDS` field still has this storage name.
  - `post_delete` receivers for each model in `MEDIA_FIELDS`: `transaction.on_commit(lambda: _delete_if_unreferenced(storage, name))`.
  - `pre_save` receivers for `Part.stl_file` and `Project.image`: when the stored name changes, delete the old file on commit if it is unreferenced. A `FileVersion` that keeps the old path counts as a reference.
  - Command `cleanup_orphan_media [--delete]`. It walks the `upload_to` directories of `MEDIA_FIELDS` under `MEDIA_ROOT`. It prints each orphan path and then `"N orphaned files, X MB"`, and deletes only with `--delete`. Exit code 0.

- [ ] **Step 1: Write failing tests** in `test_media_cleanup.py`. Use `override_settings(MEDIA_ROOT=tmpdir)`, a `TestCase` with `captureOnCommitCallbacks(execute=True)`, and a `TransactionTestCase` for the rollback case:
  - `test_deleting_part_removes_stl_after_commit`
  - `test_subtree_delete_removes_all_files` (part STL, document, project image)
  - `test_file_shared_with_fileversion_is_kept`
  - `test_rolled_back_delete_keeps_file`
  - `test_replacing_stl_removes_old_file_unless_versioned`
  - `test_print_job_gcode_untouched_on_part_delete`
  - `test_cleanup_command_dry_run_lists_and_keeps` (output contains the orphan path; the file still exists)
  - `test_cleanup_command_delete_removes_only_orphans`
- [ ] **Step 2: Run** `DEBUG=1 uv run python manage.py test core.tests.test_media_cleanup`. Expected: FAIL.
- [ ] **Step 3: Implement** using the signatures above. Check the actual `upload_to` values in `core/models` and do not guess the directory names.
- [ ] **Step 4: Run** the same command. Expected: PASS. Then run the full suite: `DEBUG=1 uv run python manage.py test core`.
- [ ] **Step 5: Commit** `git commit -m "feat: delete media files with their rows; cleanup_orphan_media command"`. Open PR 4. Hand-off note for the coordinator: after deploying, run `cleanup_orphan_media` on prod as a dry run, review the output, then run it with `--delete`.

## PR 5 — Docs and leftovers

Branch: `docs/tree-rollback-4`.

### Task 18: Docs, superseded plans, stale-comment sweep

**Files:**
- Modify: `README.md` (features + the API section: the table from line 240 onward, the prose near line 272 and the curl example near lines 279–304), `docs/user-guide/projects.md` (preset inheritance, copy, delete with blocking jobs, no shared parts), `CLAUDE.md` (models list: make sure no `composition.py`; keep the aggregation-pattern line), `docs/superpowers/specs/2026-09-20-project-variants-dag-composition-design.md` and `docs/superpowers/specs/2026-09-22-project-preset-resolution-design.md` (add the banner `> **Superseded by** \`2026-10-28-tree-rollback-design.md\`.` on the first line under the title)
- Delete: `docs/superpowers/plans/2026-09-20-project-variants-phase*.md` (7 files), `docs/superpowers/plans/2026-09-22-project-preset-resolution.md`
- Test: `core/tests/test_deployment_config.py` (only if it checks the README); otherwise the docs gate is `ruff format --check` (Markdown code blocks) plus a grep

**Interfaces:** none.

- [ ] **Step 1: Update the README API table.** Remove the `components` and `/projects/{id}/parts/` rows and the `duplicate` row. Add `POST /projects/{id}/copy/ {name?, parent?}` and `POST /parts/{id}/copy/ {project}`. Note that `parts` require `project`, and `projects` accept `parent` and `quantity`, with the preset required only for roots. Drop `variant_progress` from `requirements`. In the curl example, replace the `components/` call with `-d "parent=$ASM&quantity=1"` on `POST /projects/`, and replace step 6 with a `copy` call. The `project=…&quantity=…` part example becomes correct as written.
- [ ] **Step 2: Rewrite the user guide.** `docs/user-guide/projects.md` lines 16–38 describe bottom-up preset inheritance (only top-level projects require a preset; "inherited from parent"). Add the sections "Copying projects and parts", "Moving parts and sub-projects" and "Deleting a project" (subtree, counts, blocked by unfinished jobs, finished history kept, files removed). Mention `manage.py cleanup_orphan_media [--delete]` in README admin/maintenance notes.
- [ ] **Step 3: Run the stale-comment grep, which must print nothing.**
  ```bash
  grep -rnE "composition edge|Phase[ -][1-6]|Phase-[1-6]|legacy FK|legacy .parent|DAG|Variant B|build path" core --include='*.py' --include='*.html' | grep -v "core/migrations/"
  ```
  Fix each hit in the docstrings and comments.
- [ ] **Step 4: Run** `uv run ruff format --check . && DEBUG=1 uv run python manage.py test core`. Expected: green.
- [ ] **Step 5: Commit** `git commit -m "docs: tree rollback — README, user guide, superseded DAG docs"`, then open PR 5.

---

## Self-review notes (resolved inline)

- **Spec coverage:** every Goal (1–7), each Removal-list bullet and each Tests bullet maps to Tasks 1–16. "Kept" fields (`PrintJob.print_preset`, `Part.estimated_with_preset`, `HardwareShoppingItem`) are untouched.
- **Spec deviations and decisions recorded here:**
  - (a) `_ProjectScopedMixin` is kept: the documents and hardware endpoints still use it.
  - (b) `Part` has no thumbnail field, so the spec's "STL/3MF file and its thumbnail" copies only the model file.
  - (c) The spec only names `ProjectDeleteView`, but the API `DELETE /projects/{id}/` now enforces the same unfinished-job block (409). Otherwise CASCADE would bypass the owner's decision.
  - (d) In-flight estimation statuses are reset to `none` on copy, so `_requeue_stale_estimates` re-queues them.
  - (e) The subtree delete guard ships in PR 2, together with CASCADE.
- **Type consistency:** `subtree_ids()`, `path_label`, `blocking_jobs()`, `deletion_summary()`, `build_progress()`, `_requeue_stale_estimates()` and `duplicate_field_file()` are each defined once (Tasks 5, 6, 13, 12) and used with the same names in later tasks.
