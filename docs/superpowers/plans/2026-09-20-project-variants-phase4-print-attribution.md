# Project Variants — Phase 4 (Migrate: print attribution + per-assembly progress)

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development (or executing-plans). Steps use checkbox (`- [ ]`).
>
> **CRITICAL Kontor rule:** run every test/verify command in the FOREGROUND (one blocking Bash call, generous timeout). NEVER use `run_in_background`, `Monitor`, or `gh ... --watch` to wait — they don't reliably re-invoke you here and you will stall. After push + PR, send the parent ONE `status:info` message and STOP; the coordinator drives CI/Copilot/merge (and pokes you for a single foreground fix-turn if needed).

**Goal:** Track print progress **per assembly context**. A print that produces a part can be
attributed to the top-level assembly (e.g. "Truck A") it is for, so a part shared by two variants
counts toward the right one. Add `PrintJobPart.target_assembly`, let the flow set it, and expose
per-assembly progress — while keeping the existing global progress for backward compatibility.

**Architecture:** Migrate step. Builds on merged Phase 1–3b (`main@d585b28`). One additive DB
field (`PrintJobPart.target_assembly`, nullable FK → Project); existing rows stay null =
"unattributed" (behave exactly as today, counted globally). New context-aware helpers compute
`printed_for(assembly)` and per-assembly progress; the old global `printed_quantity` /
`aggregated_status` are untouched so dashboards and standalone views keep working.

**Tech Stack:** Django 6.0, Python 3.13, SQLite, Bootstrap 5.3, ruff 0.15.20, Django test runner.

**Spec:** `docs/superpowers/specs/2026-09-20-project-variants-dag-composition-design.md`
(section "Print attribution & per-context progress").

## Global Constraints

- Base: `main` at/after `d585b28` (Phase 3b merged). Branch from current main.
- Double quotes; f-strings; type hints; Google-style docstrings. CBV only; Django ORM.
- **RBAC:** any write view uses the appropriate role mixin (print-job write views already use
  `RoleRequiredMixin`/queue mixins — keep their existing mixins; do not weaken).
- **Exactly ONE generated migration** (AddField for `target_assembly`). No hand-authored schema.
  Existing `PrintJobPart` rows get `target_assembly=NULL` automatically — no data migration needed.
- **File ownership:** `core/models/printing.py`, `core/models/parts.py`, `core/models/projects.py`,
  `core/views/print_jobs.py`, `core/forms/print_jobs.py`, the one new migration, templates
  (`print_job_detail.html`, `project_detail.html`, part-detail if it shows progress), and tests.
  Do NOT touch `composition.py` or Phase 1–3 edge logic beyond adding read helpers.
- Tests: `.venv/bin/python manage.py test core` (coverage ≥ 55). Lint: `ruff check . && ruff format --check .`.
  Fresh worktree: `.venv` + `pip install -r requirements.txt coverage`; tests need `DEBUG=1` +
  `DJANGO_SECRET_KEY` + `collectstatic` first.

## Key definitions (read before coding)

- **Context = a top-level assembly** (a Project you actually build, e.g. Truck A).
- `printed_for(part, A)` = Σ `PrintJobPart.quantity` over job entries where `target_assembly == A`
  **and** the job has ≥1 completed plate (same "completed" rule as today's `printed_quantity`,
  just filtered by `target_assembly`).
- `needed(part, A)` = the part's total required count within A = Σ over
  `A._collect_parts_with_multiplier()` of `part.quantity × mult` for that part (already edge-based).
- **A's progress %** = `100 × Σ_parts min(printed_for, needed) / Σ_parts needed` (0 if needed=0).
- Global `printed_quantity` (unfiltered) stays as-is for backward-compat (dashboard, standalone
  module view). Per-assembly is **additional**.

---

### Task 1: `PrintJobPart.target_assembly` field + migration

**Files:** `core/models/printing.py`, one generated migration, `core/tests/test_print_attribution.py` (new)

**Interfaces:** `PrintJobPart.target_assembly` = `ForeignKey("core.Project", null=True, blank=True,
on_delete=SET_NULL, related_name="attributed_job_parts")`.

- [ ] **Step 1: Failing test** (`core/tests/test_print_attribution.py`):

```python
"""Per-assembly print attribution (Phase 4)."""

from django.test import TestCase

from core.models import Part, PrintJob, PrintJobPart, Project


class TargetAssemblyFieldTests(TestCase):
    def test_job_part_can_be_attributed_to_assembly(self):
        home = Project.objects.create(name="home")
        truck = Project.objects.create(name="Truck A")
        part = Part.objects.create(project=home, name="bolt", quantity=1)
        job = PrintJob.objects.create()
        jp = PrintJobPart.objects.create(print_job=job, part=part, quantity=5, target_assembly=truck)
        self.assertEqual(jp.target_assembly, truck)

    def test_target_assembly_defaults_null(self):
        home = Project.objects.create(name="home")
        part = Part.objects.create(project=home, name="bolt", quantity=1)
        job = PrintJob.objects.create()
        jp = PrintJobPart.objects.create(print_job=job, part=part, quantity=1)
        self.assertIsNone(jp.target_assembly)
```

- [ ] **Step 2: Run → fail** (`TypeError: ... unexpected keyword 'target_assembly'`).
- [ ] **Step 3: Add the field** to `PrintJobPart` in `core/models/printing.py`:

```python
    target_assembly = models.ForeignKey(
        "core.Project",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="attributed_job_parts",
        help_text="Top-level assembly this print contributes to (for per-variant progress). "
        "Null = unattributed (counted only in global progress).",
    )
```

- [ ] **Step 4: Generate migration** — `.venv/bin/python manage.py makemigrations core`
  (creates one AddField migration; do not edit). Verify `makemigrations --check --dry-run` clean.
- [ ] **Step 5: Run test → pass.** Commit (`git add core/models/printing.py core/migrations/00XX_* core/tests/test_print_attribution.py`).

---

### Task 2: Context-aware progress on Part

**Files:** `core/models/parts.py`, test in `test_print_attribution.py`

**Interfaces:** `Part.printed_quantity_for(assembly: Project) -> int` — completed-plate job-entry
quantity attributed to `assembly`. Keep global `printed_quantity` unchanged.

- [ ] **Step 1: Failing test:**

```python
class PrintedForContextTests(TestCase):
    def _completed_job_for(self, part, qty, assembly):
        from core.models import PrintJobPlate
        job = PrintJob.objects.create(status="completed")
        PrintJobPart.objects.create(print_job=job, part=part, quantity=qty, target_assembly=assembly)
        PrintJobPlate.objects.create(print_job=job, plate_number=1, status=PrintJobPlate.STATUS_COMPLETED)
        return job

    def test_printed_quantity_for_filters_by_assembly(self):
        home = Project.objects.create(name="home")
        a = Project.objects.create(name="Truck A")
        b = Project.objects.create(name="Truck B")
        bolt = Part.objects.create(project=home, name="bolt", quantity=1)
        self._completed_job_for(bolt, 10, a)   # 10 printed for A
        self.assertEqual(bolt.printed_quantity_for(a), 10)
        self.assertEqual(bolt.printed_quantity_for(b), 0)   # none for B
        self.assertEqual(bolt.printed_quantity, 10)          # global still counts it
```

- [ ] **Step 2: Run → fail.**
- [ ] **Step 3: Implement** (mirror the DB path of `printed_quantity` but filter `target_assembly`):

```python
    def printed_quantity_for(self, assembly: "Project") -> int:
        """Completed-plate print quantity of this part attributed to ``assembly``.

        Same completion rule as :attr:`printed_quantity` (a job counts once it has at least one
        completed plate), but restricted to job entries whose ``target_assembly`` is ``assembly``.
        """
        completed = "completed"
        completed_pks = (
            self.job_entries.filter(target_assembly=assembly, print_job__plates__status=completed)
            .values_list("pk", flat=True)
            .distinct()
        )
        from django.db.models import Sum

        return self.job_entries.filter(pk__in=completed_pks).aggregate(total=Sum("quantity"))["total"] or 0
```

- [ ] **Step 4: Run → pass.** Commit.

---

### Task 3: Per-assembly progress on Project

**Files:** `core/models/projects.py`, test in `test_print_attribution.py`

**Interfaces:** `Project.variant_progress() -> dict` with keys `percent` (int 0–100),
`needed` (int), `printed` (int), and `parts` (list of `{part, needed, printed, remaining}`),
computed for THIS project as the assembly context. Uses `_collect_parts_with_multiplier()` for
`needed` and `printed_quantity_for(self)` for `printed`.

- [ ] **Step 1: Failing test** (the core shared-part scenario):

```python
class VariantProgressTests(TestCase):
    def _complete(self, part, qty, assembly):
        from core.models import PrintJobPlate
        job = PrintJob.objects.create(status="completed")
        PrintJobPart.objects.create(print_job=job, part=part, quantity=qty, target_assembly=assembly)
        PrintJobPlate.objects.create(print_job=job, plate_number=1, status=PrintJobPlate.STATUS_COMPLETED)

    def test_shared_part_progress_is_per_variant(self):
        from core.models import ProjectPart
        a = Project.objects.create(name="Truck A")
        b = Project.objects.create(name="Truck B")
        home = Project.objects.create(name="home")
        bolt = Part.objects.create(project=home, name="bolt", quantity=10)  # each truck needs 10
        ProjectPart.objects.create(project=a, part=bolt, quantity=1)  # membership; part.quantity=10 is the count
        ProjectPart.objects.create(project=b, part=bolt, quantity=1)
        self._complete(bolt, 10, a)   # printed 10 bolts FOR Truck A

        self.assertEqual(a.variant_progress()["percent"], 100)  # A satisfied
        self.assertEqual(b.variant_progress()["percent"], 0)    # B still needs its own 10
```

(Note: `part.quantity=10` is the per-membership count during the transition — Phase-2 semantics:
`needed = part.quantity × edge-chain mult`. Here mult=1, so needed(bolt, A)=10.)

- [ ] **Step 2: Run → fail.**
- [ ] **Step 3: Implement** on `Project`:

```python
    def variant_progress(self) -> dict:
        """Per-assembly print progress using this project as the build context.

        ``needed`` per part = ``part.quantity × edge-chain multiplier`` (from
        :meth:`_collect_parts_with_multiplier`); ``printed`` = quantities attributed to THIS
        assembly via ``PrintJobPart.target_assembly``. Progress caps each part at its need.
        """
        from collections import defaultdict

        needed_by_part: dict = defaultdict(int)
        part_objs: dict = {}
        for part, mult in self._collect_parts_with_multiplier():
            needed_by_part[part.pk] += part.quantity * mult
            part_objs[part.pk] = part

        rows, total_needed, total_printed = [], 0, 0
        for pk, needed in needed_by_part.items():
            part = part_objs[pk]
            printed = part.printed_quantity_for(self)
            counted = min(printed, needed)
            total_needed += needed
            total_printed += counted
            rows.append({"part": part, "needed": needed, "printed": printed, "remaining": max(0, needed - printed)})

        percent = int(total_printed / total_needed * 100) if total_needed else 0
        return {"percent": percent, "needed": total_needed, "printed": total_printed, "parts": rows}
```

- [ ] **Step 4: Run → pass.** Commit.

---

### Task 4: Capture `target_assembly` in the add-part-to-job flow

**Files:** `core/views/print_jobs.py`, `core/forms/print_jobs.py`, the add-part template, test.

**Interfaces:** `AddPartToJobView` (print_jobs.py:182) accepts an optional `target_assembly`
POST param and sets it on the created/updated `PrintJobPart`. Candidate assemblies for a part =
the top-level assemblies that (transitively) contain it — computed from `part.containing_projects()`
walked up to roots, OR simply all top-level projects; keep it a simple optional `<select>`.

- [ ] **Step 1: Failing test:**

```python
    def test_add_part_to_job_sets_target_assembly(self):
        from core.models import Part, PrintJob, PrintJobPart, Project
        truck = Project.objects.create(name="Truck A", created_by=self.user)
        home = Project.objects.create(name="home", created_by=self.user)
        part = Part.objects.create(project=home, name="bolt", quantity=1)
        job = PrintJob.objects.create(created_by=self.user)
        self.client.force_login(self.user)
        self.client.post(
            reverse("core:add_part_to_job", kwargs={"part_pk": part.pk}),
            {"job": job.pk, "quantity": 2, "target_assembly": truck.pk},
        )
        jp = PrintJobPart.objects.get(print_job=job, part=part)
        self.assertEqual(jp.target_assembly_id, truck.pk)
```

(Adapt the POST keys/URL name to the real `AddPartToJobView` contract — inspect it first.)

- [ ] **Step 2: Run → fail.**
- [ ] **Step 3: Implement** — in `AddPartToJobView.post`, read `target_assembly` from
  `request.POST` (validate it's a real Project or None) and set it on the `PrintJobPart` in the
  `get_or_create`/create call (`defaults`/assignment). Add an optional `target_assembly` select to
  the add-to-job form/template (top-level assemblies; blank = unattributed). Keep the existing
  RBAC mixin.
- [ ] **Step 4: Run → pass.** Commit.

---

### Task 5: Show per-variant progress on the project detail (top-level assemblies)

**Files:** `core/views/projects.py`, `core/templates/core/project_detail.html`, test.

**Interfaces:** `ProjectDetailView` adds `context["variant_progress"] = self.object.variant_progress()`
when the project is a top-level assembly (`not self.object.is_subproject`) OR always (cheap). Template
renders a small progress bar + per-part needed/printed/remaining table for the build context.

- [ ] **Step 1: Failing render test:**

```python
    def test_detail_shows_variant_progress_bar(self):
        truck = Project.objects.create(name="Truck A", created_by=self.user)
        self.client.force_login(self.user)
        resp = self.client.get(reverse("core:project_detail", kwargs={"pk": truck.pk}))
        self.assertContains(resp, "Build progress")  # the new per-variant progress section label
```

- [ ] **Step 2: Run → fail.**
- [ ] **Step 3: Implement** context + a Bootstrap progress bar / small table in `project_detail.html`
  labeled e.g. "Build progress" showing `variant_progress.percent` and the per-part rows. Keep the
  existing aggregate status badge (global) as-is.
- [ ] **Step 4: Run → pass.** Commit.

---

### Task 6: Full suite + gates (foreground) + PR

- [ ] **Step 1 (foreground):**
```bash
cd <worktree> && DEBUG=1 DJANGO_SECRET_KEY=phase4 .venv/bin/python manage.py test core 2>&1 | tail -25
ruff check . && ruff format --check .
.venv/bin/python manage.py makemigrations --check --dry-run   # exactly the one new migration, in sync
.venv/bin/python manage.py check --fail-level WARNING
```
- [ ] **Step 2:** push + `gh pr create` (title "feat: per-assembly print attribution — Project
  Variants Phase 4"; body: model field + migration, per-context progress helpers, add-to-job
  target selector, project detail build-progress; note global progress preserved). Write PR# to
  memory `feature/project-variants/phase4-progress`, send parent ONE `status:info`, STOP.

---

## Self-Review

**Spec coverage:** `PrintJobPart.target_assembly` (Task 1); per-context printed/needed/progress
(Tasks 2–3); flow sets target assembly (Task 4); UI shows per-variant progress (Task 5). Global
progress preserved for backward-compat (explicit in defs + Tasks 2/5). Backfill = existing rows
null (automatic, no data migration). ✅

**Placeholder scan:** Task 4 test says "adapt POST keys/URL to real AddPartToJobView contract" —
that is an instruction to inspect the real view first, not a code placeholder; the implementer
must read `AddPartToJobView` (print_jobs.py:182) and match its param names. All other steps concrete. ✅

**Type consistency:** `printed_quantity_for(assembly)`, `variant_progress()` (dict keys
percent/needed/printed/parts), `target_assembly` / `attributed_job_parts` used consistently across
tasks. `needed` uses Phase-2 semantics (`part.quantity × mult`), matching merged aggregation. ✅

**Migration:** exactly one AddField; `makemigrations --check` verified in Task 1 + Task 6. ✅

**Kontor discipline:** foreground waits; PR then hand off (no background/Monitor/--watch). ✅
