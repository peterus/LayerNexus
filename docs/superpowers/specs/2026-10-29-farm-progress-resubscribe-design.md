# Farm progress stuck at 0 % — re-subscribe after Klippy restarts

Date: 2026-10-29 · Status: draft (PM topic 33)

## Intent

`/farm/` showed 0 % for a running print while the printer was at 84 %. The farm overview
is the core "Moonraker tracking" view; if it lies, it is worthless. The fix must make
progress reliable for **every** way a LayerNexus print is started, without introducing a
new copy of printer state (the larger "derive status instead of storing it" refactor is
PM topic 32 and is out of scope here).

## Root cause (verified live on prod, 2026-10-29, read-only)

- The worker's WebSocket client (`core/services/moonraker_ws.py`) sends
  `printer.objects.subscribe` (print_stats, virtual_sdcard, …) **once per connection**,
  right after connecting.
- Moonraker drops a client's object subscriptions whenever **Klippy** disconnects /
  restarts, while the WebSocket itself stays open. Clients are expected to re-subscribe
  after `notify_klippy_ready`. The worker never does.
- On 2026-10-29 Klippy restarted five times between 15:34 and 16:21 (a `klippy_shutdown`
  at the start of plate p6, then firmware restarts). The worker WS stayed open the whole
  time (opened 04:20 at deploy, no close in `moonraker.log`). From then on only broadcast
  notifications reached the worker: `notify_history_changed` (print finished) and
  `notify_gcode_response` (KlipperScreen buttons). So completion and operator dialogs
  kept working, but `notify_status_update` never arrived and `PrintQueue.progress` was
  never written.
- Evidence: queue entry 28 (plate p5, started 18:36 via KlipperScreen "start next")
  had `status=printing`, `progress=None`, `status_updated_at=None` while Moonraker
  reported `virtual_sdcard.progress=0.897`. Entry 26 (started the day before, before the
  Klippy restarts) had progress written. All worker threads were idle (no hang).
- Confirmation: restarting only the worker (20:49, user-approved) re-subscribed and
  wrote `progress=0.977` within a second; at print end entry 28 moved to
  `awaiting_review` correctly (20:52). Plate/job stay `printing` until review — by design.
- The KlipperScreen dialog (#81) is **not** the cause; it was the start path that
  happened to be used after the Klippy restarts. Every start path is affected equally.
- `/farm/` (`FarmDashboardView`) and the dashboard read `PrintQueue.progress`; they are
  correct and need no change.

Secondary defect found while reading the start path: `mark_queue_entry_started`
(`core/services/queue.py`) does not reset `progress` / `status_updated_at`. A re-queued
entry (retry after cancel, e.g. entry 26 with 17.9 %) starts showing its stale progress.

## Goals

1. **Re-subscribe after Klippy restarts.** When the WS client receives
   `notify_klippy_ready`, it re-sends the subscribe request on the same connection.
2. **Use the subscribe response — for progress only.** Moonraker's reply to
   `printer.objects.subscribe` contains the current status of all subscribed objects
   (`result.status`). Today the client discards it. Forward it to the event handler as a
   synthetic `notify_status_update` (same params shape: `[status, eventtime]`) **with
   `print_stats.state` removed**, so progress is written immediately after every
   (re)subscribe, but the snapshot can never end a print. *(My call, from the builder
   check.)* Reason: a full snapshot always carries `print_stats.state`; right after
   `notify_klippy_ready` that is typically `standby`/`error`, and `standby`/`complete`/
   `cancelled`/`error` are terminal in `printer_status_sync`. Passing it through would
   send printing entries to review on every re-subscribe — a change to completion
   behaviour, which is a non-goal. Completion keeps coming from live
   `notify_status_update` state changes and the `notify_history_changed` broadcast,
   exactly as today.
3. **Watchdog safety net (user decision: yes).** If the client has not received a single
   status message for 60 s on an open connection **while Klippy is believed ready**, it
   re-subscribes and logs a WARNING. If the next 60 s are silent again, it closes the
   connection so the existing reconnect-with-backoff loop takes over. Rationale:
   subscribed objects include `extruder` / `heater_bed` temperatures, which change
   continuously whenever Klippy is ready, so 60 s of silence reliably means
   "subscription lost", printing or not. *(My calls, from the builder check:)*
   - "Status message" = a `notify_status_update` **or** a successful subscribe response;
     both reset the timer.
   - The client tracks Klippy readiness from notifications: `notify_klippy_shutdown` /
     `notify_klippy_disconnected` → not ready (watchdog paused, no warnings);
     `notify_klippy_ready` → ready (re-subscribe, timer restarts). It also subscribes
     `webhooks.state`: the subscribe response (and live status updates carrying it) set
     ready only for `"ready"`, so a connection made while Klippy is already in
     startup/shutdown/error starts paused (state predates the connection, so no shutdown
     notification arrives). Missing `webhooks.state` → ready. A rejected subscribe (Klippy
     host not connected) → not ready, and the watchdog quietly retries the subscribe once
     per window without reconnecting. So a printer left in shutdown overnight produces
     no re-subscribe/close loop and no log spam. *(Refined during review of PR #93.)*
4. **Reset progress on start.** `mark_queue_entry_started` sets `progress=None` and
   `status_updated_at=None` together with `status=printing`.
5. **Regression test per start path** (mocked Moonraker, never the real API): every path
   ends in `mark_queue_entry_started`; for each path, start an entry, then feed a
   `notify_status_update` with `virtual_sdcard.progress` through the worker's event
   handler (`_handle_event_for_printer`) and assert `PrintQueue.progress` is written;
   then feed `notify_history_changed` finished and assert `awaiting_review`. Paths:
   - web "Run Next" / queue start (`core/views/queue.py`, both call sites)
   - scheduled start (`core/services/scheduled_start.py`)
   - KlipperScreen "start next" (`core/services/operator_dialog.py`)
   - remaining plates of a printing job (#78) — through whichever of the above it uses
   - a re-queued entry with stale progress (goal 4)
6. **WS-level tests** in `core/tests/test_services_moonraker_ws.py` with a fake websocket:
   `notify_klippy_ready` → second subscribe sent; subscribe response → handler receives a
   synthetic `notify_status_update` **without `print_stats.state`** (and a printing entry
   fed that snapshot with state `standby` stays `printing`); watchdog → re-subscribe after
   timeout, close after a second silent window, timer reset by any status update or
   subscribe response, paused after `notify_klippy_shutdown` / `notify_klippy_disconnected`
   and resumed by `notify_klippy_ready`. The watchdog interval is a module constant so
   tests can shrink it.

## Non-goals

- Showing progress for prints **not** started via LayerNexus (direct at the printer /
  Fluidd). They have no queue entry; showing them needs live printer state, which is
  topic 32. (User decision.)
- New features in the KlipperScreen dialog.
- The status refactor (topic 32); no new stored status fields.
- Changing how completion / review works (verified working).

## Anti-goals

- Re-subscribing in a tight loop or flooding Moonraker (at most one re-subscribe per
  watchdog window, plus one per `notify_klippy_ready`).
- Any change that lets a handler exception or a malformed subscribe response kill the
  printer's event pump.

## Constraints

- Project conventions (CLAUDE.md): type hints, Google docstrings, double quotes,
  f-strings in new code (existing logger calls use %-style args — keep that idiom for
  logging), `logging.getLogger(__name__)`.
- No migration expected.
- CI gate as usual (ruff, makemigrations --check, tests, coverage ≥ 55).

## Left to the builder

- Exact structure of the watchdog (e.g. `asyncio.wait_for` around `ws.recv()` vs. a
  separate task); keep it inside `MoonrakerWebSocketClient`.
- How the subscribe response is matched (request `id`) — keep it simple.
- Log wording.

## Rollout

PR → green CI → review loop to 0 findings → merge → prod redeploy after user approval
(`ops/prod-deploy`). After deploy, verify on prod: worker log shows subscribe;
a `FIRMWARE_RESTART` is **not** to be triggered on prod for testing — verification of the
klippy_ready path relies on tests unless a natural Klippy restart happens.
