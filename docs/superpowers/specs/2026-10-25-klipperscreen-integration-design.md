# KlipperScreen integration — review & next job at the printer

Date: 2026-10-25 · Status: approved 2026-10-25

## Intent

Today, after a LayerNexus print finishes, the operator clears the bed and inspects the
parts at the printer, then has to walk to a PC/laptop to (1) mark the plate Pass/Fail in
LayerNexus and (2) press "Run Next" to start the next queued job. The goal is to do both
**on the printer's touchscreen (KlipperScreen)** so the operator never leaves the printer.

## Goals

1. When a LayerNexus-started print ends, KlipperScreen shows a dialog asking whether the
   plate is **Good** or **Bad**. The answer is written to the DB with exactly the same
   effect as Pass/Fail on the web review page (`QueueEntryReviewView`).
2. A way to **start the next waiting queue job** for this printer from KlipperScreen, with
   the same rules as "Run Next" (`RunNextQueueView`).
3. Before starting, the dialog shows **which filament (material + colour)** the next job
   expects, so the operator can swap the spool first. (User: "das wäre wichtig".)

## Non-goals

- Per-part pass/fail. Review stays per plate, as in the web UI today.
- Automatically starting the next job. **A start must always be an explicit button press**
  (user decision). Pressing "Good" never starts anything by itself.
- Checking/comparing the currently loaded spool (Spoolman active spool) against the
  expected filament — display only. (My call; possible follow-up.)
- Multi-printer UX beyond what falls out naturally. The user has one printer with
  KlipperScreen today, but nothing may be hard-wired to a single printer.

## Anti-goals

- Installing custom Python code into KlipperScreen (fragile across KS updates).
- Prints the user starts manually (not from LayerNexus) must never get a dialog.
- Two code paths with diverging review/run-next logic (web vs. printer).
- Silent failures: if a start or review fails, the operator sees the reason on the screen.

## Decided approach (user agreed: "gute Idee")

**No KlipperScreen plugin.** KlipperScreen has no plugin system (custom panels are just
files dropped into its `panels/` dir). Instead use the **Klipper macro-prompt protocol**
(`action:prompt_begin / _text / _button / _button_group_start / _end / _show / _end`),
which KlipperScreen renders natively (`screen.py` → `Prompt`) — and Mainsail as well.

All logic lives in LayerNexus' existing **`moonraker_worker`**, which already holds one
WebSocket per printer to Moonraker (`core/services/moonraker_ws.py`) and already detects
print end (`core/services/printer_status_sync.py` → sets the queue entry to
`awaiting_review`).

### Flow

1. **Print ends** (worker transitions the active queue entry to `awaiting_review`, which
   already happens for completed, cancelled and error states):
   the worker sends a prompt to that printer via Moonraker `printer.gcode.script` with
   `RESPOND TYPE=command MSG="action:prompt_..."` lines:
   - Title: `LayerNexus`
   - Text: job name, `Plate n of m`, and the end state if not completed
     (e.g. "Cancelled" / error text from `last_error`).
   - Buttons: **Good** / **Bad**.
2. **Operator presses Good/Bad** → the button runs a macro, e.g.
   `LN_REVIEW ENTRY=<queue_entry_pk> RESULT=PASS|FAIL`. The macro only echoes a
   recognisable line (`RESPOND ...`). The worker sees it as `notify_gcode_response` on its
   existing WebSocket and applies the review.
3. **Result feedback + next-job dialog** (one combined prompt):
   - Result line: "Plate passed (3/5 plates done)" / "Failed — requeued (retry 1/3)" /
     "Failed — max retries reached, removed from queue" — same wording/semantics as the
     web messages.
   - If a waiting entry exists for this printer: "Next: <job>, plate n — <material>
     <colour>" (+ estimated print time if known) with buttons **Start** / **Later**.
   - If the queue is empty: "Queue empty" with **Close**.
4. **Start** → macro `LN_NEXT_JOB ENTRY=<pk>` → worker starts exactly that entry via the
   same code as Run Next (printer must be free; entry must still be the next waiting
   one). Result is shown as a short prompt ("Started …" or the error).
   **Later** / **Close** → `action:prompt_end`, nothing else.
5. **KlipperScreen menu entry "LayerNexus"** (configured in `KlipperScreen.conf`, method
   `printer.gcode.script`, script `LN_MENU`) → worker re-shows the right dialog:
   the pending review dialog if an entry for this printer is `awaiting_review`, otherwise
   the next-job dialog (or "Queue empty" / "Printer busy"). This also recovers a dialog
   that was dismissed or missed.

### "Comes from LayerNexus" detection

Implicit: only the worker raises dialogs, and only for its own queue entries (it already
tracks the active entry per printer; uploaded files are named `LN_<name>_p<n>.gcode`).
Manual prints have no queue entry → no dialog. `LN_MENU` with no queue entry shows just
the next-job/empty dialog.

### Architecture & layering (user requirement)

User: Moonraker/Klipper-specific LayerNexus code runs **only in `moonraker_worker`**, and
the design must be abstract enough that adding e.g. a **Bambu Lab** printer later does not
require restructuring. This extends the existing `PrinterBackend` protocol idea
(`core/services/printer_backend.py`, which already anticipates Bambu Lab). Three layers:

1. **Printer-agnostic queue actions** (shared by web views and every worker; no
   Moonraker/Klipper knowledge). Pass/Fail and Run-Next logic currently lives inside the
   views (`QueueEntryReviewView.post`, `RunNextQueueView.post` in `core/views/queue.py`).
   Extract it into a service (e.g. `core/services/queue.py`:
   `review_queue_entry(entry, passed) -> ReviewResult`,
   `start_next_for_printer(printer, expected_entry=None) -> StartResult`). Web behaviour
   and messages must stay unchanged (existing tests keep passing). Starting still goes
   through `get_printer_backend()`, so it is already backend-neutral.
2. **Printer-agnostic operator dialog model** (new, e.g. `core/services/operator_dialog.py`):
   plain dataclasses describing *what* to ask the operator — title, text lines, and
   buttons that carry an abstract action (`Review(entry_pk, passed)`,
   `StartNext(entry_pk)`, `Dismiss`, `ShowMenu`). Functions build dialogs from DB state
   (`review_dialog(entry)`, `next_job_dialog(printer)` incl. filament, `menu_dialog(printer)`)
   and handle an abstract action → returns the follow-up dialog. Includes all validation
   (entry belongs to printer, expected state, "Already handled"). No G-code, no
   Moonraker, no string escaping here. Fully unit-testable without any printer.
3. **Moonraker/Klipper adapter** (only imported by `moonraker_worker`; e.g.
   `core/services/moonraker_operator_ui.py`): renders a dialog into `action:prompt_*`
   `RESPOND` lines (incl. sanitising), sends them over the worker's connection, and parses
   `LN_*` macro echoes from `notify_gcode_response` back into abstract actions. Nothing
   outside the worker imports it; the web process never talks prompt/G-code.

A future Bambu Lab worker would implement only its own layer-3 adapter (render the same
dialog on whatever channel it has, or not at all); layers 1–2 stay untouched. The
per-printer opt-in flag is therefore named generically (e.g.
`operator_prompts_enabled`, "Show operator dialogs on the printer"), not
KlipperScreen-specific.

### Robustness / safety rules

- Every macro carries the queue entry pk. The worker re-validates before acting: entry
  exists, belongs to **this** printer (the one whose WebSocket delivered the response),
  and is in the expected state (`awaiting_review` for review; `waiting` + printer free for
  start). Stale/double presses → a "Already handled" prompt, no DB change.
- Use the existing IMMEDIATE-transaction setup (#75) — the worker and web can race on the
  same entry; the state check must be inside the transaction.
- Text sent through `RESPOND MSG="..."` must be sanitised (job/project names may contain
  quotes, `|`, newlines, non-ASCII). `|` is the button field separator in the prompt
  protocol.
- Authorization: anyone who can send G-code to the printer (touchscreen, Mainsail) can
  review/start. This is accepted — physical/printer access equals operator access. There
  is no LayerNexus user on these actions; log them as coming from the printer (logger +
  whatever audit the web path has — currently none).
- If the worker is down, the dialogs simply don't appear; the web UI stays the fallback.
- **Opt-in per printer** (my call): new `PrinterProfile` boolean, e.g.
  `operator_prompts_enabled` (default `False`, editable in the printer form, migration via
  `makemigrations`). The worker only sends prompts to, and only acts on `LN_*` responses
  from, printers with the flag set — printers without `[respond]`/macros never get
  unknown-command spam.
- **Start must not hold the SQLite write lock during the G-code upload** (my call). Do the
  state re-validation (entry still `waiting`, still next, printer free) in a short
  transaction, then upload/start outside it — same as web Run Next today, which has no
  transaction. Guard against double presses with a per-printer `asyncio.Lock` in the
  worker (single worker process). No new queue status, no change to web Run Next
  semantics.
- **Changes made in the web UI** (my call, kept simple): if an entry is reviewed on the
  web while its printer prompt is open, the worker closes the stale prompt best-effort
  (e.g. on its reconcile tick) — otherwise pressing a button just yields
  "Already handled". A cancel from the web does **not** raise a dialog on the printer
  (the operator is at the PC then); `LN_MENU` shows it if wanted.
- **Klipper error/shutdown endings** (my call): no automatic re-send after a Klipper
  restart; the `LN_MENU` entry is the recovery path.

### Filament shown in the next-job dialog

A plate has no direct part list; the job does (`PrintJob.job_parts → Part.material /
Part.color`). Show the distinct material+colour combinations of the job's parts (usually
exactly one). If none are set: "Filament: not specified". (Builder may use a better source
if one exists, e.g. a filament profile on the job.)

### Printer-side files (user decision: option A — manual install)

Two ready-made files ship **in the LayerNexus repo** (e.g. `klipper/`):
- `klipper/layernexus.cfg` — `[respond]` plus the thin echo macros `LN_REVIEW`,
  `LN_NEXT_JOB`, `LN_MENU` (no logic; LayerNexus does the work). Must not clash if the
  user already has `[respond]` — document that, or leave `[respond]` as a documented
  requirement instead of putting it in the file (builder's call, but a duplicate section
  must not break Klipper startup).
- `klipper/layernexus_klipperscreen.conf` — the `[menu __main layernexus]` entry
  (`method: printer.gcode.script`, script `LN_MENU`).

The user uploads both to `~/printer_data/config/` (e.g. via Mainsail/Fluidd) and adds once:
`[include layernexus.cfg]` to `printer.cfg` and `[include layernexus_klipperscreen.conf]`
to `KlipperScreen.conf` (both support `[include]`), then `FIRMWARE_RESTART` and a
KlipperScreen restart. LayerNexus never writes printer config files. Automatic
installation via Moonraker's file API is a possible later follow-up, not part of this.

New doc page `docs/integrations/klipperscreen.md` describes these steps, the opt-in flag,
and notes the same dialogs appear in Mainsail. If the macros change in a later
LayerNexus version, the release notes must say so (files must be re-uploaded).

## Constraints

- Follow CLAUDE.md (style, type hints, tests with `unittest.mock`, never hit real
  Moonraker in tests, coverage gate, ruff).
- No new runtime dependency; reuse the existing WebSocket client. Sending G-code may go
  over the same WebSocket (JSON-RPC `printer.gcode.script`) or the existing REST client —
  builder's call, but it must not block the worker's event loop.
- `notify_gcode_response` is broadcast by Moonraker to all WS clients; the worker
  currently ignores it — it must start handling it without disturbing status sync.

## Left to the builder

- Exact macro names/parameters and the exact echo-line format the worker parses.
- Prompt wording/layout and button colours (keep it short, touch-friendly, English like
  the rest of the UI).
- Whether to re-show the review dialog automatically after a worker restart/reconnect
  (the `LN_MENU` entry covers recovery either way).
- Service function names and result types.

## Calls I made (not explicitly decided by the user)

- English UI text (rest of LayerNexus is English).
- "Later" just closes the dialog; no reminder.
- No spool-match check, display only.
- Combined "result + next job" prompt after review instead of two separate dialogs.
- Opt-in flag per printer (default off).
- Start: short validation transaction + per-printer lock, upload outside the DB lock.
- Web review closes a stale printer prompt best-effort; web cancel raises no printer dialog.
- After Klipper error/shutdown, recovery only via the "LayerNexus" menu entry.
