# Status ableiten statt speichern — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Druckversuche als Historie behalten, Platten-/Auftrags-/Queue-Status daraus berechnen, Platten kennen ihre Teile, gedruckte Stückzahl = bestandene Platten + manuelle Buchungen.

**Architecture:** Fünf einzeln mergebare PRs (PR-A … PR-E), je eine Kind-Session im eigenen Worktree. Fakten bleiben als Felder, Zustände werden als Properties + QuerySet-Methoden/Annotationen am Modell abgeleitet; alle Übergänge sind Modell-Methoden, Services/Views/API rufen nur diese. Datenmigrationen per `makemigrations` + `RunPython`.

**Tech Stack:** Django 6.1, Python 3.14, SQLite, DRF, Bootstrap 5.3 Templates, Django-Testrunner (`uv run python manage.py test core`).

**Spec:** `docs/superpowers/specs/2026-10-30-derived-status-design.md` — jedes Kind liest Spec **und** seinen PR-Abschnitt hier.

## Global Constraints

- CLAUDE.md gilt: doppelte Anführungszeichen, f-Strings, Type Hints, Google-Docstrings, CBVs, ORM only, `related_name` + `__str__` + `Meta.ordering` an jedem neuen Modell, RBAC-Mixins an jedem schreibenden View.
- Migrationen **nur** via `makemigrations`; Datenmigrationen als `RunPython` mit `reverse_code` (mind. `noop`).
- CI-Gate je PR: `uv lock --check`, `ruff check`, `ruff format --check`, `makemigrations --check`, `check --fail-level WARNING`, Tests grün, Coverage ≥ 55.
- Kein N+1: Listen (Auftragsliste, Queue-Liste, Dashboard, Projekt-Detail) bekommen Query-Count-Tests (`assertNumQueries` bzw. bestehende Muster in `test_views_jobs.py` / `test_views_dashboard.py`).
- **Stückzahlen und Projektfortschritt dürfen durch keine Migration sinken.**
- Kein Weg (UI, REST-API, Admin) löscht Druckhistorie still.
- Abhängigkeiten: PR-A → PR-B → PR-D → PR-E; PR-C unabhängig (parallel zu A möglich).
- Moonraker-WS-Fix #93 ist gemergt; `printer_status_sync.py` / `moonraker_ws.py` auf aktuellem main aufsetzen.

## Review Focus

1. Moonraker meldet „Druck fertig“ für einen Versuch, der inzwischen entfernt/abgebrochen (`cancelled`) wurde → darf ihn nicht wieder öffnen, kein neuer Zustand. (Test in Task A3.)
2. Gleichzeitige Prüfung aus Web und KlipperScreen → genau ein Urteil, zweites bekommt `ReviewStateError` (bestehendes `select_for_update` beibehalten). (Test in Task A3.)
3. Drucker wird deaktiviert, während er wartende Versuche hat → diese starten nicht, bleiben sichtbar, können umgehängt/entfernt werden. (Test in Task A4.)
4. Einplatten-G-Code ohne Objekt-Labels bzw. leere Plattenliste → keine Exception, Fallback „alle Teile auf diese Platte“ bzw. Warnung. (Test in Task D2.)
5. Prod-ähnlicher Altbestand (Platten `completed`/`failed`, offene Einträge in allen drei Status, Job mit fehlender G-Code-Datei) → nach allen Migrationen gleiche oder höhere Stückzahlen. (Tests in A5, D3, E1.)

---

## PR-A: Druckversuch-Historie

Branch `feat/print-attempt-history`. Ziel: `PrintQueue`-Zeilen bleiben, Queue-Status abgeleitet. `PrintJobPlate.status` existiert noch, wird aber **nur** noch von einer Methode geschrieben.

### Task A1: Felder + abgeleiteter Versuchszustand

**Files:**
- Modify: `core/models/queue.py` (PrintQueue), `core/models/printers.py` (PrinterProfile)
- Test: `core/tests/test_print_attempts.py` (neu)

**Interfaces:**
- Produces:
  - `PrintQueue.RESULT_PASSED = "passed"`, `RESULT_FAILED = "failed"`, `RESULT_CANCELLED = "cancelled"`; Felder `result: CharField(blank=True, default="")`, `resolved_at: DateTimeField(null=True)`, `is_retry: BooleanField(default=False)`; `printer` → `null=True, on_delete=PROTECT`.
  - `PrinterProfile.is_active: BooleanField(default=True)`.
  - `PrintQueueQuerySet` als Manager: `.open()` (result=""), `.waiting()`, `.printing()`, `.awaiting_review()`, `.resolved()`.
  - `PrintQueue.state -> str` ∈ {`"waiting"`, `"starting"`, `"printing"`, `"awaiting_review"`, `"passed"`, `"failed"`, `"cancelled"`} (Regeln: Spec §1-Tabelle; `"starting"` = waiting + aktiver Claim wie `active_start_claim_q`).
  - UniqueConstraint `printqueue_one_open_attempt_per_plate`: `fields=["plate"], condition=Q(result="")` (ersetzt `printqueue_unique_plate_when_waiting`).
  - `status`/`retry_count` bleiben in A1 noch (werden in A5 entfernt).

- [ ] **Step 1: Failing tests** — `test_state_waiting_when_not_started`, `test_state_printing_when_started_not_completed`, `test_state_awaiting_review_when_completed_no_result`, `test_state_resolved_values`, `test_open_queryset_excludes_resolved`, `test_second_open_attempt_for_plate_violates_constraint` (`IntegrityError`), `test_resolved_attempt_does_not_block_new_open_attempt`.
- [ ] **Step 2:** `uv run python manage.py test core.tests.test_print_attempts` → FAIL.
- [ ] **Step 3:** Felder, QuerySet, Property, Constraint; `makemigrations`.
- [ ] **Step 4:** Tests → PASS.
- [ ] **Step 5:** Commit `feat(queue): attempt result fields and derived attempt state`.

### Task A2: Übergänge als Modell-Methoden

**Files:**
- Modify: `core/models/queue.py`, `core/models/printing.py`, `core/services/queue.py` (`review_queue_entry`, `mark_queue_entry_started`, `start_next_for_printer`), `core/services/printer_status_sync.py:~157`, `core/views/queue.py` (`CancelQueueEntryView:~577`, `PrintQueueDeleteView:~246-283`), `core/views/printers.py:~160` (`UploadToPrinterView`)
- Test: `core/tests/test_print_attempts.py`, bestehende `test_services_queue.py`, `test_views_queue.py`, `test_services_printer_status_sync.py`, `test_services_operator_dialog.py` anpassen

**Interfaces:**
- Produces (alle in `core/models/queue.py` an `PrintQueue`, jede in `transaction.atomic` wo mehrere Zeilen betroffen):
  - `mark_started(self, *, when: datetime | None = None) -> None`
  - `mark_finished(self, *, when: datetime | None = None, error: str = "") -> None` — No-op, wenn `result` gesetzt.
  - `resolve(self, passed: bool) -> str` — liefert Outcome `"passed" | "requeued" | "discarded"`; bei Fail und nicht ausgeschöpften Fehlversuchen neuer Versuch `is_retry=True`, `position=0`, gleicher Drucker/Priorität.
  - `remove(self) -> None` — nie gestartet → `delete()`; sonst `result="cancelled"`, `resolved_at`.
- Produces an `PrintJobPlate` (`core/models/printing.py`):
  - `failed_attempts_since_manual_enqueue(self) -> int` — zählt `failed` nach dem letzten Versuch mit `is_retry=False` (inkl. diesem).
  - `refresh_stored_status(self) -> None` — **einzige** Stelle, die `PrintJobPlate.status` schreibt (Übergangslösung bis PR-B).
- `review_queue_entry(...)` behält Signatur + `ReviewResult`; delegiert an `resolve()`. `ReviewResult.retry_count` = `failed_attempts_since_manual_enqueue()`.
- `CancelQueueEntryView`: Verhalten unverändert (Druck stoppen → `mark_finished`).
- `UploadToPrinterView`: setzt keinen Plattenstatus mehr.

- [ ] **Step 1: Failing tests** — `test_pass_keeps_attempt_and_completes_plate`, `test_fail_creates_retry_attempt_first_in_priority_group` (neuer Versuch `is_retry=True`, `position==0`, alter `result=="failed"`), `test_fail_after_max_failures_marks_plate_failed_and_creates_no_retry` (max_retries=3), `test_manual_requeue_resets_failure_counter`, `test_remove_unstarted_deletes`, `test_remove_started_marks_cancelled_and_frees_plate`, `test_cancelled_never_counts_as_failure`, `test_upload_to_printer_does_not_touch_plate_status`.
- [ ] **Step 2:** Run → FAIL.
- [ ] **Step 3:** Methoden implementieren, Aufrufer umstellen; `grep -rn "status=PrintQueue.STATUS\|\.status = PrintQueue\|plate.status =" core --include=*.py` darf nur noch in `core/models/` treffen.
- [ ] **Step 4:** `uv run python manage.py test core` → PASS.
- [ ] **Step 5:** Commit `refactor(queue): attempt transitions as model methods, keep history`.

### Task A3: Leser auf offene Versuche umstellen

**Files:**
- Modify: `core/views/queue.py` (Liste, `_queueable_plates:~86`, Sortier-`Case/When:~115`), `core/views/dashboard.py:79-94,149-193,277-291`, `core/services/operator_dialog.py` (`pending_review_entry`), `core/services/moonraker_operator_ui.py:~201,212`, `core/management/commands/moonraker_worker.py:~85`, `core/models/queue.py:~152,157` (Busy-Check), `core/services/queue.py:~139,169`
- Test: bestehende Tests + `core/tests/test_print_attempts.py`

**Interfaces:**
- Consumes: `PrintQueue.objects.open()/.waiting()/.printing()/.awaiting_review()`, `PrintQueue.state`.

- [ ] **Step 1: Failing tests** — `test_queue_list_hides_resolved_attempts`, `test_worker_active_entry_ignores_resolved`, `test_finish_event_for_cancelled_attempt_is_ignored` (Review Focus 1), `test_concurrent_review_second_call_raises` (Review Focus 2), `test_farm_card_state_from_open_attempts`, Query-Count-Test Queue-Liste unverändert.
- [ ] **Step 2:** Run → FAIL.
- [ ] **Step 3:** Alle Filter auf `status=` durch QuerySet-Methoden ersetzen.
- [ ] **Step 4:** Volle Suite → PASS.
- [ ] **Step 5:** Commit `refactor(queue): read paths use open attempts`.

### Task A4: Drucker deaktivieren + Löschschutz

**Files:**
- Modify: `core/forms/printers.py`, `core/views/printers.py` (`PrinterProfileDeleteView:110`), `core/templates/core/printer*` , `core/services/queue.py` (`start_next_for_printer`), `core/views/dashboard.py` (Farm-Karten), Drucker-Auswahl im Queue-Formular
- Test: `core/tests/test_views_printers.py`, `core/tests/test_services_queue.py`

**Interfaces:**
- Produces: `PrinterProfile.has_print_history(self) -> bool`; `PrinterProfile.objects.active()`.

- [ ] **Step 1: Failing tests** — `test_delete_printer_with_attempts_refused_with_message` (kein 500, Objekt existiert noch), `test_delete_printer_without_attempts_works`, `test_inactive_printer_does_not_start_waiting_attempts` (Review Focus 3), `test_inactive_printer_hidden_from_farm_and_queue_form`, `test_is_active_editable_by_printer_manager_only`.
- [ ] **Step 2–4:** FAIL → implementieren → PASS.
- [ ] **Step 5:** Commit `feat(printers): deactivate printers, protect print history`.

### Task A5: Datenmigration + Altfelder entfernen

**Files:**
- Create: `core/migrations/00xx_backfill_print_attempts.py` (über `makemigrations --empty core`), danach Feld-Entfernung via `makemigrations`
- Test: `core/tests/test_migration_print_attempts.py` (Muster: `test_migration_tree.py`)

**Interfaces:**
- Backfill-Regeln (Spec „Migration“ Punkt 1): je `completed`-Platte ohne bestandenen Versuch ein `passed`-Versuch (`started_at`/`completed_at`/`resolved_at` aus Platte, `printer` aus `PrintJob.printer` sonst `None`); je `failed`-Platte `max_retries` `failed`-Versuche (erster `is_retry=False`); offene Einträge: `status=printing` ohne `started_at` → `started_at=status_updated_at or added_at`; `awaiting_review` ohne `completed_at` → `completed_at=status_updated_at or added_at`.
- Danach `PrintQueue.status` und `retry_count` entfernen.

- [ ] **Step 1: Failing test** — `test_backfill_creates_passed_attempt_for_completed_plate`, `test_backfill_failed_plate_derives_failed`, `test_backfill_translates_open_statuses`, `test_backfill_is_idempotent`, `test_printed_quantity_unchanged_after_backfill` (Review Focus 5, Fixture: Job mit 2/2 completed + Job mit 1/2 completed).
- [ ] **Step 2–4:** FAIL → Migration → PASS; `makemigrations --check` sauber.
- [ ] **Step 5:** Commit `feat(migrations): backfill print attempts, drop queue status`.

**PR-A Abschluss:** PR öffnen, CI grün, Review-Loop bis 0, Merge, Prod-Deploy (Backup; Druck < 95 % sofort), danach Queue-Liste + Farm-Dashboard + Stückzahlen auf Prod gegen vorher prüfen.

---

## PR-B: Platten- und Auftragsstatus ableiten

Branch `feat/derived-plate-job-status`. Setzt PR-A voraus.

### Task B1: Platten-Status abgeleitet, Feld weg

**Files:**
- Modify: `core/models/printing.py`, alle Leser (`views/queue.py:_queueable_plates`, `views/dashboard.py:125,201,232`, `models/parts.py:28,327-329`, `services/queue.py:258`, Templates `printjob_detail.html` u. a.), Migration
- Test: `core/tests/test_plate_state.py` (neu)

**Interfaces:**
- Produces: `PrintJobPlate.state -> str` ∈ {`"completed"`, `"waiting"`, `"starting"`, `"printing"`, `"awaiting_review"`, `"failed"`, `"not_queued"`} (Priorität Spec §2); `PrintJobPlate.open_attempt -> PrintQueue | None`; `PrintJobPlate.completed_at -> datetime | None` (Property aus bestandenem Versuch); `PrintJobPlateQuerySet.with_state()` (Annotation `state` per `Exists`/`Subquery`, für Listen) und `.queueable()`.
- Entfernt: `PrintJobPlate.status`, `started_at`, `completed_at` (Felder), `refresh_stored_status()`.

- [ ] **Step 1: Failing tests** — je Zustand ein Test (`test_plate_state_<x>`), `test_plate_state_queued_shows_printer_and_position` (Topic 31: Anzeige-Text „Eingereiht · Drucker X, Position N“ im Detail-Template), `test_enqueue_button_hidden_for_queued_or_completed_plate`, `test_job_detail_shows_attempt_history_per_plate` (Drucker, Start, Ende, Ergebnis je erledigtem Versuch, Spec §1), `test_with_state_annotation_matches_property` (gleiche Werte für alle Fixtures), Query-Count Auftrags-Detail.
- [ ] **Step 2–4:** FAIL → implementieren + `makemigrations` → PASS.
- [ ] **Step 5:** Commit `refactor(plates): derive plate state from attempts`.

### Task B2: Auftragsstatus = Slicing-Fakt + abgeleiteter Anzeige-Status

**Files:**
- Modify: `core/models/printing.py`, `core/views/print_jobs.py` (Filter `:154-186`, `status_choices`, „Fertige ausblenden“ `:263-277`), `core/views/dashboard.py:65-67,119,224-263,357,373,427`, `core/templates/core/includes/_job_status_badge.html`, `core/admin.py` (`list_filter`), `core/models/parts.py:165`, `core/models/projects.py:330`, `core/views/projects.py:103`, `apps.py:92-93`, `services/slicing_worker.py`, Migration
- Test: `core/tests/test_job_display_status.py` (neu), `test_views_jobs.py`, `test_views_dashboard.py`

**Interfaces:**
- `PrintJob.status` Choices nur noch `draft, pending, slicing, sliced, failed` (Name des Felds bleibt `status`, Label „Slicing-Status“). Datenmigration: `printing`/`completed`/`uploading`/`uploaded`/`cancelled` → `sliced`.
- Produces: `PrintJob.DISPLAY_*` Konstanten `draft, pending, slicing, slicing_failed, ready, printing, in_progress, completed, partially_failed`; `PrintJob.display_status -> str`; `PrintJobQuerySet.with_display_status()` (Annotation `display_status`); `PrintJob.FINISHED_DISPLAY_STATUSES = ("completed", "partially_failed")`; `PrintJob.print_started -> bool` (irgendein Versuch zu einer Platte).
- `blocking_jobs()` (Part/Project) nutzt `with_display_status()` + „nicht in FINISHED“ statt `TERMINAL_STATUSES`; `TERMINAL_STATUSES` entfällt.
- Entfernt: `PrintJob.printer`, `started_at`, `completed_at`; Drucker-Filter der Liste = `plates__queue_entries__printer`.

- [ ] **Step 1: Failing tests** — Tabelle aus Spec §3 je Zeile ein Test; `test_job_with_failed_plate_and_unqueued_plate_is_in_progress`; `test_status_filter_multi_select_uses_display_status`; `test_hide_completed_hides_completed_and_partially_failed`; `test_printer_filter_matches_jobs_with_attempt_on_printer`; `test_dashboard_counts_by_display_status`; Query-Count Auftragsliste (≤ heutiger Wert).
- [ ] **Step 2–4:** FAIL → implementieren → PASS.
- [ ] **Step 5:** Commit `refactor(jobs): job status is slicing fact, print status derived`.

### Task B3: Bearbeiten, Neu-Slicen, Löschen

**Files:**
- Modify: `core/views/print_jobs.py` (`PrintJobUpdateView:366`, `AddPartToJobView:412`, `RemovePartFromJobView:520`, `PrintJobSliceView:649`, `PrintJobDeleteView:391`), `core/forms/print_jobs.py`, `printjob_detail.html`, `core/models/printing.py`
- Test: `core/tests/test_views_jobs.py`

**Interfaces:**
- Produces: `PrintJob.can_change_contents -> bool` (= `not print_started`); `PrintJob.discard_slicing(self) -> None` (Platten + Dateien löschen, `status="draft"`, Aggregatwerte leeren; Aufrufer: Teile-/Preset-Änderung wenn Status ≠ draft); `PrintJob.can_delete -> bool` (= `not print_started`).
- Formular: Name + Notiz immer; Preset nur bei `can_change_contents`.

- [ ] **Step 1: Failing tests** — `test_name_and_notes_editable_while_printing`, `test_add_part_after_slicing_resets_to_draft_and_deletes_plates`, `test_preset_change_after_slicing_resets_to_draft`, `test_add_part_refused_after_first_attempt` (403/Meldung, nichts geändert), `test_reslice_refused_after_first_attempt`, `test_delete_refused_after_first_attempt`, `test_delete_allowed_after_unstarted_attempt_removed`.
- [ ] **Step 2–4:** FAIL → implementieren → PASS.
- [ ] **Step 5:** Commit `feat(jobs): edit, reslice and delete rules follow print start`.

### Task B4: Löschschutz Teil/Projekt

**Files:**
- Modify: `core/models/parts.py` (`blocking_jobs:158`), `core/models/projects.py` (`blocking_jobs:314`, `deletion_summary:334`), Delete-Views (`views/parts.py`, `views/projects.py`), `core/api/views.py` (Delete-Endpoints `:129-161`, `:372-404`), `core/admin.py`
- Test: `core/tests/test_delete_guards.py`, `test_project_delete.py`, `test_api_projects.py`

**Interfaces:**
- Produces: `Part.has_print_history(self) -> bool` (in PR-B: Teil ist `PrintJobPart` eines Auftrags mit `print_started`; PR-D/E erweitern um Plattenteile/Buchungen); `Project.history_blocking_parts(self, ids: set[int] | None = None) -> QuerySet[Part]`.
- API: 409 mit Grund `"print_history"`; UI: Meldung im Lösch-Dialog; Admin: `has_delete_permission` respektiert Guard.

- [ ] **Step 1: Failing tests** — `test_delete_part_with_history_refused_ui`, `..._api_409`, `test_delete_project_with_history_in_subtree_refused`, `test_deletion_summary_reports_print_history`, `test_admin_cannot_delete_part_with_history`.
- [ ] **Step 2–4:** FAIL → implementieren → PASS.
- [ ] **Step 5:** Commit `feat(guards): parts and projects with print history are not deletable`.

**PR-B Abschluss:** wie PR-A (Review-Loop, Merge, Deploy, Prod-Prüfung Auftragsliste/Filter/Dashboard). Notiz an PM-Topic 31.

---

## PR-C: Teile-Schätzung bündeln (unabhängig)

Branch `refactor/part-request-estimate`.

### Task C1: `Part.request_estimate()` + Worker-Übergänge

**Files:**
- Modify: `core/models/parts.py`, `core/views/helpers.py:47,91`, `core/views/parts.py:252,345`, `core/views/projects.py:374`, `core/api/views.py:202,450`, `core/services/slicing_worker.py:219,312,322,377-408`, `core/apps.py:82`
- Test: `core/tests/test_estimation.py`, `test_api_estimation.py`

**Interfaces:**
- Produces an `Part`: `request_estimate(self) -> bool` (setzt `pending` + leert Schätzwerte/Fehler, gibt `False` zurück wenn Voraussetzungen fehlen — gleiche Bedingungen wie heute `_trigger_part_estimation`); `claim_estimate(cls, pk: int) -> bool` (pending→estimating, atomar); `finish_estimate(self, **values) -> None` (nur wenn noch `estimating`); `fail_estimate(self, error: str) -> None`.
- Danach schreibt außerhalb `core/models/parts.py` niemand `estimation_status` (grep-Check wie A2).

- [ ] **Step 1: Failing tests** — `test_request_estimate_resets_and_queues`, `test_request_estimate_without_prereqs_returns_false`, `test_finish_estimate_ignored_when_not_estimating`, bestehende Tests grün halten.
- [ ] **Step 2–4:** FAIL → implementieren → PASS.
- [ ] **Step 5:** Commit `refactor(parts): estimation transitions as model methods`.

**PR-C Abschluss:** Review-Loop, Merge, Deploy.

---

## PR-D: Platte kennt ihre Teile

Branch `feat/plate-parts`. Setzt PR-B voraus.

### Task D1: Deterministisches Bundle mit Objekt-Zuordnung

**Files:**
- Modify: `core/services/threemf.py` (`create_3mf_bundle:276`, `_build_3dmodel_xml:120`), `core/services/slicing_worker.py:434-438`
- Test: `core/tests/test_services_threemf.py`

**Interfaces:**
- Produces: `@dataclass(frozen=True) BundleObject(object_id: int, part_index: int, copy_index: int, mesh_index: int, mesh_count: int)`; `create_3mf_bundle(parts: list[tuple[Path | str, int]]) -> tuple[bytes, list[BundleObject]]` (alle Aufrufer anpassen); Worker iteriert `job.job_parts.order_by("pk")`.

- [ ] **Step 1: Failing tests** — `test_bundle_objects_numbered_per_copy` (2 Teile × Mengen 2/1 → object_ids 1..3 mit korrekten part_index/copy_index), `test_bundle_multi_mesh_part_has_mesh_count`.
- [ ] **Step 2–4:** FAIL → implementieren → PASS.
- [ ] **Step 5:** Commit `feat(threemf): record object-to-part mapping when bundling`.

### Task D2: G-Code parsen + `PrintJobPlatePart`

**Files:**
- Create: `core/services/gcode_objects.py`
- Modify: `core/models/printing.py` (neues Modell), `core/models/__init__.py`, `core/admin.py`, `core/services/slicing_worker.py:476-510`, Migration
- Test: `core/tests/test_gcode_objects.py` (neu), `test_services_slicing.py`

**Interfaces:**
- Produces: `parse_gcode_object_ids(gcode: bytes) -> list[int]` — liest `EXCLUDE_OBJECT_DEFINE NAME=Object_<N>_…`, Fallback `; printing object Object_<N>`, eindeutig + sortiert.
- Modell `PrintJobPlatePart(plate FK→PrintJobPlate CASCADE related_name="plate_parts", part FK→Part PROTECT related_name="plate_entries", copy_index: PositiveIntegerField, meshes_on_plate: PositiveIntegerField, mesh_count: PositiveIntegerField)`; unique (`plate`, `part`, `copy_index`).
- `PrintJob.plate_parts_known: BooleanField(default=True)` — `False`, wenn Zuordnung nicht ermittelbar (dann gilt die alte „alle Platten fertig“-Regel).
- `PrintJob.unplaced_copies(self) -> dict[int, int]` (part_pk → Anzahl Kopien mit fehlenden Meshes); Warnung im Detail-Template „N× <Teil> liegen auf keiner Platte“.
- Fallback: genau eine Platte und keine Labels → alle Objekte dieser Platte; mehrere Platten ohne Labels → `plate_parts_known=False` + Warnung „Zuordnung unbekannt“.

- [ ] **Step 1: Failing tests** — `test_parse_exclude_object_define` (Zeilen aus Spec-Spike), `test_parse_printing_object_fallback`, `test_parse_no_labels_returns_empty`, `test_slicing_creates_plate_parts`, `test_unplaced_copies_warning` (25 Kopien, 23 auf Platten → 2), `test_single_plate_without_labels_maps_all` (Review Focus 4), `test_multi_plate_without_labels_marks_unknown`.
- [ ] **Step 2–4:** FAIL → implementieren → PASS.
- [ ] **Step 5:** Commit `feat(plates): plates know their parts from G-code object labels`.

### Task D3: Backfill aus vorhandenen G-Code-Dateien

**Files:**
- Create: Datenmigration via `makemigrations --empty core` (nutzt `parse_gcode_object_ids` als reine Funktion; Mesh-Anzahl aus aktueller Teildatei)
- Test: `core/tests/test_migration_plate_parts.py`

**Interfaces:**
- Annahme Altbestand: Objekt-IDs in `PrintJobPart`-pk-Reihenfolge. Passt Summe der Objekte nicht zu Σ Menge × Meshes, oder fehlt eine Datei → `plate_parts_known=False` für den Auftrag, keine Plattenteile.

- [ ] **Step 1: Failing tests** — `test_backfill_maps_objects_in_pk_order`, `test_backfill_missing_file_marks_unknown`, `test_backfill_count_mismatch_marks_unknown`, `test_backfill_leaves_unplaced_copies_visible`.
- [ ] **Step 2–4:** FAIL → Migration → PASS.
- [ ] **Step 5:** Commit `feat(migrations): backfill plate parts from existing G-code`.

`Part.has_print_history()` zusätzlich: Teil hat `plate_entries` auf Platten mit Versuch.

**PR-D Abschluss:** Review-Loop, Merge, Deploy, auf Prod Warnung bei Job 41 prüfen.

---

## PR-E: Gedruckte Stückzahl + manuelle Buchung

Branch `feat/printed-quantity-records`. Setzt PR-D voraus.

### Task E1: `printed_quantity` neu

**Files:**
- Modify: `core/models/parts.py:19-29,255-336`, `core/models/projects.py` (`aggregate_prefetch_lookups:251`, `build_progress:595-624`)
- Test: `core/tests/test_printed_quantity.py` (neu), `test_aggregation.py`

**Interfaces:**
- `Part.printed_quantity -> int` = Kopien (pro Auftrag + `copy_index`), deren `Σ meshes_on_plate` über Platten mit bestandenem Versuch `== mesh_count`, **plus** für Aufträge mit `plate_parts_known=False` die alte Regel (alle Platten bestanden → `PrintJobPart.quantity`), **plus** `Σ PrintedPartRecord.quantity`. Prefetch-Pfad und DB-Pfad liefern identische Werte.
- Fortschritt wie heute je Teil beim Bedarf gedeckelt.

- [ ] **Step 1: Failing tests** — `test_partially_printed_job_counts_passed_plates` (Job-33-Fall: Platten 1+2 bestanden, 3 offen), `test_multi_mesh_copy_counts_only_when_all_meshes_passed`, `test_unknown_mapping_job_uses_legacy_rule`, `test_manual_records_add_up`, `test_prefetched_equals_db_path`, `test_progress_capped_at_need`, `test_counts_not_lower_than_legacy_rule` (Review Focus 5).
- [ ] **Step 2–4:** FAIL → implementieren → PASS.
- [ ] **Step 5:** Commit `feat(parts): printed quantity from passed plates and records`.

### Task E2: `PrintedPartRecord` + UI + API

**Files:**
- Modify: `core/models/parts.py`, `core/models/__init__.py`, `core/admin.py`, `core/forms/parts.py`, `core/views/parts.py`, `core/urls/parts.py`, `core/templates/core/part_detail.html`, `core/api/views.py`, `core/api/serializers.py`, `core/api/urls.py`, Migration
- Test: `core/tests/test_views_parts.py`, `core/tests/test_api_printed_records.py` (neu), `test_permissions.py`

**Interfaces:**
- Modell `PrintedPartRecord(part FK→Part PROTECT related_name="printed_records", quantity: PositiveIntegerField(validators=[MinValueValidator(1)]), printed_on: DateField(default=today), note: CharField(max_length=255, blank=True), created_by FK→User SET_NULL null, created_at auto_now_add)`; `Meta.ordering = ["-printed_on", "-pk"]`.
- Views `PrintedPartRecordCreateView`, `PrintedPartRecordDeleteView` mit `ProjectManageMixin`; Teil-Detail zeigt Buchungsliste **und** read-only die automatisch gezählten Platten (Auftrag, Platte, Anzahl, Datum).
- API: `GET/POST /api/v1/parts/<id>/printed-records/`, `DELETE /api/v1/parts/<id>/printed-records/<rid>/`, Berechtigung wie andere Part-Schreibaktionen.
- `Part.has_print_history()` zusätzlich: `printed_records` existieren.

- [ ] **Step 1: Failing tests** — `test_designer_can_add_record`, `test_operator_cannot_add_record` (403), `test_quantity_zero_rejected`, `test_delete_record_reduces_printed_quantity`, `test_api_create_and_list_records`, `test_part_detail_shows_auto_counted_plates`, `test_part_with_records_not_deletable`.
- [ ] **Step 2–4:** FAIL → implementieren → PASS.
- [ ] **Step 5:** Commit `feat(parts): manual printed-part records`.

**PR-E Abschluss:** Review-Loop, Merge, Deploy, Prod-Stückzahlen vorher/nachher vergleichen. Danach Hinweis an User: Altfälle (Job 33 → R02, R05–R09) per Buchung nachtragen. Notiz an PM-Topic 30, dann `pm_report done`.
