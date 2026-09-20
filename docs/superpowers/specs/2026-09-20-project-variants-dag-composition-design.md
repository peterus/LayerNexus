# Design: Variant support via DAG composition (reusable parts & modules)

**Date:** 2026-09-20
**Status:** Draft for review
**Scope:** `core` app data model, aggregation, print/queue attribution, UI flows, migration.

## Problem

LayerNexus models a project as a tree: a `Part` belongs to exactly one `Project`
(`Part.project`, CASCADE), and a `Project` has at most one `parent` (self-FK) with a
`quantity` multiplier. This is the modular-truck bundling mechanism today.

Users building **variants** of an assembly (e.g. two versions of a modular truck that
share 80–90 % of parts and differ only in a hood, or sometimes a single part inside an
otherwise-shared module) must currently **re-create every part and sub-module** for each
variant, because a shared module can only hang off a single `parent` and a part can only
belong to a single project.

## Goal

Let parts and modules be **reusable building blocks** that can be composed into multiple
assemblies. A "variant" then requires no special concept — it is simply another assembly
that references the same shared building blocks and swaps the differing ones. Sharing is
live where blocks are referenced, and per-variant where they are not ("Mischung").

## Decisions (from brainstorming)

- **Sharing semantics:** Mixture — some blocks live-shared, some variant-specific. A
  single many-to-many composition model expresses both (reference the same block → shared;
  reference a different block → variant-specific).
- **Granularity:** Differences may go down to a **single part** inside an otherwise-shared
  module. Handled by making modules *thin reference bundles*: fork the thin module node
  (copy its edges, re-point one) while the parts themselves stay shared objects.
- **Appetite:** Clean & future-proof — real M:N composition via through-models, not a
  copy-on-create shortcut.
- **Architecture:** DAG composition (chosen over "projects-only M:N" and over a
  "base + overrides" variant/diff layer). Overrides can be added additively later; they
  are YAGNI now.
- **Progress semantics:** **Exact, per assembly context.** A print counts toward a
  specific assembly, not a global per-part tally. "Context" granularity = the **top-level
  assembly** being built (e.g. "Truck Variant A").

## Non-goals (YAGNI)

- Per-edge attribute overrides (same STL, different color/preset per reference). Reserved
  as a future additive field on `ProjectPart`.
- Full path-level print attribution (attribution is to the top-level assembly, not to a
  specific path through the DAG). Reserved as a future refinement.
- A dedicated `Variant`/diff model (Approach 3). Not built.

## Data model

Two through-models replace the two rigid FKs. `quantity` moves from the **node** to the
**edge**, because the same block used in two places may be needed in different counts.

```
ProjectComponent                     ProjectPart
  parent_project  FK Project           project  FK Project      # the containing module
  child_project   FK Project           part     FK Part
  quantity        PositiveInt >= 1     quantity PositiveInt >= 1
  position        int (ordering)        position int (ordering)
  unique(parent_project, child_project) unique(project, part)
```

- **`Part`** loses `project` (FK) and `quantity` (→ edge). It becomes a standalone,
  reusable building block. A part with zero edges is a "library part" (listed in the parts
  library, never auto-deleted as an orphan).
- **`Project`** loses `parent` (self-FK) and `quantity` (→ edge). "Module", "assembly" and
  "variant" are all just `Project`; the difference is pure composition. A **top-level
  assembly** is a project with no incoming `ProjectComponent` edge.
- **Creating a variant** = a new assembly that receives the same
  `ProjectComponent`/`ProjectPart` edges as the original, except the differing ones.
- **Shared module, one differing part** = fork only the *thin* module node (copy its
  edges, re-point one). The parts behind it stay the same objects — no part is duplicated.
- **Cycle detection** stays (visited-set), now over the DAG, enforced in `clean()`/`save()`
  of both through-models — an assembly may not (transitively) contain itself.

### Cascade / delete semantics

- Removing a block *from* an assembly deletes only the **edge**
  (`ProjectComponent`/`ProjectPart` row); the block and any other references survive.
- Deleting a `Project` or `Part` **node** is a separate action that blocks (or warns) when
  the node is still referenced — never a silent cascade onto shared blocks. Old
  `CASCADE`/`PROTECT` on the removed FKs is replaced by explicit edge/node handling.

## DAG traversal & aggregation

- The recursive `_collect_*` methods (`_collect_parts_with_multiplier`,
  `_collect_hardware_with_multiplier`, `_collect_documents`) are rewritten to traverse
  `ProjectComponent`/`ProjectPart` and multiply the **edge `quantity`** along the path
  (same idea as today's multiplier, DAG instead of tree).
- **Need per context:** `needed(part, assembly A)` = sum over all paths from A to the part
  of the product of edge quantities. A part reachable via two paths in A sums correctly.
- **Memoization per aggregation run:** DAG sharing can revisit the same node via different
  paths; a per-run cache keeps traversal flat and terminates on any raced cycle
  (visited-set guard retained).
- **Prefetch:** `aggregate_prefetch_lookups()` is rewritten against the through-relations;
  traversal depth stays capped as today (deep levels degrade to lazy queries, never worse
  than the current behavior).

## Print attribution & per-context progress

- `PrintJobPart` gains `target_assembly` (FK → `Project`, nullable for legacy/unattributed
  prints). Enqueuing/printing selects **which assembly** the print is for.
- `printed_quantity` becomes context-aware: `printed(part, A)` = Σ of completed job-entry
  quantities attributed to A. `remaining` / `is_complete` / `progress` are computed **per
  assembly**.
- "Context" = the **top-level assembly** being built. This is the granularity of "exact"
  here; finer path-level attribution is a future refinement.
- Queue flow: choosing the target assembly is added when a print job is created
  (`services/queue.py` + queue/print-job views). This is the one real intrusion into the
  print/queue subsystem (previously insulated from project hierarchy).

## Navigation

- `get_ancestors()` (single breadcrumb path) is removed — a module can have multiple
  parents. Replaced by:
  - a **"Used in"** list (all assemblies referencing this node), and
  - a breadcrumb built from the **actually-navigated path** (via URL context), not the
    model.

## UI/UX flows

- **Parts library:** a new list view of all parts as reusable building blocks. "Add part
  to module" references an existing part (creating a new part stays possible).
- **Assembly editor:** add/remove/reorder existing modules and parts with a quantity
  (managing edges).
- **"Duplicate as variant":** copies an assembly's edges into a new assembly, which the
  user then re-points selectively (swap the hood, etc.). Nodes/parts are **referenced, not
  copied**.

## Migration & compatibility

- Data migration: each existing `Part.project` → one `ProjectPart` edge (`quantity` taken
  from the part); each existing `Project.parent`/`quantity` → one `ProjectComponent` edge.
  The old fields are then removed.
- Existing print jobs: backfill `target_assembly` to the part's (ex-)root assembly where
  unambiguous; otherwise leave null (unattributed).
- Exactly **one** new migration (CI gate `makemigrations --check` allows only generated
  migrations; project convention is a single migration for this change set).

## Delivery phases

Because of the ~130 coupling points plus print attribution, deliver in phases — each green
and merged before the next starts, TDD throughout, coverage gate ≥ 55 %.

1. **Model + composition core:** through-models plus the single migration that, in one
   file, creates the edge tables → migrates data from the old FKs (`RunPython`) → removes
   the old `Part.project`/`Part.quantity` and `Project.parent`/`Project.quantity` fields;
   aggregation rewrite lands in the same phase so the suite stays green.
2. **Navigation + editing:** edge-vs-node delete semantics, "Used in" navigation, parts
   library, assembly editor.
3. **Print attribution:** `PrintJobPart.target_assembly`, queue/job flow selects context,
   per-context progress/status.
4. **Variant convenience:** "Duplicate as variant".

## Blast radius (reference)

~130 coupling points assume "a part belongs to exactly one project" or "a project has one
parent": aggregation (`_collect_*`, `aggregate_prefetch_lookups`), breadcrumbs
(`get_ancestors`), cycle detection, cascade deletes, prefetch/`select_related` chains,
forms (`forms/parts.py`, `forms/projects.py`), templates (`project_detail.html`,
`project_list.html`), admin inlines/filters, dashboard filters
(`Part.objects.filter(project__in=...)`), and tests. Queue/PrintJob are otherwise
insulated from the hierarchy **except** for the new `target_assembly` attribution added in
phase 3.

## Testing

- Aggregation over a DAG (shared child via multiple paths, correct multiplier sums).
- Cycle rejection on both through-models.
- Edge delete vs node delete (shared block survives edge removal; referenced-node delete
  blocked/warned).
- Per-context `needed`/`printed`/`remaining`/`is_complete` with a part shared across two
  assemblies.
- Migration correctness (old FK graph → equivalent edge graph; counts/filament totals
  unchanged for existing single-owner projects).
- Backfill of `target_assembly` for existing print jobs.
