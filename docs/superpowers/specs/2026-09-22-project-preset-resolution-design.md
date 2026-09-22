# Project Preset Resolution — Design of Record

> Status: implemented (brainstormed with the user 2026-09-21/22, shipped 2026-09-22). This
> document is the single design-of-record for the "mandatory project preset + top-down
> per-build-path resolution" feature. The implementation plan is
> `docs/superpowers/plans/2026-09-22-project-preset-resolution.md`.

## Problem

Phase 6 removed the project → part print-preset fallback. Today the effective print preset
for a part is **only** `Part.print_preset`:

```python
# core/models/parts.py
@property
def effective_print_preset(self):
    return self.print_preset if self.print_preset_id else None
```

258 of 329 legacy parts have no `print_preset`. When such a part is sliced, the worker sends
`print_preset=None` to OrcaSlicer, which fails with *"incorrect slicing parameters in the
3mf"*. Concrete failure: Job 21 (part E06 / Part 82, has an STL, `print_preset` is `None`).
`Project 24` has `default_print_preset=4`, but since Phase 6 that default is **dead for parts** —
it is only used to pre-fill the field when creating a *new* part in the project.

The same `None`-preset problem exists for background **estimation**
(`_estimate_part_in_background`), because it also reads `part.effective_print_preset`.

Composition is now **edge-based** (Phase 6 removed `Part.project` and `Project.parent` FKs):

- `ProjectPart` — edge from a `Project` to a `Part` (`project.part_links` / `part.project_links`),
  carries a per-edge `quantity`.
- `ProjectComponent` — edge from a parent `Project` to a child `Project`
  (`parent.child_links` / `child.parent_links`), carries a per-edge `quantity`.

A part or module can therefore live under **multiple parents**, which is exactly why a naive
project → project preset inheritance is ambiguous.

## Design / Decisions

### No project → project fallback

We deliberately do **not** walk parent projects to find a preset (`Project.parent` no longer
exists, and a shared module has many parents — inheritance would be ambiguous). The existing
`Project.effective_default_print_preset` / `effective_default_print_preset_id` properties walk
the legacy `self.parent` chain and are now dead for this purpose; they are not used by the new
resolver.

### Top-down, per-build-path resolution (Variant B)

Resolution happens **while expanding a project DAG**, carrying the *current nearest project's
preset* down each path. At a part leaf the effective preset is:

> **`part.print_preset` (override) ELSE the `default_print_preset` of the nearest (directly
> containing) project on THIS path.**

Precedence: **part override > nearest-project preset**. Because a shared module reached via two
parents is expanded once *per path*, each build has exactly one path to each leaf, so the
resolution is unambiguous even under multi-parent composition.

### Project default preset becomes mandatory

`Project.default_print_preset` is required going forward, enforced at the **form / serializer
level** (not by a hardcoded data migration):

- `ProjectForm`, `SubProjectForm`, `ProjectEditForm` — field required.
- `ProjectSerializer` — `default_print_preset` `required=True`.

The DB column stays `null=True` for portability. A `null=False` `AlterField` is **out of
scope** here — it would only be safe after a deployment-specific data backfill. **No migration
may hardcode preset 4** (that PK is only valid in Peter's deployment).

### Data note (already done — do not repeat)

On 2026-09-21 all 24 existing projects were set to `default_print_preset=4` **via the API
(data, not code)**. The implementation must **not** include that data step and must **not**
hardcode preset 4 anywhere.

## Slicing

`CreateJobsFromProjectView` (`projects/<pk>/create-jobs/`) already groups eligible parts by
`(effective_print_preset_id, spoolman_filament_id)` so a multi-preset build yields multiple
draft jobs ("one bundle = one preset"). The only change is that grouping must key on the
**B-conformant resolved preset in the project context** (nearest containing project), not on the
bare `part.effective_print_preset_id`.

New field **`PrintJob.print_preset`** (nullable FK → `OrcaPrintPreset`): set at job creation to
the resolved group preset. `_slice_job_in_background` slices with `job.print_preset` instead of
re-reading `first_part.effective_print_preset`. This needs a small schema-only migration.

## Job entry points (UX unchanged)

Two entry points remain:

1. **Project path** — `CreateJobsFromProjectView`. Normal case; groups by the resolved
   `(preset, filament)` in the project context. No UI change.
2. **Part path** — `AddPartToJobView` (from the part detail "Add to Job" form). Preset
   resolution for the standalone part:
   - `part.print_preset` override, ELSE
   - exactly **1** containing project → that project's `default_print_preset`, ELSE
   - multiple containing projects, all with the **same** default preset → that one, ELSE
   - multiple containing projects with **different** default presets and no override →
     **dropdown** to choose among the distinct presets of the containing projects.

   `AddPartToJobView` still enforces "same preset + filament per job".

## Estimation (Variant b — no default in code)

Estimation uses the **same** resolution, with no hardcoded default:

- `part.print_preset` override, ELSE preset of the containing project.
- 1 project / all-equal → unambiguous, estimate proceeds.
- Multiple different presets, no override, and **no UI context** (background job) → **do not
  guess**. Set an estimation status/message like *"Preset ambiguous, set an override"* instead
  of picking one.

New field **`Part.estimated_with_preset`** (nullable FK → `OrcaPrintPreset`): records which
preset produced the stored estimate. If a later build context resolves a different preset, the
stored estimate is still displayed (possibly slightly off) and can be re-estimated on demand.

### Estimation entry points (verified against current main)

There are **five** places that queue estimation, all currently gating on the context-free
`part.effective_print_preset` and all needing to become resolver-aware:

1. **Background worker** — `_estimate_part_in_background` (`core/services/slicing_worker.py`).
   The actual slice; resolves per-part with `Part.resolve_estimation_preset()` and refuses to
   guess when ambiguous (records the error status). #44 already fixed the stale `select_related`
   here — the query already reads `Part.objects.select_related("print_preset").get(...)`, so that
   is NOT part of this feature's work.
2. **GUI single part** — `PartReEstimateView` (`core/views/parts.py`).
3. **GUI whole project** — `ProjectReEstimateView` (`core/views/projects.py`). #51 made it
   deduplicate shared parts (`{p.pk: p for p, _mult in project._collect_parts_with_multiplier()}`);
   the preset resolution must run **in project context** and that dedup must be preserved.
4. **API single part** — `PartViewSet.estimate` (`POST /api/v1/parts/{id}/estimate/`).
5. **API whole project** — `ProjectViewSet.re_estimate` (`POST /api/v1/projects/{id}/re-estimate/`).
   Same per-project dedup as #51; resolve in project context.

Per-project re-estimate (paths 3 and 5) resolves each part's preset in the **project context**
(nearest containing project on the path — unambiguous under Variant B), so a legacy part with no
override but a project default is estimated instead of being skipped. The single-part paths (2
and 4) skip only when there is genuinely no resolvable preset; a part with an *ambiguous* preset
is still queued so the background worker records the "ambiguous" status rather than the UI/API
silently swallowing it.

Per-project requirements / build-progress views resolve the preset in the project context
(unambiguous under Variant B).

## Affected code (verified against the tree)

- `core/models/parts.py` — `effective_print_preset` is a context-free `@property`; add a
  context-aware resolver (a plain function, since a property has no project context) and the
  `estimated_with_preset` FK.
- `core/models/projects.py` — expansion (`_expand_parts_relative` /
  `_collect_parts_with_multiplier`) must carry the nearest project's `default_print_preset` down
  each path; add a public per-path resolver method.
- `core/models/printing.py` — add `PrintJob.print_preset` FK.
- `core/services/slicing_worker.py` — `_slice_job_in_background` uses `job.print_preset`;
  `_estimate_part_in_background` uses `Part.resolve_estimation_preset()` and handles the
  ambiguous case (the #44 `select_related` fix is already in place — do not re-do it).
- `core/views/print_jobs.py` — `CreateJobsFromProjectView` groups on the resolved preset and
  sets `PrintJob.print_preset`; `AddPartToJobView` handles the dropdown case; job-detail
  effective-preset display reads `job.print_preset`.
- `core/views/parts.py` — `PartReEstimateView` skip-check via `resolve_estimation_preset`;
  part-detail dropdown context.
- `core/views/projects.py` — `ProjectReEstimateView` resolves each deduped part's preset in
  project context (preserving the #51 dedup).
- `core/api/views.py` — `PartViewSet.estimate` and `ProjectViewSet.re_estimate` use the same
  resolver-aware eligibility as their GUI twins (drf-spectacular `@extend_schema` description
  text updated to match; the OpenAPI schema itself is auto-generated).
- `core/forms/projects.py` — `default_print_preset` required on the three project forms.
- `core/api/serializers.py` — `ProjectSerializer.default_print_preset` required.
- Templates — `core/templates/core/part_detail.html` (preset dropdown for the multi-preset
  part path), project/job detail displays.
- Migrations — `PrintJob.print_preset`, `Part.estimated_with_preset` (**schema only, no preset
  hardcoding**).

## Out of scope

- The `default_print_preset` `null=False` `AlterField` (needs a deployment-specific backfill).
- Setting existing projects to preset 4 (already done as data on 2026-09-21).
- Removing the now-dead `Project.effective_default_print_preset*` properties (still consumed by
  `ProjectDetailView` for a display label; leave them — cleanup, not a deliverable here). The
  stale `select_related` in `_estimate_part_in_background` was already fixed by #44.
- Any change to the `(preset, filament)` bundling *policy* itself (only the resolution feeding
  it changes).
