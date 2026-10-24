# Hardware-Einkaufsliste Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Pro Projekt eine Seite „Shopping List“ mit aggregiertem Hardware-Bedarf und einem 3-Stufen-Status (offen → im Warenkorb → bestellt), der pro Zeile automatisch zurückfällt, wenn sich der Bedarf ändert.

**Architecture:** Neues Modell `HardwareShoppingItem` (nur „in_cart“/„ordered“ gespeichert, mit Stückzahl-Snapshot). Die gesamte Logik (Liste mit effektivem Status bauen, Setzen, Bulk) lebt als Classmethods am Modell in `core/models/hardware.py` und baut auf `Project.hardware_requirements()` auf. Vier dünne CBVs (1 Lesen, 3 Schreiben via POST/PRG), ein Template; Projektansicht ersetzt den Aggregat-Block durch eine Zusammenfassung.

**Tech Stack:** Django 6.1, Bootstrap 5.3 (+ Bootstrap Icons), Django-Testrunner, uv.

**Spec:** `docs/superpowers/specs/2026-10-24-hardware-shopping-list-design.md`

## Global Constraints

- Kein Preis/keine Kosten, kein CSV, kein Lagerbestand, kein Bestellmanagement, kein API-Endpoint.
- Lesen: `LoginRequiredMixin`; jede Schreib-View: `ProjectManageMixin` (Permission `core.can_manage_projects`, 403 bei fehlendem Recht). Template-Gating: `perms.core.can_manage_projects`.
- „Offen“ wird nie gespeichert (kein Datensatz = offen).
- Effektiver Status = gespeicherter Status nur wenn `item.quantity == aktuelle aggregierte Stückzahl`, sonst „open“ mit `previous_quantity = item.quantity`.
- Schreib-Formulare senden den **Ziel**-Status (`status=<ziel>`), nie „advance“.
- PRG: Redirect auf die Liste mit Anker `#hw-<hardware_part_pk>` (Einzelzeile) bzw. ohne Anker (Bulk).
- Kategorien-Reihenfolge = `HardwarePart.CATEGORY_CHOICES`, innerhalb Name A–Z.
- UI-Texte Englisch. Kein Inline-CSS/JS; Bestätigungen per Bootstrap-Modal (`data-bs-toggle`).
- CLAUDE.md-Stil: doppelte Quotes, f-Strings, Type Hints, Google-Docstrings, CBVs, ORM. Migration nur via `makemigrations`.
- `hardware_requirements()` Dict-Keys unverändert lassen (Kostenseite + `core/api/views.py` nutzen sie).
- Tests: `export DEBUG=1`; `uv run python manage.py test core`; Gates: `uv run ruff check . && uv run ruff format --check .`, `uv run python manage.py makemigrations --check --dry-run`, `uv run python manage.py check --fail-level WARNING`, Coverage ≥ 55.

## Review Focus

1. **Gleiches Teil direkt im Projekt UND in einem Unterprojekt** → eine Zeile, Menge summiert (Test in Task 1: `test_build_list_merges_part_used_directly_and_in_child`).
2. **Teil wird aus dem Bedarf entfernt, während die Seite offen ist**, dann POST darauf → 404, kein Datensatz entsteht (Test in Task 2: `test_set_status_part_not_required_returns_404`).
3. **Ungültiger Status-Wert im POST** (z. B. `status=shipped`) → 400, nichts gespeichert (Test in Task 2: `test_set_status_invalid_value_returns_400`).
4. **Bulk-Aktionen bei leerer Liste / ohne Warenkorb-Zeilen** → no-op, Redirect 302, keine Exception (Test in Task 2: `test_bulk_actions_on_empty_list_are_noops`).
5. **Katalog-Teil wird gelöscht** (nachdem es aus allen Projekten entfernt wurde) → Shopping-Datensatz verschwindet per CASCADE, kein `ProtectedError` (Test in Task 1: `test_deleting_hardware_part_cascades_shopping_items`).

---

### Task 1: Modell `HardwareShoppingItem` + Listen-/Status-Logik

**Files:**
- Modify: `core/models/hardware.py` (Modell + `ShoppingRow` dataclass + Classmethods)
- Modify: `core/models/__init__.py` (re-export `HardwareShoppingItem`, `ShoppingRow`)
- Modify: `core/admin.py` (registrieren: `list_display = ["project", "hardware_part", "status", "quantity", "updated_at"]`, `list_filter = ["status"]`)
- Create: `core/migrations/00XX_hardwareshoppingitem.py` (via `makemigrations`)
- Optional Modify: `core/models/projects.py:872` — `select_related("hardware_part", "project")` gegen N+1 (Keys unverändert)
- Test: `core/tests/test_hardware_shopping.py`

**Interfaces:**
- Produces:
  - `class HardwareShoppingItem(models.Model)` mit `STATUS_OPEN = "open"`, `STATUS_IN_CART = "in_cart"`, `STATUS_ORDERED = "ordered"`, `STATUS_CHOICES = [(STATUS_IN_CART, "In cart"), (STATUS_ORDERED, "Ordered")]`, `ALL_STATUSES = (STATUS_OPEN, STATUS_IN_CART, STATUS_ORDERED)`.
    Felder: `project` FK `core.Project` CASCADE `related_name="hardware_shopping_items"`; `hardware_part` FK `HardwarePart` CASCADE `related_name="shopping_items"`; `status` CharField(max_length=10, choices=STATUS_CHOICES); `quantity` PositiveIntegerField(validators=[MinValueValidator(1)]); `updated_at` auto_now. Meta: `ordering = ["project", "hardware_part__category", "hardware_part__name"]`, `UniqueConstraint(fields=["project", "hardware_part"], name="uniq_shoppingitem_project_part")`. `__str__` → `f"{self.project.name}: {self.hardware_part.name} ({self.get_status_display()})"`.
  - `@dataclass class ShoppingRow`: `hardware_part: HardwarePart`, `quantity: int`, `status: str`, `previous_quantity: int | None`, `projects: list[str]`; property `next_status -> str` (open→in_cart→ordered→open).
  - `class ShoppingItemNotRequired(LookupError)` — Teil nicht im Bedarf.
  - `HardwareShoppingItem.build_list(project: Project) -> list[ShoppingRow]` — sortiert nach CATEGORY_CHOICES-Index, dann Name.
  - `HardwareShoppingItem.counts(rows: list[ShoppingRow]) -> dict[str, int]` — Keys `"open"`, `"in_cart"`, `"ordered"` (immer alle drei).
  - `HardwareShoppingItem.set_status(project: Project, hardware_part_id: int, status: str) -> ShoppingRow` — `ValueError` bei Status ∉ ALL_STATUSES, `ShoppingItemNotRequired` wenn Teil nicht im Bedarf; schreibt `quantity` = aktuelle Menge; `open` löscht den Datensatz; entfernt verwaiste Datensätze des Projekts.
  - `HardwareShoppingItem.mark_cart_ordered(project: Project) -> int` — Anzahl umgestellter Zeilen (nur effektiv „in_cart“); entfernt verwaiste Datensätze.
  - `HardwareShoppingItem.reset(project: Project) -> int` — löscht alle Datensätze des Projekts, gibt Anzahl zurück.

- [ ] **Step 1: Failing tests schreiben** in `core/tests/test_hardware_shopping.py` (`TestCase`; Fixture: Root-Projekt `R` mit `ProjectHardware(M3, qty=4)`, Kind `C` via `ProjectComponent(parent_project=R, child_project=C, quantity=2)` mit `ProjectHardware(M3, qty=3)` und `ProjectHardware(Bearing, qty=1)`; Teile `M3` (screws), `Bearing` (bearings), `Magnet` (magnets, nirgends zugewiesen)):
  - `test_build_list_sums_over_children_with_edge_quantity`: Zeile M3 `quantity == 4 + 3*2 == 10`, Bearing `== 2`; alle `status == "open"`, `previous_quantity is None`.
  - `test_build_list_merges_part_used_directly_and_in_child`: genau eine Zeile für M3; `sorted(row.projects) == ["C", "R"]`.
  - `test_build_list_orders_by_category_choices_then_name`: Reihenfolge Screws vor Bearings (CATEGORY_CHOICES), nicht alphabetisch nach Key.
  - `test_set_status_persists_snapshot_quantity`: `set_status(R, M3.pk, "in_cart")` → Datensatz `status="in_cart"`, `quantity=10`; `build_list` liefert `"in_cart"`.
  - `test_set_status_open_deletes_row` und `test_set_status_same_target_twice_is_idempotent` (zweimal `"ordered"` → 1 Datensatz, Status `"ordered"`).
  - `test_changed_quantity_resets_only_that_row`: M3 und Bearing auf `"ordered"`; dann `ProjectHardware(R, M3).quantity` auf 5 ändern → M3 `status=="open"`, `previous_quantity==10`, `quantity==11`; Bearing bleibt `"ordered"`.
  - `test_changed_edge_quantity_resets_affected_rows`: M3 + Bearing `"ordered"`, Kante R→C auf `quantity=3` → beide `"open"` (`previous_quantity` 10 bzw. 2).
  - `test_set_status_on_changed_row_takes_new_quantity`: nach Änderung `set_status(..., "in_cart")` → `quantity` = neue Menge, `previous_quantity is None`.
  - `test_set_status_rejects_invalid_status` (`ValueError`) und `test_set_status_rejects_part_not_required` (`Magnet` → `ShoppingItemNotRequired`, kein Datensatz).
  - `test_mark_cart_ordered_only_effective_cart_rows`: M3 in_cart (aktuell), Bearing in_cart aber Menge geändert → Rückgabe `1`, M3 `"ordered"`, Bearing-Datensatz unverändert `"in_cart"`.
  - `test_reset_only_affects_given_project`: Datensätze in R und C → `reset(R)` löscht nur R's.
  - `test_state_independent_per_project`: `set_status(R, Bearing.pk, "ordered")` → `build_list(C)` Bearing `"open"`.
  - `test_orphan_rows_ignored_and_pruned_on_write`: M3 `"ordered"` in R, dann alle M3-Zuweisungen löschen → nicht in `build_list(R)`; nach `set_status(R, Bearing.pk, "in_cart")` existiert kein M3-Datensatz mehr.
  - `test_counts_returns_all_three_keys`: leere Liste → `{"open": 0, "in_cart": 0, "ordered": 0}`.
  - `test_deleting_hardware_part_cascades_shopping_items`: Datensatz für M3 anlegen, alle `ProjectHardware` für M3 löschen, `M3.delete()` → kein Fehler, 0 Datensätze.

- [ ] **Step 2: Tests laufen lassen, müssen fehlschlagen**
  Run: `DEBUG=1 uv run python manage.py test core.tests.test_hardware_shopping`
  Expected: ImportError `HardwareShoppingItem`.

- [ ] **Step 3: Modell, Dataclass, Exception und Classmethods implementieren** (Signaturen oben). `build_list`: `project.hardware_requirements()` + ein Query `project.hardware_shopping_items.all()` als Dict nach `hardware_part_id`; Sortier-Key `(category_index, name.lower())`. Schreibmethoden in `transaction.atomic()`; `set_status` nutzt `update_or_create`. Re-export + Admin. Optional N+1-Fix in `_collect_hardware_with_multiplier`.

- [ ] **Step 4: Migration erzeugen**
  Run: `DEBUG=1 uv run python manage.py makemigrations core` → genau eine neue Migration; dann `makemigrations --check --dry-run` → „No changes detected“.

- [ ] **Step 5: Tests grün + Vollsuite**
  Run: `DEBUG=1 uv run python manage.py test core.tests.test_hardware_shopping core.tests.test_aggregation core.tests.test_api_requirements`
  Expected: OK.

- [ ] **Step 6: Commit** — `feat(hardware): HardwareShoppingItem model + shopping-list logic`

---

### Task 2: Einkaufslisten-Seite (Views, URLs, Template)

**Files:**
- Create: `core/views/shopping.py`
- Modify: `core/views/__init__.py` (Exports + `__all__`)
- Create: `core/urls/shopping.py`; Modify: `core/urls/__init__.py` (+ `shopping_urls`)
- Create: `core/templates/core/project_shopping_list.html` (extends `base.html`)
- Test: `core/tests/test_views_shopping.py`

**Interfaces:**
- Consumes: Task 1 (`HardwareShoppingItem.build_list/counts/set_status/mark_cart_ordered/reset`, `ShoppingRow`, `ShoppingItemNotRequired`).
- Produces (URL-Namen im Namespace `core`):
  - `project_shopping_list` → `projects/<int:pk>/shopping-list/` — `ProjectShoppingListView(LoginRequiredMixin, DetailView)`, Context: `rows: list[ShoppingRow]`, `grouped_rows: list[tuple[str, list[ShoppingRow]]]` (Kategorie-Label, Zeilen), `counts: dict[str, int]`.
  - `project_shopping_item_set` → `projects/<int:pk>/shopping-list/<int:hardware_part_pk>/` — `ProjectShoppingItemSetView(ProjectManageMixin, View)`, nur POST, Feld `status`.
  - `project_shopping_cart_ordered` → `projects/<int:pk>/shopping-list/cart-ordered/` — `ProjectShoppingCartOrderedView(ProjectManageMixin, View)`, nur POST.
  - `project_shopping_reset` → `projects/<int:pk>/shopping-list/reset/` — `ProjectShoppingResetView(ProjectManageMixin, View)`, nur POST.
  Schreib-Views: `http_method_names = ["post"]` (GET → 405), Projekt via `get_object_or_404`, `ValueError` → `HttpResponseBadRequest`, `ShoppingItemNotRequired` → `Http404`, Erfolgs-`messages.success`, Redirect wie in Global Constraints.

- [ ] **Step 1: Failing tests** in `core/tests/test_views_shopping.py` (Basis `_RBACTestBase` aus `core/tests/mixins.py`: `admin_user`/`designer_user` dürfen, `operator_user` nicht; Passwort `testpass123`; Fixture wie Task 1 auf `self.project`):
  - `test_list_renders_for_operator_without_controls`: 200, enthält Teilname + Menge, enthält **keinen** `project_shopping_item_set`-URL und kein „Reset all“.
  - `test_list_renders_controls_for_designer`: enthält Formular-Action `project_shopping_item_set` und „Mark cart as ordered“, „Reset all“.
  - `test_list_requires_login`: anonym → 302 auf Login-URL.
  - `test_list_shows_no_prices`: Teil mit `unit_price=9.99` → `"9.99"` und `"€"` nicht im Response.
  - `test_list_shows_no_link_badge_and_shop_link`: Teil mit `url` → `href="<url>"` + `target="_blank"`; Teil ohne `url` → Text `no link`.
  - `test_list_shows_quantity_changed_hint`: Datensatz mit veralteter Menge 8 → `Quantity changed (was 8)`.
  - `test_set_status_designer_redirects_with_anchor`: POST `status=in_cart` → 302, `Location` endet mit `#hw-<pk>`, Datensatz `in_cart`.
  - `test_write_endpoints_forbidden_for_operator`: alle drei POST-Endpunkte → 403, keine Datensatzänderung.
  - `test_write_endpoints_reject_get`: GET auf alle drei → 405.
  - `test_set_status_part_not_required_returns_404` und `test_set_status_invalid_value_returns_400`.
  - `test_cart_ordered_and_reset_via_views`: Zustände wie Task-1-Tests, nach POST korrekt.
  - `test_bulk_actions_on_empty_list_are_noops`: Projekt ohne Hardware → beide Bulk-POSTs 302.
  - `test_empty_list_shows_empty_state`: Projekt ohne Hardware → Leerzustandstext `No hardware` + Link zum Projekt.

- [ ] **Step 2: Fehlschlag prüfen** — `DEBUG=1 uv run python manage.py test core.tests.test_views_shopping` → NoReverseMatch.

- [ ] **Step 3: Views + URLs implementieren** (Signaturen/Verhalten oben; `grouped_rows` per `itertools.groupby` über die bereits sortierten Zeilen, Label `row.hardware_part.get_category_display()`).

- [ ] **Step 4: Template** — Kopf (Projektname, Zurück-Link, Zähler „N open · M in cart · K ordered“), Bulk-Knöpfe + zwei Bootstrap-Modals (nur mit `perms.core.can_manage_projects`; „Mark cart as ordered“ `disabled` wenn `counts.in_cart == 0`), je Kategorie eine Zwischenüberschrift, Zeile `id="hw-{{ row.hardware_part.pk }}"`: Name + ⓘ (`<i class="bi bi-info-circle" title="Used in: …">`), Menge, Link-Knopf bzw. `badge text-bg-warning` „no link“, Status-Badge (open=secondary, in_cart=primary, ordered=success) + Mini-Formular mit hidden `status={{ row.next_status }}` (nur mit Recht), bestellte Zeilen mit Klasse `text-body-secondary` + `opacity-50` auf Namen/Menge. Hinweis „Quantity changed (was N)“ als kleines `text-warning-emphasis`-Element.

- [ ] **Step 5: Tests grün** — gleicher Befehl → OK; `uv run ruff check . && uv run ruff format --check .` → sauber.

- [ ] **Step 6: Commit** — `feat(hardware): project shopping list page`

---

### Task 3: Projektansicht + Kostenseite anbinden, Doku

**Files:**
- Modify: `core/views/projects.py:159-162` (ProjectDetailView-Context) und `ProjectCostView` nicht nötig außer Template
- Modify: `core/templates/core/project_detail.html:607-660` (Aggregat-Block entfernen) + Hardware-Abschnitt (~531)
- Modify: `core/templates/core/project_cost.html` (Knopf „Shopping list“)
- Modify: `docs/user-guide/projects.md` (kurzer Abschnitt „Shopping list“)
- Test: `core/tests/test_views_shopping.py` (weitere Klasse) bzw. `core/tests/test_views_projects.py`

**Interfaces:**
- Consumes: Task 1 (`build_list`, `counts`), Task 2 (URL `core:project_shopping_list`).
- Produces: Context-Key `shopping_counts: dict[str, int] | None` in `ProjectDetailView` (`None` wenn `rows` leer).

- [ ] **Step 1: Failing tests**:
  - `test_project_detail_shows_shopping_summary`: Projekt mit Hardware (1 Zeile `in_cart`) → enthält `reverse("core:project_shopping_list", args=[pk])` und `1 in cart`.
  - `test_project_detail_drops_aggregated_hardware_block`: Text `Aggregated Hardware (incl. Sub-Projects)` nicht mehr im Response (Projekt mit Unterprojekt-Hardware).
  - `test_project_detail_no_summary_without_hardware`: ohne Hardware kein Shopping-Link, `No hardware items yet.` vorhanden.
  - `test_cost_page_links_shopping_list`: `project_cost` enthält den Shopping-List-URL.

- [ ] **Step 2: Fehlschlag prüfen** — `DEBUG=1 uv run python manage.py test core.tests.test_views_shopping core.tests.test_views_projects`.

- [ ] **Step 3: Implementieren** — Context `shopping_counts` (aus `build_list`, wiederverwendet statt zusätzlich `hardware_requirements()` zu rechnen, wo möglich; `hardware_requirements` wird im Template nur noch für den Leerzustand gebraucht → durch `shopping_counts` ersetzen und den Context-Key entfernen, falls kein anderer Template-Teil ihn nutzt). Zusammenfassungszeile: `bi-cart` Icon, „Shopping list — N open · M in cart · K ordered“, `btn btn-outline-primary btn-sm` zur Liste. Kostenseite: Knopf neben bestehenden Header-Knöpfen. Doku-Abschnitt (3–6 Sätze: Zweck, Status-Stufen, Auto-Reset bei Bedarfsänderung, Rechte).

- [ ] **Step 4: Vollsuite + Gates**
  Run: `DEBUG=1 uv run python manage.py test core` → OK; `uv run ruff check . && uv run ruff format --check .`; `uv run python manage.py makemigrations --check --dry-run`; `uv run python manage.py check --fail-level WARNING`; `uv run coverage run manage.py test core && uv run coverage report` → ≥ 55.

- [ ] **Step 5: Commit** — `feat(hardware): shopping-list summary on project + cost page, docs`
