# Project Variants — Phase 3b (Editing: parts library + assembly editor + delete semantics)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans. Steps use checkbox (`- [ ]`) syntax.
>
> **CRITICAL Kontor rule:** run every test/verify command in the FOREGROUND (one blocking Bash call, generous timeout). NEVER use `run_in_background`, the `Monitor` tool, or `gh ... --watch` to wait — they do not reliably re-invoke you here and you will stall. After the branch is pushed and the PR is open, send the parent ONE `status:info` message and STOP; the coordinator drives CI/Copilot/merge (and pokes you for a single foreground fix-turn if Copilot has findings).

**Goal:** Give users the UI to compose variants: a **parts library**, an **assembly editor** on the project detail page (add existing modules/parts via a picker with a quantity, edit quantity, remove-from-assembly), and clear **delete semantics** (removing an edge ≠ deleting a node; node delete warns "used in N").

**Architecture:** Migrate step (editing). Builds on merged Phase 1–3a (`main@d4b4254`): composition edges `ProjectComponent`/`ProjectPart` (+ cycle guard on `ProjectComponent.save/clean`), dual-write, edge-based aggregation and read/nav (`parent_assemblies`, `child_modules`, `direct_parts`, `containing_projects`, `used_in`, `_used_in.html`). This phase adds **edge write** views/forms + templates. Creating a brand-new part keeps the existing `PartCreateView` (sets `project` FK, dual-write mirrors a `ProjectPart` edge); the new "add existing" flows create edges directly so a block can be shared across assemblies.

**Tech Stack:** Django 6.0, Python 3.13, SQLite, Bootstrap 5.3 + minimal vanilla JS (a Bootstrap modal), ruff 0.15.20, Django test runner.

**Spec:** `docs/superpowers/specs/2026-09-20-project-variants-dag-composition-design.md`

## Global Constraints

- Base: `main` at/after `d4b4254` (Phase 3a merged). Branch from current main.
- Double quotes; f-strings; type hints; Google-style docstrings. Class-based views only; Django ORM.
- **RBAC MANDATORY:** every write/delete view uses `ProjectManageMixin` (from `core/mixins.py`).
  Read-only list/detail may use `LoginRequiredMixin` only.
- **No migration** — models unchanged (edges already exist). `makemigrations --check` stays clean.
- **File ownership:** `core/views/projects.py`, `core/views/parts.py`, `core/forms/projects.py`,
  `core/forms/parts.py`, `core/urls/projects.py`, `core/urls/parts.py` (as needed),
  `core/views/__init__.py` (export new views), templates under `core/templates/core/`
  (project_detail.html, new parts_library.html, new small includes, project_confirm_delete.html /
  part_confirm_delete.html), and tests under `core/tests/`. Do NOT touch `composition.py`,
  models (except read helpers already present), or migrations.
- Tests: `.venv/bin/python manage.py test core` (coverage ≥ 55). Lint: `ruff check . &&
  ruff format --check .`. Fresh worktree: `.venv` + `pip install -r requirements.txt coverage`;
  tests need `DEBUG=1` + `DJANGO_SECRET_KEY` + a `collectstatic` run first.
- Existing patterns to follow: write views subclass `ProjectManageMixin` + a generic CBV
  (see `PartUpdateView`, `SubProjectCreateView`); URL namespace is `core:`; forms live in
  `core/forms/*` and are re-exported via `core/forms/__init__.py`; views via `core/views/__init__.py`.

## Merged interfaces consumed

- `ProjectComponent(parent_project, child_project, quantity, position)` — `.save()`/`.clean()`
  raise `ValidationError` on a cycle; `component_would_create_cycle(parent_id, child_id)` helper;
  unique `(parent_project, child_project)`.
- `ProjectPart(project, part, quantity, position)` — unique `(project, part)`.
- `Project.get_descendant_ids()`, `Project.parent_assemblies()`, `Project.child_modules()`,
  `Project.direct_parts()`, `Project.direct_part_count()`; `Part.containing_projects()`.

---

### Task 1: Parts library list view

**Files:**
- Create: `core/templates/core/parts_library.html`
- Modify: `core/views/parts.py`, `core/views/__init__.py`, `core/urls/parts.py` (or `core/urls/projects.py` if parts share it — check `core/urls/__init__.py` for the parts include)
- Test: `core/tests/test_views_parts.py`

**Interfaces:**
- Produces: `PartLibraryListView` (LoginRequiredMixin, ListView) at `core:part_library`
  (URL `parts/`), context `parts` = all parts annotated with their `project_links` count
  (how many assemblies use each). Read-only.

- [ ] **Step 1: Failing test**

```python
    def test_part_library_lists_all_parts(self):
        from core.models import Part
        Part.objects.create(project=self.project, name="LibPartA", quantity=1)
        self.client.force_login(self.user)
        resp = self.client.get(reverse("core:part_library"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "LibPartA")
```

- [ ] **Step 2: Run → fail** (`NoReverseMatch: 'part_library'`).
  `.venv/bin/python manage.py test core.tests.test_views_parts -v 2`

- [ ] **Step 3: Implement**

In `core/views/parts.py`:

```python
class PartLibraryListView(LoginRequiredMixin, ListView):
    """Read-only library of all parts as reusable building blocks."""

    model = Part
    template_name = "core/parts_library.html"
    context_object_name = "parts"
    paginate_by = 50

    def get_queryset(self):
        from django.db.models import Count

        return Part.objects.annotate(used_in_count=Count("project_links", distinct=True)).order_by("name")
```

Add `PartLibraryListView` to `core/views/__init__.py` exports. Add the URL to the parts urlconf
(find it via `core/urls/__init__.py`; add `path("parts/", PartLibraryListView.as_view(),
name="part_library")`). Create `parts_library.html` extending `base.html`: a Bootstrap table of
parts (name, material, `used_in_count` badge, link to `part_detail`). Add a nav link to the
library in the main nav include (grep the navbar template for existing links; keep it minimal).

- [ ] **Step 4: Run → pass.** Commit.
```bash
git add core/views/parts.py core/views/__init__.py core/urls/*.py core/templates/core/parts_library.html core/tests/test_views_parts.py
git commit -m "feat: parts library list view (Phase 3b)"
```

---

### Task 2: Forms for adding existing module/part to a project (cycle-safe)

**Files:**
- Modify: `core/forms/projects.py`, `core/forms/parts.py`, `core/forms/__init__.py`
- Test: `core/tests/test_forms.py`

**Interfaces:**
- Produces:
  - `AddComponentForm(parent_project)` — ModelForm for `ProjectComponent` with fields
    `child_project`, `quantity`. `__init__` takes the parent and restricts the `child_project`
    queryset to exclude the parent itself, its existing children, and its descendants
    (`get_descendant_ids`) to prevent cycles/dupes. `clean` also relies on the model cycle guard.
  - `AddPartToProjectForm(project)` — ModelForm for `ProjectPart` with fields `part`, `quantity`;
    excludes parts already linked to this project.

- [ ] **Step 1: Failing test**

```python
    def test_add_component_form_excludes_self_and_descendants(self):
        from core.models import Project, ProjectComponent
        from core.forms import AddComponentForm
        root = Project.objects.create(name="Root")
        child = Project.objects.create(name="Child")
        ProjectComponent.objects.create(parent_project=root, child_project=child)
        form = AddComponentForm(parent_project=root)
        qs = list(form.fields["child_project"].queryset)
        self.assertNotIn(root, qs)   # not itself
        self.assertNotIn(child, qs)  # already a child

    def test_add_component_form_rejects_cycle(self):
        from core.models import Project, ProjectComponent
        from core.forms import AddComponentForm
        a = Project.objects.create(name="A")
        b = Project.objects.create(name="B")
        ProjectComponent.objects.create(parent_project=a, child_project=b)
        # adding a under b would cycle; queryset excludes it, and clean must reject if forced
        form = AddComponentForm(parent_project=b, data={"child_project": a.pk, "quantity": 1})
        self.assertFalse(form.is_valid())
```

- [ ] **Step 2: Run → fail** (ImportError).

- [ ] **Step 3: Implement** (in `core/forms/projects.py`):

```python
class AddComponentForm(forms.ModelForm):
    """Add an existing project/module as a child of ``parent_project`` (composition edge)."""

    class Meta:
        model = ProjectComponent
        fields = ["child_project", "quantity"]

    def __init__(self, *args, parent_project: "Project", **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.parent_project = parent_project
        excluded = {parent_project.pk} | parent_project.get_descendant_ids()
        existing = parent_project.child_links.values_list("child_project_id", flat=True)
        self.fields["child_project"].queryset = Project.objects.exclude(
            pk__in=set(excluded) | set(existing)
        ).order_by("name")

    def clean(self):
        cleaned = super().clean()
        child = cleaned.get("child_project")
        if child and component_would_create_cycle(self.parent_project.pk, child.pk):
            raise forms.ValidationError("Adding this module would create a cycle.")
        return cleaned

    def save(self, commit: bool = True) -> "ProjectComponent":
        self.instance.parent_project = self.parent_project
        return super().save(commit=commit)
```

Import `component_would_create_cycle` and `ProjectComponent`/`Project` at top of the form module.
Analogously add `AddPartToProjectForm` in `core/forms/parts.py` (ModelForm for `ProjectPart`,
fields `part`,`quantity`; `__init__(project)` sets `self.instance.project`/restricts `part`
queryset to exclude parts already linked: `project.part_links.values_list("part_id", flat=True)`).
Re-export both from `core/forms/__init__.py`.

- [ ] **Step 4: Run → pass.** Commit.
```bash
git add core/forms/projects.py core/forms/parts.py core/forms/__init__.py core/tests/test_forms.py
git commit -m "feat: cycle-safe forms to add existing module/part to a project (Phase 3b)"
```

---

### Task 3: Edge write views (add / edit-quantity / remove) + URLs

**Files:**
- Modify: `core/views/projects.py`, `core/views/__init__.py`, `core/urls/projects.py`
- Test: `core/tests/test_views_projects.py`

**Interfaces:** all under `ProjectManageMixin`. Produces:
- `ProjectAddComponentView` (POST) at `projects/<int:pk>/components/add/` → uses `AddComponentForm`,
  redirects back to `project_detail`.
- `ProjectAddPartView` (POST) at `projects/<int:pk>/parts/add/` → uses `AddPartToProjectForm`.
- `ProjectComponentDeleteView` at `components/<int:pk>/remove/` → deletes the `ProjectComponent`
  edge ("remove from assembly"); redirect to the parent `project_detail`.
- `ProjectPartDeleteView` at `project-parts/<int:pk>/remove/` → deletes the `ProjectPart` edge.
- Quantity edit: reuse the delete views' sibling `UpdateView`s
  (`ProjectComponentUpdateView`/`ProjectPartUpdateView`) editing only `quantity`, or accept an
  inline POST — keep it a small `UpdateView` with a one-field form for clarity.

- [ ] **Step 1: Failing tests**

```python
    def test_add_existing_component_creates_edge(self):
        from core.models import Project, ProjectComponent
        truck = Project.objects.create(name="Truck", created_by=self.user)
        cabin = Project.objects.create(name="Cabin", created_by=self.user)
        self.client.force_login(self.user)
        resp = self.client.post(
            reverse("core:project_add_component", kwargs={"pk": truck.pk}),
            {"child_project": cabin.pk, "quantity": 2},
        )
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(ProjectComponent.objects.filter(parent_project=truck, child_project=cabin, quantity=2).exists())

    def test_remove_component_edge_keeps_node(self):
        from core.models import Project, ProjectComponent
        truck = Project.objects.create(name="Truck", created_by=self.user)
        cabin = Project.objects.create(name="Cabin", created_by=self.user)
        edge = ProjectComponent.objects.create(parent_project=truck, child_project=cabin)
        self.client.force_login(self.user)
        resp = self.client.post(reverse("core:project_component_remove", kwargs={"pk": edge.pk}))
        self.assertEqual(resp.status_code, 302)
        self.assertFalse(ProjectComponent.objects.filter(pk=edge.pk).exists())
        self.assertTrue(Project.objects.filter(pk=cabin.pk).exists())  # node survives

    def test_add_component_requires_manage_permission(self):
        # a Designer/Operator without manage rights -> 403 (ProjectManageMixin raise_exception)
        ...  # use an unauthorized user fixture per existing RBAC test patterns
```

- [ ] **Step 2: Run → fail.**

- [ ] **Step 3: Implement** the views (in `core/views/projects.py`), e.g.:

```python
class ProjectAddComponentView(ProjectManageMixin, View):
    """Add an existing module as a child of this project (composition edge)."""

    def post(self, request, *args, **kwargs):
        parent = get_object_or_404(Project, pk=kwargs["pk"])
        form = AddComponentForm(parent_project=parent, data=request.POST)
        if form.is_valid():
            form.save()
            messages.success(request, "Module added to assembly.")
        else:
            messages.error(request, "; ".join(form.errors.get("__all__", []) or ["Could not add module."]))
        return redirect(reverse("core:project_detail", kwargs={"pk": parent.pk}))
```

`ProjectComponentDeleteView(ProjectManageMixin, DeleteView)` with `model = ProjectComponent`,
`get_success_url` → parent `project_detail`; no confirm template needed if you POST directly from
the assembly editor (use `DeleteView` POST). Analogous `ProjectAddPartView` /
`ProjectPartDeleteView`. Add small `quantity`-only `UpdateView`s if inline edit is wanted (else
allow remove+re-add). Wire all URLs in `core/urls/projects.py` with the `core:` names used above;
export views from `core/views/__init__.py`.

- [ ] **Step 4: Run → pass.** Commit.
```bash
git add core/views/projects.py core/views/__init__.py core/urls/projects.py core/tests/test_views_projects.py
git commit -m "feat: edge write views add/remove module+part on a project (Phase 3b)"
```

---

### Task 4: Assembly-editor UI on the project detail page (picker modal)

**Files:**
- Modify: `core/templates/core/project_detail.html`
- Create: `core/templates/core/includes/_assembly_editor.html` (optional split)
- Test: `core/tests/test_views_projects.py` (render assertions)

**Interfaces:** consumes the Task 3 views + Task 2 forms exposed in `ProjectDetailView` context.

- [ ] **Step 1: Extend the detail context**

In `ProjectDetailView.get_context_data` (projects.py) add, guarded by manage permission so
read-only users don't see edit controls:

```python
        if self.request.user.has_perm("core.can_manage_projects"):
            context["add_component_form"] = AddComponentForm(parent_project=self.object)
            context["add_part_form"] = AddPartToProjectForm(project=self.object)
            context["can_edit_assembly"] = True
```

- [ ] **Step 2: Template**

In `project_detail.html`, in the sub-modules and parts sections (which after Phase 3a iterate
`child_modules` and `direct_parts`), add — only when `can_edit_assembly` —:
- a "Remove" button per child/part row that POSTs to `project_component_remove` /
  `project_part_remove` (small inline `<form method="post">` with `{% csrf_token %}`).
- an "Add existing module" and "Add existing part" button that open Bootstrap modals containing
  the respective forms, POSTing to `project_add_component` / `project_add_part`. Use Bootstrap 5.3
  modal markup + the form fields; no custom JS beyond Bootstrap's data-bs-toggle.
Keep the "Create new part" / "Create sub-project" actions that already exist (they still work via
dual-write). Render edge `quantity` next to each row.

- [ ] **Step 3: Render test**

```python
    def test_detail_shows_assembly_editor_for_manager(self):
        proj = Project.objects.create(name="EditProj", created_by=self.user)
        self.client.force_login(self.user)  # self.user is Admin in TestDataMixin
        resp = self.client.get(reverse("core:project_detail", kwargs={"pk": proj.pk}))
        self.assertContains(resp, "Add existing module")
```

- [ ] **Step 4: Run → pass.** Commit.
```bash
git add core/templates/core/project_detail.html core/templates/core/includes core/views/projects.py core/tests/test_views_projects.py
git commit -m "feat: assembly editor (picker modals + remove) on project detail (Phase 3b)"
```

---

### Task 5: Node-delete semantics — "used in N" warning

**Files:**
- Modify: `core/views/projects.py` (ProjectDeleteView), `core/views/parts.py` (PartDeleteView),
  their confirm templates (`project_confirm_delete.html` / `part_confirm_delete.html` — grep names).
- Test: `core/tests/test_views_projects.py`, `core/tests/test_views_parts.py`

**Interfaces:** deleting a node still cascades its edges (the through-models have `CASCADE` on
both FKs), so deletion removes the block from all assemblies. The confirm page must surface this.

- [ ] **Step 1: Failing test**

```python
    def test_delete_confirm_warns_used_in(self):
        from core.models import Project, ProjectComponent
        cabin = Project.objects.create(name="Cabin", created_by=self.user)
        truck = Project.objects.create(name="Truck", created_by=self.user)
        ProjectComponent.objects.create(parent_project=truck, child_project=cabin)
        self.client.force_login(self.user)
        resp = self.client.get(reverse("core:project_delete", kwargs={"pk": cabin.pk}))
        self.assertContains(resp, "Truck")   # shows which assemblies it's used in
        self.assertContains(resp, "used in")  # warning copy
```

- [ ] **Step 2: Run → fail.**

- [ ] **Step 3: Implement**

In `ProjectDeleteView.get_context_data`: add `context["used_in"] = self.object.parent_assemblies()`.
In `PartDeleteView.get_context_data`: add `context["used_in"] = self.object.containing_projects()`.
In both confirm templates, when `used_in` is non-empty, render a warning alert:
"This is used in N assemblies (list); deleting it removes it from them." Keep the delete action
(the cascade handles edge cleanup). Both delete views must keep `ProjectManageMixin`.

- [ ] **Step 4: Run → pass.** Commit.
```bash
git add core/views/projects.py core/views/parts.py core/templates/core core/tests
git commit -m "feat: node-delete confirm warns 'used in N assemblies' (Phase 3b)"
```

---

### Task 6: Full-suite + gates (foreground) and PR

- [ ] **Step 1: Foreground full suite + gates**

```bash
cd <worktree> && DEBUG=1 DJANGO_SECRET_KEY=phase3b .venv/bin/python manage.py test core 2>&1 | tail -25
ruff check . && ruff format --check .
.venv/bin/python manage.py makemigrations --check --dry-run
.venv/bin/python manage.py check --fail-level WARNING
```
Expected: all green, coverage ≥ 55, no new migration.

- [ ] **Step 2: Push + open PR (foreground), then hand off**

```bash
git push -u origin sub/<id> && gh pr create --repo peterus/LayerNexus --base main --head sub/<id> \
  --title "feat: parts library + assembly editor + delete semantics — Project Variants Phase 3b" \
  --body "<summary of Tasks 1-5; RBAC via ProjectManageMixin; no migration>"
```
Write the PR number to memory `feature/project-variants/phase3b-progress`, then
`kontor_send_to_session(<parent>, status:'info', "PR #<n> geöffnet, übergebe CI/Copilot/Merge")`
and STOP. Do NOT poll CI (coordinator drives it).

---

## Self-Review

**Spec coverage (Phase 3b):** parts library (Task 1); "add existing module/part" picker on the
project detail page (Tasks 2–4); remove-edge vs delete-node semantics with "used in" guard
(Tasks 3, 5); RBAC via `ProjectManageMixin` on every write/delete view (Tasks 3–5). "Duplicate as
variant" = Phase 5 (out of scope). No migration. ✅

**Placeholder scan:** one intentional `...` in `test_add_component_requires_manage_permission`
(fill using the existing RBAC unauthorized-user fixture in the test module — pattern already in
`test_views_projects.py`). All other steps concrete. Fix that stub when implementing. ✅

**Type consistency:** form names `AddComponentForm(parent_project=…)` / `AddPartToProjectForm(project=…)`
and view/URL names (`project_add_component`, `project_component_remove`, `project_add_part`,
`project_part_remove`, `part_library`) used consistently across Tasks 1–5 and templates. ✅

**Cycle safety:** `AddComponentForm` excludes self+descendants+existing children AND re-checks via
`component_would_create_cycle`; the model `save()` guard is the backstop. ✅

**Kontor discipline:** all waits foreground; PR then hand off (no background/Monitor/--watch). ✅
