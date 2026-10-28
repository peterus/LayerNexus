# Back to a strict project tree (roll back the n:m composition)

Status: draft, 2026-10-28
Supersedes: `2026-09-20-project-variants-dag-composition-design.md`,
`2026-09-22-project-preset-resolution-design.md` (per-build-path part)

## Why

In September the project model moved from a simple tree to an n:m graph. A part
could be linked into several projects (`ProjectPart`), and a sub-project could
sit under several assemblies (`ProjectComponent`). The goal was to reuse one
part or module across projects.

In daily use this caused more trouble than it saved:

- **Management became unwieldy.** Parts live in a separate library and have to
  be attached to a project. Every page shows "Used in" lists. Editing a shared
  part silently changes every project that uses it.
- **Print management became ambiguous.** Once part A is printed, which project
  does the print belong to, and which top-level project? The code needs
  `PrintJobPart.target_assembly`, per-variant progress, per-build-path preset
  resolution and a preset-ambiguity dropdown just to answer that. Several real
  bugs came out of this machinery: double re-estimate queueing (#50/#51), the
  `Project.save()` edge corruptor (#53), and a 500 on project delete via the
  stale legacy `parent` FK.

The owner (sole user) decided: **every part belongs to exactly one project, and
every sub-project has exactly one parent.** Reuse happens by **deep copy**.
After copying, the copy is fully independent of the original.

## Goals

1. A strict tree:
   - `Part` → exactly one `Project` (with a quantity);
   - `Project` → at most one parent `Project` (with a quantity);
   - top-level projects have no parent.
2. A recursive **deep copy** of a project or sub-project, and of a single part.
3. "Add existing part / sub-project" to a project = **deep copy** of the
   selection into that project. The original stays where it is.
4. **Print preset resolution bottom-up**, as before the DAG:
   1. the part's own `print_preset`;
   2. else the nearest ancestor project with a `default_print_preset`, walking
      `parent` up to the root.

   Only **top-level** projects must have a default preset. Sub-projects and
   parts may leave it empty.
5. **Deleting a project deletes its whole subtree** (sub-projects, parts,
   documents, hardware assignments). A confirmation page first lists what goes
   with it.
6. **Remove everything that only existed for the DAG**, in code and in the
   database (tables, columns, migrations-generated schema). See "Removal list".
7. Production data migrates without loss. Prod was cleaned up on 2026-10-28,
   and nothing there is shared any more.
8. **Move** (added 2026-10-28): a part or sub-project can be re-parented in
   the web UI, keeping its print history (see "Move").
9. **No orphaned files** (added 2026-10-28): deleting rows deletes their media
   files, and a one-off command cleans up existing orphans (see "File
   cleanup").

## Non-goals

- No backwards compatibility for the Build API (`/api/v1/`). The owner
  confirmed nothing depends on the current contract. Breaking changes are fine.
- No "linked copy" or "update copies from original" feature. Copies are
  independent forever.
- No "archive project" feature. It is a good idea (hide finished projects
  while keeping their print history) but deliberately deferred to a separate
  feature.
- No change to the print queue, scheduled start, KlipperScreen, slicing or
  Spoolman logic beyond what is needed to drop `target_assembly`.

## Anti-goals (this would be a failure even if it "works")

- Losing or re-attributing existing print history (`PrintJob`, `PrintJobPart`)
  on prod during the migration.
- A copy that still shares anything mutable with its original: part rows, files
  on disk, documents, or hardware assignment rows. The shared hardware
  **catalog** (`HardwarePart`) is intentionally the exception.
- Any leftover DAG code path: dead helpers, multi-path dedup, ambiguity
  handling, "used in" UI, or edge tables left in the schema.

## Current production data (verified 2026-10-28)

19 projects, 200 parts. **0 parts in more than one project, 0 projects with
more than one parent.** Top-level projects:

- 24 "Truck mit Anhängerkupplung" (actively being printed)
- 21
- 20
- 18
- 11

The legacy `Project.parent` column is **not reliable**. Edges added through the
assembly editor or API never set it. For prod it was hand-corrected on
2026-10-28 for 3/4/5/6/10/26 → 24, but the migration must not trust it.
Backup: `/root/LayerNexus/backups/db.backup-20261028-162311-precleanup.sqlite3`.

## Decided design

### Data model

- `Part.project`: FK → `Project`, **non-null**, `on_delete=CASCADE`,
  `related_name="parts"`.
- `Part.quantity`: `PositiveIntegerField(default=1)`, ≥1 constraint.
- `Project.parent`: FK → self, nullable, **`on_delete=CASCADE`** (was PROTECT),
  `related_name="subprojects"`.
- `Project.quantity`: kept, ≥1. It means how many of this sub-project the
  parent needs, and it is ignored on top-level projects.
- Cycle protection stays, as a simple walk up the single `parent` chain
  (`_assert_parent_acyclic` style). It also runs in the "move/re-parent" paths
  if any exist.
- **Removed models / fields:**
  - `ProjectPart`
  - `ProjectComponent`
  - `PrintJobPart.target_assembly`
  - `Part.estimation_requested_preset`. This was a transient channel for
    per-build-path re-estimates; with one parent chain the preset is always
    derivable.
- **Kept:**
  - `PrintJob.print_preset` — one preset per job, not DAG-specific.
  - `Part.estimated_with_preset`
  - `HardwareShoppingItem`

### Migration (one or more generated migrations, data step via RunPython)

1. Add `Part.project` (nullable for now) and `Part.quantity`.
2. RunPython, forward:
   - **Precondition:** abort with a clear error if any part has more than one
     `ProjectPart`, or any project has more than one `ProjectComponent` parent
     edge. Prod is clean, so this only protects other installs.
   - **Parts:** for every `ProjectPart`, set `part.project` and
     `part.quantity = edge.quantity`.
   - **Projects:** for every project, set `parent` and `quantity` from its
     single parent edge, or `parent=None` if it has none. This overwrites the
     unreliable legacy values.
   - **Parts without an edge:** none on prod. The migration must still handle
     them deterministically. My call: abort with a list. A partless orphan
     cannot be placed.
   - Reverse: a no-op or best-effort edge rebuild is acceptable. The prod
     backup is the real rollback path.
3. Make `Part.project` non-null. Change `Project.parent` to CASCADE.
4. Remove `PrintJobPart.target_assembly`, `Part.estimation_requested_preset`,
   and the `ProjectPart` / `ProjectComponent` models.

Generate everything with `makemigrations`, plus the one RunPython. No
hand-edited schema operations, per the repo rules.

### Aggregation (back to the simple tree shape)

All recursive aggregation walks `subprojects` with the quantity multiplier:

- parts
- hardware
- documents
- filament
- totals
- status
- the shopping list (`hardware_requirements`)
- `get_descendant_ids`

The reference shape is the original `_collect_parts_with_multiplier()` before
#34. **Remove** the memo/path-dedup machinery and the per-path tuples.
`needed = part.quantity × product of sub-project quantities up the chain`.

Printed progress becomes simple again: per part, "printed X of N", where X
comes from all its completed `PrintJobPart`s and needs no attribution. The
project build-progress card (#47, collapsible) stays, but it is computed from
this simple per-part progress.

### Print preset resolution

`Part.effective_print_preset`:

1. `self.print_preset`;
2. else walk `project → parent → …` and return the first
   `default_print_preset`;
3. else `None`. This only happens with bad data, because roots are required to
   have one.

This single function replaces all of the following:

- `resolve_part_preset`
- `resolve_part_presets`
- `resolve_estimation_preset(_map)`
- `_resolve_presets_walk`
- `resolve_job_preset_candidates`
- `printed_quantity_for_by_preset`
- all "ambiguous" branches, including the worker's "Preset ambiguous across
  projects" error and the preset-choice dropdown on the part page.

`Project.effective_default_print_preset` stays and walks up the chain, as it
does today.

Forms and API:

- **Top-level** project create/edit: `default_print_preset` is required.
- Sub-project create/edit and parts: optional, with the help text "inherited
  from parent".
- API `ProjectSerializer`: required only when `parent` is null, validated in
  `validate()`.

`CreateJobsFromProjectView` keeps grouping jobs by `(preset, filament)`. The
preset now comes from `effective_print_preset`.

### Deep copy

`Project.deep_copy(*, parent: Project | None, name: str | None, created_by) -> Project`
is recursive and runs in one `transaction.atomic()`.

What gets copied for the project node:

- name (overridable for the top node only), description, image (new file),
  default preset, quantity;
- `parent` = the given target, or `None` to make a new top-level project;
- `ProjectDocument`s, each with a **new file copy**;
- `ProjectHardware` rows, pointing to the same `HardwarePart` catalog entries;
- every part via `Part.deep_copy(project=new_project)`;
- every sub-project via a recursive `deep_copy(parent=new_project, name=None)`.

`Part.deep_copy(*, project) -> Part` copies:

- all scalar fields (name, quantity, material, colour, Spoolman filament,
  `print_preset`, notes, …);
- the STL/3MF file and its thumbnail as **new files**;
- the estimate fields (`estimated_print_time`, `filament_used_grams`,
  estimation status, `estimated_with_preset`) and its `PrintTimeEstimate` rows,
  so no re-slicing is needed.

What is **not** copied:

- print history (`PrintJobPart` / jobs);
- `HardwareShoppingItem` status;
- `FileVersion` history. My call: the copy starts with its current file only.

The builder must check the `Part` model for any field added since this spec was
written and decide per field. The rule is "copy content, never history".

Presets and estimates when copying (my call):

- When a sub-project is copied **as a new top-level project** and has no
  preset of its own, its *effective* preset (inherited from the original
  chain) is written onto the new root. This keeps the "roots must have a
  preset" rule.
- After the copy, every copied part whose new `effective_print_preset`
  differs from its copied `estimated_with_preset` is queued for
  re-estimation, using the existing estimation trigger. Parts whose preset is
  unchanged keep their copied estimates.

UI:

- A **"Copy" button** on the project detail page, for top-level and
  sub-projects. It opens a form with two fields:
  - new name, default "<name> (Kopie)" / "(copy)" to match the UI language;
  - target: "as new top-level project" or "into project …". This is a select
    over all projects, excluding the copied subtree itself.
- A **"Copy" action on a part**, with the target project as a select.
- **"Add existing part" / "Add existing sub-project"** on the project detail
  page. They replace the current picker modals, which attach edges. Each is a
  searchable picker over all parts or projects (excluding the current project's
  own subtree for sub-projects). Choosing an item runs the corresponding
  `deep_copy` into the current project.
- RBAC: `ProjectManageMixin` on every copy/add view.

### Delete

- `ProjectDeleteView` deletes the subtree. CASCADE on `parent` and
  `Part.project` makes this the DB behaviour.
- The confirmation page lists the counts that will go: sub-projects, parts,
  documents, hardware assignments. If any part in the subtree has print
  history, it warns that this history will be deleted too.
- **Blocked while unfinished jobs exist** (decided by the owner).
  - If any part in the subtree is in a `PrintJob` whose status is not
    finished, the confirmation page refuses the delete. "Not finished" means
    anything other than completed, failed or cancelled; the builder maps this
    to the actual status enum. Draft, sliced, queued and printing jobs all
    count.
  - The page lists the blocking jobs with links, so the user can finish or
    remove them first.
  - The POST must enforce the same check again inside the transaction, not
    only the GET page.
- **Finished jobs are kept** as history. They only lose the `PrintJobPart` rows
  of the deleted parts (CASCADE). A finished job with no parts left stays as
  a row, together with its plates, G-code and stats.
- Success redirect: to the parent project if there is one, else to the project
  list.
- No more 500 from `ProtectedError`.

### Move (added 2026-10-28)

- The part edit form gets a "project" field, a select over all projects with
  the `path_label` label. The sub-project edit form gets a "parent" field: a
  select over all projects except its own subtree. A top-level project's edit
  form shows "parent" too, but it is optional, so a root can become a
  sub-project. Moving a sub-project to "no parent" makes it a root, and then a
  `default_print_preset` is required, either its own or the one entered in
  the same form.
- Validation: a project can never be moved into its own subtree. This is the
  same cycle check as the API.
- Print history and files stay with the moved row. Nothing is copied.
- After a move, parts whose `effective_print_preset` changed are re-queued
  for estimation. This is the same helper as for copy
  (`_requeue_stale_estimates`).
- RBAC: `ProjectManageMixin`, as on the existing edit views.
- The API already allows re-parenting via `PATCH parent` / `PATCH project`,
  with the same validation and re-estimate behaviour.

### File cleanup (added 2026-10-28)

Media files covered:

- `Part.stl_file`
- `FileVersion.file`
- `ProjectDocument.file`
- `Project.image`

`PrintJob` / plate files (`gcode_file`, plate `thumbnail`) belong to the kept
job history and are **not** touched, unless their own row is deleted.

- **On delete:** when one of these rows is deleted, including through CASCADE
  from a subtree delete, its file is removed from storage
  **after the transaction commits** (`transaction.on_commit`), and only if no
  remaining row in any of these fields references the same storage name. A
  `FileVersion` may share a path with its part's `stl_file`. Replacing a file
  on edit (new STL upload, new image) also removes the old file under the same
  rule, unless it is kept as a `FileVersion`.
- **One-off command** `manage.py cleanup_orphan_media`:
  - scans the upload directories of the fields above (`stl_files/`,
    `file_versions/`, `project_documents/`, `project_images/`, and whatever
    `upload_to` values the code actually uses);
  - lists files that no DB row references;
  - **dry-run by default.** It prints the paths and the total size, and
    deletes only with `--delete`.
  - The coordinator runs it on prod after the deploy: dry-run first, then
    `--delete`.
- Tests:
  - delete a part, a project subtree and a document, and check that the files
    are gone after commit;
  - a shared storage name is kept;
  - a rolled-back delete keeps its files;
  - the command finds orphans, the dry run deletes nothing, and `--delete`
    removes only the orphans.

### Print jobs

- Remove `target_assembly` everywhere: the `AddPartToJobView` "Build target"
  select, the `_resolve_target_assembly` helper, and `CreateJobsFromProjectView`
  pinning.
- The `printjob_detail` "Project" column shows `part.project`. My call: show
  the root project too, e.g. "Truck mit AHK › Double axle module".
- `PrintJobListView` project filter (#79):
  - choices are **top-level projects**;
  - a job matches when any of its parts lies in the selected project's subtree;
  - remembered-filter session values that point to a now-non-root or missing
    project are dropped silently.

### Build API (`/api/v1/`) — breaking, no compatibility needed

- `PartSerializer`: add `project` (required on create) and `quantity`.
- `ProjectSerializer`: add `parent` (nullable) and `quantity`. Preset is
  required only for roots. Cycle check on re-parent.
- **Remove:**
  - `/projects/{id}/components/` (+detail)
  - `/projects/{id}/parts/` (+detail)
  - `ProjectComponentSerializer`, `ProjectPartEdgeSerializer`
  - `_ProjectScopedMixin`, if it becomes unused
  - `variant_progress` in `requirements`
- **Replace** `POST /projects/{id}/duplicate/` with
  `POST /projects/{id}/copy/ {name?, parent?}`.
- **Add** `POST /parts/{id}/copy/ {project}`.
- Keep `tree` (now a plain recursive tree), `requirements`, `validate` (no
  dedup), `re-estimate`, search, `estimate` and `stl`.
- Update the README API section, including its examples. Its stale
  `project=…&quantity=…` example becomes correct again.

### Removal list (code)

Delete outright:

- `core/models/composition.py` and its re-exports;
- `rebuild_composition_edges`;
- the `post_delete` legacy-parent receiver;
- `Project.save()` edge seeding. Keep only the acyclic check.
- Parts library:
  - `PartLibraryListView`
  - the `part_library` URL
  - `parts_library.html`
  - the nav link in `base.html`
  - the breadcrumbs pointing to it. These now point to the part's project.
- "Used in":
  - `_used_in.html` and all 12 includes
  - `used_in` context in every view
  - `parent_assemblies`
  - `Part.containing_projects`
  - `PartDetailView._candidate_assemblies` and the `part_usage` table
  - the "used in N assemblies" delete warnings
- Edge editing:
  - `AddComponentForm`, `AddPartToProjectForm`
  - `ProjectComponentQuantityForm`, `ProjectPartQuantityForm`
  - the six edge write views and their URLs
  - the inline edge quantity/remove forms in `project_detail.html`

  Quantity is edited on the part or sub-project edit form again. Remove means
  delete.
- `duplicate_as_variant`, `ProjectDuplicateAsVariantView`,
  `project_duplicate.html` and the `project_duplicate` URL. All are replaced by
  copy.
- Variant progress and attribution:
  - `variant_progress`, `printed_quantity_for*`, `_fully_printed_entry_pks`
    filters
  - `_jsonify_variant_progress`
  - `build_progress_expanded` attribution query (rewritten simple)
- Multi-path dedup:
  - re-estimate dedup (views and API)
  - API `validate` dedup
  - `filament_requirements` `distinct_parts`
  - `printed_parts_count` per-path cap
  - `ProjectListView.distinct()`
  - `_memo` / `_path` guards
- Preset ambiguity machinery (see above) and `estimation_requested_preset`
  handling in the views, API and `slicing_worker`.
- `ProjectAdmin.exclude = ("parent", "quantity")`. Show them again.
- Stale docstrings and comments that mention edges, Phase 1–6, or "legacy FK".

Rewrite to the tree:

- `aggregate_prefetch_lookups` / `hardware_prefetch_lookups`, using
  `parts` / `subprojects` chains;
- `dashboard.py` stats, which use `ProjectPart` / `project_links` today;
- `admin_dashboard.html`;
- `ProjectListView` top-level filter (`parent__isnull=True`);
- `SubProjectCreateView`;
- `PartCreateView` (sets `project` + `quantity` from the form; the URL is still
  nested under the project);
- `PartForm` (quantity field back);
- `SubProjectForm` / `ProjectEditForm` (quantity on sub-projects; preset
  optional for sub-projects);
- the shopping list "Used in" column (`ShoppingRow.projects`, which is now the
  tree path names).

### Removal list (tests, docs)

- Delete the DAG-only test modules:
  - `test_composition`
  - `test_aggregation_dag`
  - `test_api_edges`
  - `test_navigation_edges`
  - `test_variant_duplicate`
  - `test_print_attribution`
- Rewrite `test_preset_resolution` for bottom-up resolution.
- Adapt the remaining modules and the shared fixture `core/tests/mixins.py`,
  which today uses `ProjectPart.objects.create`, to `Part(project=…,
  quantity=…)`.
- Add tests:
  - migration data step: clean data, plus abort on shared data;
  - `deep_copy`: independence, files duplicated, history not copied, recursion
    depth ≥ 2;
  - add-existing-as-copy;
  - subtree delete and its confirmation counts;
  - bottom-up preset resolution, plus "root preset required";
  - print-job project filter by subtree;
  - API copy endpoints and the create contract.
- Docs:
  - update `README.md` (features and API);
  - update `docs/user-guide/projects.md` (preset inheritance, copy, delete);
  - update `CLAUDE.md` (models list: drop `composition`; the aggregation
    pattern stays valid).
- Delete the obsolete DAG plans:
  - `docs/superpowers/plans/2026-09-20-project-variants-phase*.md`
  - `2026-09-22-project-preset-resolution.md`

  Add a "Superseded by 2026-10-28-tree-rollback-design.md" banner to the two
  superseded specs instead of deleting them. My call: the specs explain why the
  DAG existed; git keeps the plans.

## Constraints

- Repo conventions from `CLAUDE.md`:
  - RBAC mixins on every write view;
  - double quotes, type hints, Google docstrings;
  - CBVs only;
  - migrations only via `makemigrations` (plus the RunPython data step);
  - ruff 0.15.20;
  - coverage ≥ 55 %.
- Prod is in active use: project 24 is mid-print. The deploy follows the
  coordinator's prod procedure: DB backup → pull → up. The migration must be
  verified against a copy of the current prod DB before the PR is merged.
  - The builder has no prod access. The **coordinator** provides a fresh
    prod DB snapshot and runs `migrate` plus a sanity check (counts, tree
    shape, print history intact) against it before merging.
  - All 5 prod roots already have a `default_print_preset` (checked
    2026-10-28). The migration therefore only needs to **report** roots
    without one; it does not need to backfill them.
- One PR or a short sequence is fine. Each merged state must keep CI green and
  must not leave prod in a half-migrated schema.

## Left to the builder

- Exact PR split, for example (1) model + migration + aggregation + preset,
  (2) copy/delete UI + removals, (3) API + docs. The rule from "Constraints"
  applies.
- Picker UI details for "add existing" (search field, grouping by top-level
  project).
- Whether `Part.deep_copy` and `Project.deep_copy` live on the models or in a
  small `core/services/copy.py`. Business logic should stay near the models.
- File-copy mechanics: storage API, naming, and cleanup on a failed transaction.
