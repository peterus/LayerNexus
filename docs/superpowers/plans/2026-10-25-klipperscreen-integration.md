# KlipperScreen Operator Dialogs Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** When a LayerNexus print ends, the printer's touchscreen (KlipperScreen, also Mainsail) asks Good/Bad and offers to start the next queued job, with the same DB effects as the web review/Run-Next pages.

**Architecture:** Three layers. (1) Printer-agnostic queue actions extracted from the views into `core/services/queue.py`. (2) A printer-agnostic operator-dialog model (`core/services/operator_dialog.py`): dataclasses for dialogs/buttons/abstract actions plus builders and an action handler. (3) A Moonraker/Klipper adapter (`core/services/moonraker_operator_ui.py`, imported **only** by `moonraker_worker`) that renders dialogs as `action:prompt_*` `RESPOND` lines and parses `LN …` macro echoes from `notify_gcode_response`. Thin echo macros ship in `klipper/`.

**Tech Stack:** Django 6.1, asyncio + `asgiref.sync_to_async`, `websockets`, `requests` (existing `MoonrakerClient`), Klipper `[respond]` + macro prompts.

**Spec:** `docs/superpowers/specs/2026-10-25-klipperscreen-integration-design.md`

## Global Constraints

- Moonraker/Klipper-specific code runs **only in `moonraker_worker`**; `core/services/moonraker_operator_ui.py` must not be imported by views, forms, API or `operator_dialog.py`.
- Layers 1–2 contain no G-code, no Moonraker calls (other than via `get_printer_backend()` when starting a print), no prompt escaping.
- A print is never started automatically; only an explicit `StartNext` action starts one.
- Web Pass/Fail and Run-Next behaviour and flash-message texts stay byte-identical (existing `core/tests/test_views_queue.py` must pass unchanged).
- Opt-in flag `PrinterProfile.operator_prompts_enabled`, default `False`; worker sends nothing and ignores `LN` echoes for printers without it.
- Starting a print must not hold the SQLite write lock during the G-code upload: validate in a short `transaction.atomic()`, upload outside it.
- UI text in English. Review stays per plate (no per-part review). Filament is display-only.
- LayerNexus never writes printer config files; `klipper/*.cfg|conf` are installed by hand (option A).
- CLAUDE.md style: double quotes, f-strings, type hints, Google docstrings, `unittest.mock` for Moonraker, `makemigrations` only.

## Review Focus

1. Job/project names with `"`, `;`, `#`, `|`, newlines or umlauts must not break the `RESPOND` command or the button field split — Task 5 `test_sanitize_*`.
2. A stale/double button press (entry already reviewed on the web, pressed twice, or pk of another printer) must change nothing and show "Already handled" — Task 4 `test_handle_review_*_already_handled`, `test_handle_review_other_printer`.
3. Prints started manually (no queue entry) must never raise a dialog — Task 6 `test_no_dialog_without_queue_entry`.
4. Start pressed while the printer is busy or the shown entry is no longer next must not start anything — Task 2 `test_start_next_busy`, `test_start_next_not_next`.
5. A Moonraker error when sending the prompt (printer offline, macros missing) must be logged and must not crash the printer task or block status sync — Task 6 `test_prompt_send_failure_is_logged`.

---

## File Structure

| File | Responsibility |
|---|---|
| `core/services/queue.py` (modify) | + `review_queue_entry`, `start_next_for_printer`, result dataclasses |
| `core/views/queue.py` (modify) | `QueueEntryReviewView.post`, `RunNextQueueView.post` delegate to services |
| `core/models/printers.py`, `core/forms/printers.py`, `core/templates/core/printerprofile_form.html`, migration (modify/new) | opt-in flag |
| `core/services/operator_dialog.py` (new) | dialog model, builders, `handle_action` |
| `core/services/moonraker.py` (modify) | + `MoonrakerClient.run_gcode` |
| `core/services/moonraker_operator_ui.py` (new) | render / sanitize / parse / `MoonrakerOperatorUI` |
| `core/management/commands/moonraker_worker.py` (modify) | wire UI into per-printer task |
| `klipper/layernexus.cfg`, `klipper/layernexus_klipperscreen.conf` (new) | printer-side macros + menu |
| `docs/integrations/klipperscreen.md`, `mkdocs.yml` (new/modify) | setup guide |

---

### Task 1: Extract review logic into a service

**Files:**
- Modify: `core/services/queue.py`, `core/views/queue.py:427-528` (`QueueEntryReviewView.post`)
- Test: `core/tests/test_services_queue.py`

**Interfaces:**
- Produces:
  ```python
  class ReviewStateError(Exception): ...  # entry not awaiting_review

  @dataclass(frozen=True)
  class ReviewResult:
      outcome: str            # "passed" | "requeued" | "discarded"
      plate_label: str        # f"Plate {n} of '{job}'"  (exact web text)
      plates_completed: int   # for "passed"
      plate_count: int
      retry_count: int
      max_retries: int

  def review_queue_entry(entry_pk: int, passed: bool, *, printer_pk: int | None = None) -> ReviewResult
  ```
  Runs in `transaction.atomic()`; re-fetches the entry inside the transaction; raises `ReviewStateError` if missing, not `awaiting_review`, or (when `printer_pk` given) on another printer. Pass/fail effects exactly as the current view (plate completed + job completed when all plates done; fail → retry_count+1, requeue at `position=0` or plate failed + delete at max).

- [ ] **Step 1: Write failing tests** in `test_services_queue.py`, class `ReviewQueueEntryTests`:
  - `test_pass_completes_plate_and_deletes_entry` → `outcome == "passed"`, plate `completed`, entry gone.
  - `test_pass_last_plate_completes_job` → job status `completed`.
  - `test_fail_requeues_with_position_zero` → `outcome == "requeued"`, entry `waiting`, `retry_count == 1`, `position == 0`, plate `waiting`.
  - `test_fail_at_max_retries_discards` (retry_count=2, max_retries=3) → `outcome == "discarded"`, plate `failed`, entry gone.
  - `test_not_awaiting_review_raises` (status `waiting`) → `ReviewStateError`.
  - `test_other_printer_raises` (`printer_pk` of another printer) → `ReviewStateError`.
- [ ] **Step 2:** `DEBUG=1 uv run python manage.py test core.tests.test_services_queue.ReviewQueueEntryTests` → FAIL (ImportError).
- [ ] **Step 3: Implement** `review_queue_entry` + `ReviewResult` + `ReviewStateError` in `core/services/queue.py`; rewrite `QueueEntryReviewView.post` to call it (keep `_get_entry` 404 + "Invalid action." branch) and build the **same** messages from `ReviewResult`.
- [ ] **Step 4:** `DEBUG=1 uv run python manage.py test core.tests.test_services_queue core.tests.test_views_queue` → PASS.
- [ ] **Step 5: Commit** `refactor(queue): extract review_queue_entry service`.

### Task 2: Extract Run-Next logic into a service

**Files:**
- Modify: `core/services/queue.py`, `core/views/queue.py:308-356` (`RunNextQueueView.post`)
- Test: `core/tests/test_services_queue.py`

**Interfaces:**
- Consumes: `next_waiting_entry`, `start_print_for_queue_entry`, `PrintStartError`, `PrinterError`.
- Produces:
  ```python
  @dataclass(frozen=True)
  class StartResult:
      outcome: str                  # "started" | "busy" | "empty" | "not_next" | "not_configured" | "error"
      entry: PrintQueue | None      # started / next entry when relevant
      busy_entry: PrintQueue | None # for "busy"
      error: str = ""               # user-facing text for "error"

  def start_next_for_printer(printer: PrinterProfile, *, expected_entry_pk: int | None = None) -> StartResult
  ```
  Busy check + next-entry lookup (+ `expected_entry_pk` match → else `not_next`) inside a short `transaction.atomic()`; `start_print_for_queue_entry` called **after** the block. `PrintStartError` → `error=str(exc)`; `PrinterError` → logged with `logger.exception`, `error="Failed to start print. Please check the printer connection and try again."`. No `moonraker_url` → `not_configured`.

- [ ] **Step 1: Write failing tests** (`StartNextForPrinterTests`, mock `core.services.queue.get_printer_backend`):
  - `test_start_next_starts_first_waiting` → `outcome == "started"`, backend `start_print` called once.
  - `test_start_next_busy` (an entry `printing` or `awaiting_review`) → `"busy"`, backend not called.
  - `test_start_next_empty` → `"empty"`.
  - `test_start_next_not_next` (`expected_entry_pk` ≠ next) → `"not_next"`, backend not called.
  - `test_start_next_printer_error` (backend raises `PrinterError`) → `"error"`, entry still `waiting`.
- [ ] **Step 2:** run `core.tests.test_services_queue.StartNextForPrinterTests` → FAIL.
- [ ] **Step 3: Implement**; rewrite `RunNextQueueView.post` to map outcomes to the existing messages verbatim. `RunAllQueuesView` stays unchanged.
- [ ] **Step 4:** `DEBUG=1 uv run python manage.py test core.tests.test_services_queue core.tests.test_views_queue` → PASS.
- [ ] **Step 5: Commit** `refactor(queue): extract start_next_for_printer service`.

### Task 3: Per-printer opt-in flag

**Files:**
- Modify: `core/models/printers.py` (PrinterProfile), `core/forms/printers.py`, `core/templates/core/printerprofile_form.html` (only if fields are rendered explicitly)
- Create: migration via `makemigrations`
- Test: `core/tests/test_views_printers.py`

**Interfaces:**
- Produces: `PrinterProfile.operator_prompts_enabled: BooleanField(default=False, verbose_name="Show operator dialogs on the printer", help_text="Show review / next-job dialogs on the printer's screen (KlipperScreen, Mainsail). Requires the LayerNexus macros on the printer.")`

- [ ] **Step 1: Failing test** `test_update_printer_sets_operator_prompts_enabled`: POST the edit form with `operator_prompts_enabled=on` as Operator → field is `True`; and `test_operator_prompts_enabled_defaults_false`.
- [ ] **Step 2:** run → FAIL.
- [ ] **Step 3:** add field, add to `PrinterProfileForm.Meta.fields`, render checkbox in template if needed; `uv run python manage.py makemigrations core`.
- [ ] **Step 4:** run tests + `uv run python manage.py makemigrations --check --dry-run` → PASS / "No changes detected".
- [ ] **Step 5: Commit** `feat(printers): opt-in flag for operator dialogs on the printer`.

### Task 4: Printer-agnostic operator dialog model

**Files:**
- Create: `core/services/operator_dialog.py`
- Test: `core/tests/test_services_operator_dialog.py`

**Interfaces:**
- Consumes: `review_queue_entry`, `ReviewResult`, `ReviewStateError` (Task 1); `start_next_for_printer`, `StartResult`, `next_waiting_entry` (Task 2).
- Produces:
  ```python
  @dataclass(frozen=True) class Review:    entry_pk: int; passed: bool
  @dataclass(frozen=True) class StartNext: entry_pk: int
  @dataclass(frozen=True) class Dismiss:   pass
  @dataclass(frozen=True) class ShowMenu:  pass
  OperatorAction = Review | StartNext | Dismiss | ShowMenu

  @dataclass(frozen=True) class DialogButton:   label: str; action: OperatorAction; style: str = "primary"  # primary|secondary|warning|error
  @dataclass(frozen=True) class OperatorDialog: title: str; lines: tuple[str, ...]; buttons: tuple[DialogButton, ...]

  DIALOG_TITLE = "LayerNexus"
  def filament_summary(job: PrintJob) -> str
  def review_dialog(entry: PrintQueue) -> OperatorDialog
  def next_job_dialog(printer: PrinterProfile, header_lines: tuple[str, ...] = ()) -> OperatorDialog
  def menu_dialog(printer: PrinterProfile) -> OperatorDialog
  def handle_action(printer: PrinterProfile, action: OperatorAction) -> OperatorDialog | None  # None = close
  ```
  Sync functions (called by the worker via `sync_to_async`).
  - `filament_summary`: distinct non-empty `(material, color)` of `job.job_parts` → `"PLA Black"`, joined with `", "`; none → `"Filament: not specified"`; otherwise prefixed `"Filament: "`.
  - `review_dialog`: lines `[str(job), f"Plate {n} of {plate_count}"]` + `f"Ended: {entry.last_error}"` if `last_error`; buttons `Good` (`Review(pk, True)`, primary) / `Bad` (`Review(pk, False)`, error).
  - `next_job_dialog`: header_lines first; if printer busy → `"Printer busy"` + `Close`; next entry → `f"Next: {job}, plate {n}"`, `filament_summary`, `f"Est. time: {…}"` if `plate.print_time_estimate`, buttons `Start` (`StartNext(pk)`) / `Later` (`Dismiss`, secondary); none → `"Queue empty"` + `Close` (`Dismiss`).
  - `menu_dialog`: an `awaiting_review` entry on this printer → `review_dialog`, else `next_job_dialog(printer)`.
  - `handle_action`: `Review` → `review_queue_entry(..., printer_pk=printer.pk)`; result line (`"Plate passed (3/5 plates done)"`, `"Failed - requeued (retry 1/3)"`, `"Failed - max retries reached, removed from queue"`) → `next_job_dialog(printer, (line,))`. `ReviewStateError` → dialog `"Already handled"` + `Close`. `StartNext` → `start_next_for_printer(printer, expected_entry_pk=pk)`; `started` → dialog `f"Started: {job}, plate {n}"` + `Close`; `not_next`/`busy`/`empty` → `"Already handled"`-style dialog with the reason; `error` → dialog with `result.error` + `Close`. `Dismiss` → `None`. `ShowMenu` → `menu_dialog`. Every action logged with `logger.info` incl. printer name ("from printer").

- [ ] **Step 1: Failing tests**:
  - `test_filament_summary_single`, `test_filament_summary_multiple_distinct`, `test_filament_summary_none`.
  - `test_review_dialog_buttons` → buttons `Review(pk, True)` / `Review(pk, False)`; `test_review_dialog_shows_error_state` (`last_error="Printer reported: cancelled"`).
  - `test_next_job_dialog_shows_filament_and_start` ; `test_next_job_dialog_queue_empty` ; `test_next_job_dialog_busy`.
  - `test_menu_dialog_prefers_pending_review`.
  - `test_handle_review_pass_returns_next_job_dialog` (first line `"Plate passed (1/1 plates done)"`, contains `StartNext` button — and asserts **no** print started, backend mock not called).
  - `test_handle_review_twice_already_handled`, `test_handle_review_other_printer` → `"Already handled"`, DB unchanged.
  - `test_handle_start_next_started` (backend mocked), `test_handle_start_next_error_shows_message`.
  - `test_handle_dismiss_returns_none`.
  - `test_module_has_no_moonraker_imports`: `"moonraker" not in inspect.getsource(operator_dialog)`.
- [ ] **Step 2:** run `core.tests.test_services_operator_dialog` → FAIL.
- [ ] **Step 3: Implement** `core/services/operator_dialog.py`.
- [ ] **Step 4:** run → PASS.
- [ ] **Step 5: Commit** `feat(operator): printer-agnostic operator dialog model`.

### Task 5: Moonraker prompt adapter + printer files

**Files:**
- Modify: `core/services/moonraker.py` (+ `run_gcode`)
- Create: `core/services/moonraker_operator_ui.py`, `klipper/layernexus.cfg`, `klipper/layernexus_klipperscreen.conf`
- Test: `core/tests/test_services_moonraker_operator_ui.py`, `core/tests/test_services_moonraker.py`

**Interfaces:**
- Consumes: Task 4 dataclasses.
- Produces:
  ```python
  # moonraker.py
  def run_gcode(self, script: str) -> dict  # POST printer/gcode/script, json={"script": script}

  # moonraker_operator_ui.py
  RESPONSE_PREFIX = "LN"
  END_PROMPT_SCRIPT = 'RESPOND TYPE=command MSG="action:prompt_end"'
  def sanitize_prompt_text(text: str, max_len: int = 80) -> str
  def action_to_gcode(action: OperatorAction) -> str
  def render_prompt(dialog: OperatorDialog) -> str          # newline-joined script
  def parse_operator_response(line: str) -> OperatorAction | None
  ```
  Wire format (pinned; macros and parser must agree):
  - `Review(12, True)` → `LN_REVIEW ENTRY=12 RESULT=PASS`; `False` → `RESULT=FAIL`
  - `StartNext(13)` → `LN_NEXT_JOB ENTRY=13`; `Dismiss` → `LN_CLOSE`; `ShowMenu` → `LN_MENU`
  - macro echoes (`RESPOND PREFIX=LN MSG="…"`, received as gcode response): `LN REVIEW ENTRY=12 RESULT=PASS`, `LN NEXT ENTRY=13`, `LN MENU` → parsed back to `Review` / `StartNext` / `ShowMenu`; anything else (incl. `// action:…` lines, `echo: …`) → `None`.
  - `render_prompt`: `prompt_end`, `prompt_begin <title>`, one `prompt_text` per line, buttons inside `prompt_button_group_start/_end` as `prompt_button <label>|<gcode>|<style>`, `prompt_show`; each as `RESPOND TYPE=command MSG="action:…"`.
  - `sanitize_prompt_text`: NFKD → ASCII; drop `"`, `;`, `#`, `|`, `\`, `{`, `}` and control chars (newlines → space); collapse whitespace; truncate to `max_len` with trailing `"..."`.
- `klipper/layernexus.cfg`: `[respond]` is **not** included (documented requirement, avoids duplicate-section errors). Macros `LN_REVIEW`, `LN_NEXT_JOB`, `LN_MENU`, `LN_CLOSE`: each first ends the prompt (`RESPOND TYPE=command MSG="action:prompt_end"`), then (except `LN_CLOSE`) echoes the line above via `RESPOND PREFIX=LN MSG="…"`, using `params.ENTRY|int` and `params.RESULT|upper`.
- `klipper/layernexus_klipperscreen.conf`: `[menu __main layernexus]` `name: LayerNexus`, `icon: print`, `method: printer.gcode.script`, `params: {"script":"LN_MENU"}`.

- [ ] **Step 1: Failing tests**:
  - `test_run_gcode_posts_script` (mock `requests.request`; asserts `POST`, `printer/gcode/script`, `json={"script": "G28"}`).
  - `test_action_to_gcode_*` for all four actions (exact strings above).
  - `test_parse_operator_response_roundtrip` for the three echo formats; `test_parse_ignores_prompt_and_noise` (`"// action:prompt_show"`, `"echo: hi"`, `"LN REVIEW ENTRY=x RESULT=PASS"`, `"LN REVIEW ENTRY=1 RESULT=MAYBE"`) → `None`.
  - `test_sanitize_strips_gcode_breakers` (`'Bracket "v2"; #3 | x\ny'` → no `"`,`;`,`#`,`|`,`\n`), `test_sanitize_umlauts` (`"Gehäuse"` → `"Gehause"`), `test_sanitize_truncates`.
  - `test_render_prompt_structure`: begins with prompt_end then `prompt_begin LayerNexus`, ends with `prompt_show`, contains `prompt_button Good|LN_REVIEW ENTRY=5 RESULT=PASS|primary`.
  - `test_klipper_cfg_defines_macros_matching_parser`: read `klipper/layernexus.cfg`, assert `[gcode_macro LN_REVIEW]`, `LN_NEXT_JOB`, `LN_MENU`, `LN_CLOSE` present and `PREFIX=LN` used; `test_klipperscreen_conf_calls_ln_menu`.
- [ ] **Step 2:** run both test modules → FAIL.
- [ ] **Step 3: Implement** `run_gcode`, the module functions, and the two `klipper/` files.
- [ ] **Step 4:** run → PASS.
- [ ] **Step 5: Commit** `feat(moonraker): render operator dialogs as Klipper macro prompts`.

### Task 6: Wire the adapter into `moonraker_worker`

**Files:**
- Modify: `core/services/moonraker_operator_ui.py` (+ `MoonrakerOperatorUI`), `core/management/commands/moonraker_worker.py`
- Test: `core/tests/test_management_moonraker_worker.py` (async tests, `TransactionTestCase` + `IsolatedAsyncioTestCase` pattern already used there)

**Interfaces:**
- Consumes: Tasks 4–5; `apply_status_event` (unchanged).
- Produces:
  ```python
  class MoonrakerOperatorUI:
      def __init__(self, printer: PrinterProfile, client: MoonrakerClient) -> None
      async def show(self, dialog: OperatorDialog | None) -> None          # None → END_PROMPT_SCRIPT
      async def on_gcode_response(self, params: list[Any]) -> bool         # True if an LN line was handled
      async def on_review_pending(self, entry_pk: int) -> None             # show review_dialog
      async def close_if_stale(self) -> None                               # open review entry no longer awaiting_review → close
  ```
  - `show` sends via `sync_to_async(client.run_gcode)`; `MoonrakerError` → `logger.warning`, swallowed. Remembers the entry pk of an open review dialog.
  - `on_gcode_response` parses each line; actions run via `sync_to_async(handle_action, thread_sensitive=False)` under a per-instance `asyncio.Lock` (so an upload doesn't block other printers' DB writes and double presses serialise); result → `show`.
  - Worker: in `_run_printer`, build `ui = MoonrakerOperatorUI(printer, MoonrakerClient(printer.moonraker_url, printer.moonraker_api_key))` **only if** `printer.operator_prompts_enabled`. In `on_event`: `notify_gcode_response` → `ui.on_gcode_response` (skip status handling). Otherwise as today, but detect the transition: status was `printing` before `apply_status_event` and is `awaiting_review` after → `await ui.on_review_pending(entry.pk)`. A side task in `_run_printer` calls `ui.close_if_stale()` every `DEFAULT_RELOAD_INTERVAL` seconds (cancelled with the printer task).
  - No dialog on worker start/reconnect; no re-send after Klipper restart (`LN_MENU` recovers).

- [ ] **Step 1: Failing tests** (mock `MoonrakerClient.run_gcode`):
  - `test_transition_shows_review_dialog` (printing entry + `notify_history_changed` finished, flag on) → one `run_gcode` call containing `LN_REVIEW ENTRY=<pk> RESULT=PASS`.
  - `test_no_prompt_when_flag_off` → `run_gcode` never called.
  - `test_no_dialog_without_queue_entry` (manual print, no entry) → not called.
  - `test_gcode_response_pass_reviews_and_shows_next` (`notify_gcode_response` `["LN REVIEW ENTRY=<pk> RESULT=PASS"]`) → plate completed; last script contains `Plate passed`; no print started.
  - `test_gcode_response_ignored_when_flag_off` → DB unchanged.
  - `test_prompt_send_failure_is_logged` (`run_gcode` raises `MoonrakerError`) → `assertLogs(level="WARNING")`, no exception, entry still `awaiting_review`.
  - `test_close_if_stale_closes_after_web_review` → after `review_queue_entry` elsewhere, `close_if_stale` sends `END_PROMPT_SCRIPT`.
  - `test_only_worker_imports_operator_ui`: grep `core/` for `moonraker_operator_ui` imports → only `moonraker_worker.py` and tests.
- [ ] **Step 2:** run `core.tests.test_management_moonraker_worker` → FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4:** full gate: `DEBUG=1 uv run python manage.py test core`, `uv run ruff check . && uv run ruff format --check .`, `uv run python manage.py check --fail-level WARNING` → all PASS.
- [ ] **Step 5: Commit** `feat(worker): operator dialogs on Klipper printers`.

### Task 7: Setup documentation

**Files:**
- Create: `docs/integrations/klipperscreen.md`
- Modify: `mkdocs.yml` (nav under Integrations: `- KlipperScreen: integrations/klipperscreen.md`), `docs/integrations/moonraker.md` (one-line link)

- [ ] **Step 1: Write** the guide: what it does (flow incl. "start is always explicit"); requirements (`[respond]` in `printer.cfg`, worker running); enable flag on the printer page; upload both `klipper/` files to `~/printer_data/config/`; add `[include layernexus.cfg]` to `printer.cfg` and `[include layernexus_klipperscreen.conf]` to `KlipperScreen.conf`; `FIRMWARE_RESTART` + restart KlipperScreen; "LayerNexus" menu entry recovers a dismissed dialog; same dialogs in Mainsail; note to re-upload the files when release notes say the macros changed.
- [ ] **Step 2:** `uv run ruff format --check .` → PASS (docs untouched by ruff besides code blocks).
- [ ] **Step 3: Commit** `docs: KlipperScreen operator dialog setup`.
