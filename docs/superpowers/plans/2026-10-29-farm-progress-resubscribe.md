# Farm Progress Re-Subscribe Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Printing queue entries get live progress again after Klippy restarts. The worker's Moonraker WebSocket re-subscribes, uses the subscribe snapshot for progress, and a watchdog recovers silently lost subscriptions.

**Architecture:** All changes are in `MoonrakerWebSocketClient` (`core/services/moonraker_ws.py`) plus a one-line reset in `mark_queue_entry_started` (`core/services/queue.py`). Event interpretation stays in `printer_status_sync`. No model, migration or view changes.

**Tech Stack:** Django 6.1, `websockets` 17 (asyncio client), asgiref `sync_to_async`, Django test runner (`IsolatedAsyncioTestCase`, `TestCase`).

**Spec:** `docs/superpowers/specs/2026-10-29-farm-progress-resubscribe-design.md`

## Global Constraints

- Never hit a real Moonraker in tests. Mock `websockets.connect` via `fake_connect`/`FakeWebSocket` and the printer backend via `core.services.queue.get_printer_backend`.
- CLAUDE.md style: double quotes, type hints, Google docstrings. Logger calls keep the existing `%s` argument idiom.
- Watchdog interval is 60 s, as module constant `STATUS_WATCHDOG_INTERVAL = 60.0`.
- The subscribe snapshot must never carry `print_stats.state` to the handler.
- Tests run with `DEBUG=1 uv run python manage.py test core`. Run `collectstatic --noinput` once in a fresh worktree.
- CI gates: `uv run ruff check . && uv run ruff format --check .`, `makemigrations --check --dry-run` (no migration expected), coverage ≥ 55.

## Review Focus

1. A subscribe **error** response (Klippy not ready: `{"id": 1, "error": {...}}`) is neither forwarded nor fatal. Test in Task 2.
2. `notify_klippy_ready` arriving **mid-print** updates progress and leaves the entry `printing`, even though the snapshot's state would read `standby`. Test in Task 4 (`test_resubscribe_snapshot_does_not_end_print`).
3. A stream of `notify_gcode_response` / other frames does **not** reset the watchdog. Only status messages count. Test in Task 3.
4. A watchdog close returns cleanly, so `run()` reconnects with backoff. Test in Task 3.
5. An empty subscribe result (`status: {}`, or only `print_stats.state`) forwards nothing. This keeps the existing `test_status_update_forwarded_response_ignored` valid. Test in Task 2.

---

### Task 1: Reset progress when a queue entry starts

**Files:**
- Modify: `core/services/queue.py` (`mark_queue_entry_started`, ~line 484)
- Test: `core/tests/test_services_queue.py`

**Interfaces:**
- Produces: after `mark_queue_entry_started(entry, remote_filename)`, `entry.progress is None` and `entry.status_updated_at is None`, both persisted.

- [ ] **Step 1: Write failing test** `test_mark_started_resets_stale_progress`. Create an entry with `status=waiting`, `progress=0.18`, `status_updated_at=<now - 1 day>`. Call `mark_queue_entry_started(entry, "LN_x.gcode")`, then `refresh_from_db()`. Assert `progress is None`, `status_updated_at is None`, `status == PrintQueue.STATUS_PRINTING`.
- [ ] **Step 2: Run, expect FAIL:** `DEBUG=1 uv run python manage.py test core.tests.test_services_queue -k stale_progress`
- [ ] **Step 3: Implement.** Set `entry.progress = None` and `entry.status_updated_at = None`, and add both to `update_fields`.
- [ ] **Step 4: Run, expect PASS** (same command, then the whole `test_services_queue` module).
- [ ] **Step 5: Commit** `fix(queue): reset progress when a queue entry starts`

### Task 2: Forward subscribe snapshot (progress only) and re-subscribe on klippy_ready

**Files:**
- Modify: `core/services/moonraker_ws.py`
- Test: `core/tests/test_services_moonraker_ws.py`

**Interfaces:**
- Produces:
  - `MoonrakerWebSocketClient._klippy_ready: bool`, set to `True` on every (re)connect.
  - `MoonrakerWebSocketClient._handle_frame(ws: Any, event: dict[str, Any]) -> bool` handles one decoded frame. It returns `True` if the frame counts as a status message for the watchdog (a `notify_status_update`, or a successful subscribe response). Task 3 consumes it.
  - Module function `snapshot_event(result: dict[str, Any]) -> dict[str, Any] | None`. From a subscribe `result` it builds `{"jsonrpc": "2.0", "method": "notify_status_update", "params": [status_without_print_stats_state, eventtime]}`. It drops `print_stats.state`, drops `print_stats` entirely if that leaves it empty, and returns `None` if nothing remains.
- Behaviour:
  - A frame with `id == 1` (our subscribe id; keep it as constant `SUBSCRIBE_REQUEST_ID = 1`) and `result` → `snapshot_event`, forwarded via `on_event` if it is not `None`.
  - A frame with `id == 1` and `error` → log WARNING `"%s: subscribe failed: %s"` and forward nothing.
  - `notify_klippy_ready` → `_klippy_ready = True`, log INFO `"%s: klippy ready, re-subscribing"`, send the subscribe again, and still forward the event to `on_event`.
  - `notify_klippy_shutdown` / `notify_klippy_disconnected` → `_klippy_ready = False`, and the event is forwarded.
  - Handler exceptions stay caught and logged, as today.

- [ ] **Step 1: Write failing tests**, using the existing `FakeWebSocket` iteration:
  - `test_klippy_ready_resubscribes`: frames `[notify_klippy_ready]` → `len(ws.sent) == 2`, and both are `printer.objects.subscribe`.
  - `test_subscribe_response_forwarded_without_state`: response `{"id": 1, "result": {"eventtime": 5.0, "status": {"print_stats": {"state": "standby", "filename": "a.gcode"}, "virtual_sdcard": {"progress": 0.42}}}}` → `self.events == [{"jsonrpc": "2.0", "method": "notify_status_update", "params": [{"print_stats": {"filename": "a.gcode"}, "virtual_sdcard": {"progress": 0.42}}, 5.0]}]`.
  - `test_subscribe_response_only_state_forwards_nothing`: status `{"print_stats": {"state": "printing"}}` → `self.events == []`.
  - `test_subscribe_error_logged_not_forwarded`: `{"id": 1, "error": {"code": 503, "message": "Klippy Host not connected"}}` → no events, and a WARNING containing `"subscribe failed"`.
  - `test_klippy_shutdown_marks_not_ready`: frames `[notify_klippy_shutdown]` → `client._klippy_ready is False`, and the event is forwarded.
  - The existing `test_status_update_forwarded_response_ignored` must stay green unchanged (empty `status` → `None`).
- [ ] **Step 2: Run, expect FAIL:** `DEBUG=1 uv run python manage.py test core.tests.test_services_moonraker_ws`
- [ ] **Step 3: Implement.** Extract `_subscribe` so that it is reused for re-subscribes. Move the per-frame logic from `_connect_and_listen` into `_handle_frame`. Update the module docstring with one line on re-subscribe.
- [ ] **Step 4: Run, expect PASS** (same module).
- [ ] **Step 5: Commit** `fix(worker): re-subscribe after klippy restart and use subscribe snapshot`

### Task 3: Status watchdog

**Files:**
- Modify: `core/services/moonraker_ws.py` (`_connect_and_listen`)
- Modify (test helper): `FakeWebSocket` in `core/tests/test_services_moonraker_ws.py`
- Test: `core/tests/test_services_moonraker_ws.py`

**Interfaces:**
- Consumes: `_handle_frame`, `_klippy_ready`, `_subscribe` (Task 2).
- Produces: constant `STATUS_WATCHDOG_INTERVAL = 60.0`.

**Listen loop (decided):** replace `async for raw in ws` with `raw = await asyncio.wait_for(ws.recv(), timeout=remaining)`.
- `remaining` is the time until `last_status + STATUS_WATCHDOG_INTERVAL`, measured with `loop.time()`.
- `websockets.exceptions.ConnectionClosedOK` from `recv()` → return normally. This mirrors `async for`, which ends on a clean close.
- When `_handle_frame` returns `True` → reset `last_status` and clear the `resubscribed` flag.
- On `TimeoutError`:
  - If not `_klippy_ready` → just restart the window.
  - Elif not `resubscribed` → WARNING `"%s: no status for %.0fs, re-subscribing"`, call `_subscribe`, set the flag, restart the window.
  - Else → WARNING `"%s: still no status after re-subscribe, reconnecting"`, `await ws.close()`, return.
- `notify_klippy_ready` also restarts the window.

- [ ] **Step 1: Extend `FakeWebSocket`.**
  - Add `async def recv(self) -> str`. It pops the next frame; when the frames run out, it raises `close_error` if one is set. With `hang=True` it awaits forever (`await asyncio.Event().wait()`). Otherwise it raises `ConnectionClosedOK(None, None)`.
  - Add `async def close(self) -> None`, which sets `self.closed = True`.
  - Keep `__aiter__` working, or drop it if nothing uses it any more. The existing `delay_during_iteration` tracking moves into `recv`.
- [ ] **Step 2: Write failing tests.** Each patches `core.services.moonraker_ws.STATUS_WATCHDOG_INTERVAL` to `0.05` and bounds the run with `asyncio.wait_for(..., 2)`.
  - `test_watchdog_resubscribes_then_closes`: `FakeWebSocket([], hang=True)` → `_connect_and_listen` returns. `len(ws.sent) == 2` (initial plus one re-subscribe), `ws.closed is True`, and two WARNINGs are logged.
  - `test_watchdog_paused_while_klippy_down`: frames `[notify_klippy_shutdown]`, `hang=True`. Run for about 0.3 s, then cancel. `len(ws.sent) == 1` and `ws.closed` is falsy.
  - `test_gcode_responses_do_not_feed_watchdog`: frames are many `notify_gcode_response`s, then `hang=True` → the watchdog still re-subscribes and closes (`len(ws.sent) == 2`).
  - `test_status_update_resets_watchdog`: a fake whose `recv` returns a `notify_status_update` every 0.02 s for 0.3 s and then ends cleanly. Assert `len(ws.sent) == 1` and no close.
  - `test_watchdog_close_triggers_reconnect`: `run()` with connect side effects `[hanging ws, then CancelledError]` → `connect` is called twice. Reuse the pattern from `_run_with_listen_effects`.
- [ ] **Step 3: Run, expect FAIL:** `DEBUG=1 uv run python manage.py test core.tests.test_services_moonraker_ws`
- [ ] **Step 4: Implement the loop as decided above.**
- [ ] **Step 5: Run the whole WS module plus `core.tests.test_management_moonraker_worker`, expect PASS.** The worker tests' `FakeWS` in `test_run_printer_builds_ui_only_when_enabled` may need a `recv`; adapt it there if so.
- [ ] **Step 6: Commit** `fix(worker): watchdog re-subscribes when status updates stop`

### Task 4: Regression test per start path

**Files:**
- Create: `core/tests/test_progress_start_paths.py`

**Interfaces:**
- Consumes: `core.management.commands.moonraker_worker._handle_event_for_printer(printer, event, ui=None)`. It is async; call it from sync tests via `asgiref.sync.async_to_sync`.
- Consumes: `snapshot_event` (Task 2), `mark_queue_entry_started` reset (Task 1).

**Shared setup (`TestCase`):**
- An Operator user logged in via `self.client`.
- A printer with `moonraker_url="http://p1:7125"`.
- A `PrintJob` with two plates that have `gcode_file`.
- Patch `core.services.queue.get_printer_backend` (its `start_print` returns OK; mirror `test_gcode_response_start_next_starts_print`).

Helper `_assert_progress_and_completion(entry)`:
1. Feed `{"method": "notify_status_update", "params": [{"virtual_sdcard": {"progress": 0.5}}, 1.0]}` → after refresh, `entry.progress == 0.5`.
2. Feed `{"method": "notify_history_changed", "params": [{"action": "finished", "job": {"status": "completed"}}]}` → `entry.status == PrintQueue.STATUS_AWAITING_REVIEW`.

- [ ] **Step 1: Write the tests.** Each one starts a waiting entry, asserts `status == printing`, then calls the helper.
  - `test_web_run_next`: POST to the `RunNextQueueView` URL (look up the name in `core/urls/queue.py`) for the printer.
  - `test_web_run_all_queues`: POST to the `RunAllQueuesView` URL.
  - `test_scheduled_start`: create a due `ScheduledQueueStart` for the printer, then call `run_due_scheduled_starts()`.
  - `test_klipperscreen_start_next`: call `core.services.operator_dialog.handle_action(printer, StartNext(entry.pk))`.
  - `test_remaining_plate_of_printing_job`: plate 1 is already completed, the job is `printing`, and plate 2's entry is started via Run Next.
  - `test_requeued_entry_with_stale_progress`: the entry starts with `progress=0.18` and an old `status_updated_at`. After Run Next, `progress is None`; then call the helper.
  - `test_resubscribe_snapshot_does_not_end_print`: start via Run Next. Feed `snapshot_event({"eventtime": 2.0, "status": {"print_stats": {"state": "standby"}, "virtual_sdcard": {"progress": 0.7}}})` through the handler → `progress == 0.7` and the status is still `printing`.
- [ ] **Step 2: Run:** `DEBUG=1 uv run python manage.py test core.tests.test_progress_start_paths`. Expect PASS, since Tasks 1–3 are done. If a path fails, that is a real bug: fix it in its owning module, not in the test.
- [ ] **Step 3: Run full gates.** `uv run ruff check . && uv run ruff format --check .`, `DEBUG=1 uv run python manage.py makemigrations --check --dry-run`, `DEBUG=1 uv run coverage run manage.py test core && uv run coverage report`. Expect all green, with coverage ≥ 55.
- [ ] **Step 4: Commit** `test: progress regression per print start path`
