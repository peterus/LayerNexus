"""Tests for the Moonraker WebSocket helpers."""

import asyncio
import json
from collections.abc import Iterable
from typing import Any
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, MagicMock, patch

import requests
from django.test import TestCase
from websockets.exceptions import ConnectionClosedError, ConnectionClosedOK

from core.models import PrinterProfile
from core.services.moonraker_ws import (
    RECONNECT_INITIAL_DELAY,
    RECONNECT_MAX_DELAY,
    MoonrakerWebSocketClient,
    build_ws_url,
    fetch_oneshot_token,
    snapshot_event,
)
from core.tests.mixins import TestDataMixin


class BuildWsUrlTests(TestCase):
    def test_plain_http(self):
        self.assertEqual(
            build_ws_url("http://printer.local:7125"),
            "ws://printer.local:7125/websocket",
        )

    def test_https(self):
        self.assertEqual(
            build_ws_url("https://printer.local"),
            "wss://printer.local/websocket",
        )

    def test_subpath(self):
        self.assertEqual(
            build_ws_url("http://host/moonraker/"),
            "ws://host/moonraker/websocket",
        )

    def test_trailing_slash(self):
        self.assertEqual(
            build_ws_url("http://host:7125/"),
            "ws://host:7125/websocket",
        )


class FetchOneshotTokenTests(TestDataMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.printer.moonraker_url = "http://printer:7125"

    def test_no_api_key_returns_none(self):
        self.printer.moonraker_api_key = ""
        self.assertIsNone(fetch_oneshot_token(self.printer))

    @patch("core.services.moonraker_ws.requests.get")
    def test_with_api_key_returns_token(self, mock_get):
        self.printer.moonraker_api_key = "secret"
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"result": "abc123"}
        mock_resp.raise_for_status.return_value = None
        mock_get.return_value = mock_resp

        token = fetch_oneshot_token(self.printer)

        self.assertEqual(token, "abc123")
        mock_get.assert_called_once()
        kwargs = mock_get.call_args.kwargs
        self.assertEqual(kwargs["headers"]["X-Api-Key"], "secret")

    @patch("core.services.moonraker_ws.requests.get")
    def test_request_error_returns_none(self, mock_get):
        self.printer.moonraker_api_key = "secret"
        mock_get.side_effect = requests.ConnectionError("down")

        self.assertIsNone(fetch_oneshot_token(self.printer))

    @patch("core.services.moonraker_ws.requests.get")
    def test_unexpected_payload_returns_none(self, mock_get):
        self.printer.moonraker_api_key = "secret"
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"result": {"nested": "nope"}}
        mock_resp.raise_for_status.return_value = None
        mock_get.return_value = mock_resp

        self.assertIsNone(fetch_oneshot_token(self.printer))


class FakeWebSocket:
    """Minimal stand-in for a websockets client connection.

    Records sent frames and returns the given raw frames from ``recv()``.
    When the frames run out it raises ``close_error`` if set, hangs
    forever with ``hang=True``, or otherwise signals a clean close.
    """

    def __init__(
        self,
        frames: Iterable[str],
        client: MoonrakerWebSocketClient | None = None,
        close_error: Exception | None = None,
        hang: bool = False,
    ) -> None:
        self.frames = list(frames)
        self.sent: list[str] = []
        self.client = client
        self.close_error = close_error
        self.hang = hang
        self.closed = False
        self.delay_during_iteration: list[float] = []

    async def send(self, message: str) -> None:
        self.sent.append(message)

    async def recv(self) -> str:
        if self.frames:
            if self.client is not None:
                self.delay_during_iteration.append(self.client._reconnect_delay)
            return self.frames.pop(0)
        if self.close_error is not None:
            raise self.close_error
        if self.hang:
            await asyncio.Event().wait()
        raise ConnectionClosedOK(None, None)

    async def close(self) -> None:
        self.closed = True


def fake_connect(ws: FakeWebSocket) -> MagicMock:
    """Build a ``websockets.connect`` mock usable as an async context manager."""
    context = MagicMock()
    context.__aenter__ = AsyncMock(return_value=ws)
    context.__aexit__ = AsyncMock(return_value=False)
    return MagicMock(return_value=context)


class MoonrakerWebSocketClientTests(IsolatedAsyncioTestCase):
    """Characterization tests for :class:`MoonrakerWebSocketClient`.

    They pin the behaviour the worker relies on so websockets upgrades
    (14 -> 17) can be verified without a real printer.
    """

    def setUp(self) -> None:
        self.printer = PrinterProfile(name="ws-test", moonraker_url="http://printer.local:7125")
        self.events: list[dict[str, Any]] = []

        async def on_event(event: dict[str, Any]) -> None:
            self.events.append(event)

        self.client = MoonrakerWebSocketClient(self.printer, on_event)

    async def _listen(self, ws: FakeWebSocket, token: str | None = None) -> MagicMock:
        connect = fake_connect(ws)
        with (
            patch("core.services.moonraker_ws.fetch_oneshot_token", return_value=token),
            patch("core.services.moonraker_ws.websockets.connect", connect),
        ):
            await self.client._connect_and_listen()
        return connect

    async def test_connect_uses_keepalive_params(self) -> None:
        connect = await self._listen(FakeWebSocket([]))

        connect.assert_called_once_with(
            "ws://printer.local:7125/websocket",
            ping_interval=20,
            ping_timeout=20,
        )

    async def test_connect_appends_token_when_available(self) -> None:
        connect = await self._listen(FakeWebSocket([]), token="abc")

        url = connect.call_args.args[0]
        self.assertEqual(url, "ws://printer.local:7125/websocket?token=abc")
        self.assertEqual(connect.call_args.kwargs, {"ping_interval": 20, "ping_timeout": 20})

    @patch("core.services.moonraker_ws.requests.get")
    async def test_api_key_fetches_oneshot_token_for_url(self, mock_get: MagicMock) -> None:
        self.printer.moonraker_api_key = "secret"
        mock_get.return_value.json.return_value = {"result": "abc"}
        connect = fake_connect(FakeWebSocket([]))

        with patch("core.services.moonraker_ws.websockets.connect", connect):
            await self.client._connect_and_listen()

        self.assertEqual(mock_get.call_args.args[0], "http://printer.local:7125/access/oneshot_token")
        self.assertEqual(mock_get.call_args.kwargs["headers"], {"X-Api-Key": "secret"})
        self.assertEqual(connect.call_args.args[0], "ws://printer.local:7125/websocket?token=abc")

    async def test_abnormal_close_during_iteration_propagates(self) -> None:
        notify = {"method": "notify_status_update", "params": [{}, 1.0]}
        ws = FakeWebSocket([json.dumps(notify)], close_error=ConnectionClosedError(None, None))

        with self.assertRaises(ConnectionClosedError):
            await self._listen(ws)

        self.assertEqual(self.events, [notify])

    async def test_subscribe_request_sent(self) -> None:
        ws = FakeWebSocket([])
        await self._listen(ws)

        self.assertEqual(len(ws.sent), 1)
        request = json.loads(ws.sent[0])
        self.assertEqual(request["jsonrpc"], "2.0")
        self.assertEqual(request["method"], "printer.objects.subscribe")
        self.assertEqual(request["id"], 1)
        self.assertIn("print_stats", request["params"]["objects"])

    async def test_status_update_forwarded_response_ignored(self) -> None:
        notify = {
            "jsonrpc": "2.0",
            "method": "notify_status_update",
            "params": [{"print_stats": {"state": "standby"}}, 12.5],
        }
        frames = [
            json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"status": {}, "eventtime": 1.0}}),
            "not json",
            json.dumps(notify),
        ]

        with self.assertLogs("core.services.moonraker_ws", level="WARNING") as logs:
            await self._listen(FakeWebSocket(frames))

        self.assertEqual(self.events, [notify])
        self.assertTrue(any("non-JSON frame" in line for line in logs.output))

    async def test_handler_exception_does_not_stop_listening(self) -> None:
        received: list[str] = []

        async def failing_handler(event: dict[str, Any]) -> None:
            received.append(event["method"])
            if len(received) == 1:
                raise RuntimeError("boom")

        self.client.on_event = failing_handler
        frames = [json.dumps({"method": "notify_status_update"}), json.dumps({"method": "notify_gcode_response"})]

        with self.assertLogs("core.services.moonraker_ws", level="ERROR"):
            await self._listen(FakeWebSocket(frames))

        self.assertEqual(received, ["notify_status_update", "notify_gcode_response"])

    async def test_backoff_reset_after_subscribe(self) -> None:
        self.client._reconnect_delay = RECONNECT_MAX_DELAY
        ws = FakeWebSocket([json.dumps({"method": "notify_status_update"})], client=self.client)

        await self._listen(ws)

        self.assertEqual(ws.delay_during_iteration, [RECONNECT_INITIAL_DELAY])
        self.assertEqual(self.client._reconnect_delay, RECONNECT_INITIAL_DELAY)

    async def _run_with_listen_effects(self, effects: list[Any]) -> AsyncMock:
        sleep = AsyncMock()
        with (
            patch.object(self.client, "_connect_and_listen", AsyncMock(side_effect=effects)),
            patch("core.services.moonraker_ws.asyncio.sleep", sleep),
            self.assertLogs("core.services.moonraker_ws", level="INFO"),
            self.assertRaises(asyncio.CancelledError),
        ):
            await self.client.run()
        return sleep

    async def test_run_reconnects_after_connection_closed(self) -> None:
        sleep = await self._run_with_listen_effects([ConnectionClosedError(None, None), asyncio.CancelledError()])

        sleep.assert_awaited_once_with(RECONNECT_INITIAL_DELAY)

    async def test_run_reconnects_after_clean_close(self) -> None:
        sleep = await self._run_with_listen_effects(
            [ConnectionClosedOK(None, None), None, asyncio.CancelledError()],
        )

        self.assertEqual(
            [call.args[0] for call in sleep.await_args_list],
            [RECONNECT_INITIAL_DELAY, RECONNECT_INITIAL_DELAY * 2],
        )

    async def test_run_survives_os_error_and_unexpected_exception(self) -> None:
        sleep = await self._run_with_listen_effects(
            [OSError("refused"), ValueError("bug"), asyncio.CancelledError()],
        )

        self.assertEqual(sleep.await_count, 2)

    async def test_run_backoff_capped_at_max_delay(self) -> None:
        effects: list[Any] = [OSError("down")] * 8 + [asyncio.CancelledError()]
        sleep = await self._run_with_listen_effects(effects)

        delays = [call.args[0] for call in sleep.await_args_list]
        self.assertEqual(
            delays[:3], [RECONNECT_INITIAL_DELAY, RECONNECT_INITIAL_DELAY * 2, RECONNECT_INITIAL_DELAY * 4]
        )
        self.assertEqual(max(delays), RECONNECT_MAX_DELAY)
        self.assertEqual(delays[-1], RECONNECT_MAX_DELAY)


def _frame(method: str, params: list[Any] | None = None) -> str:
    """Encode a JSON-RPC notification frame."""
    return json.dumps({"jsonrpc": "2.0", "method": method, "params": params or []})


def _subscribe_response(status: dict[str, Any], eventtime: float = 5.0) -> str:
    """Encode a successful subscribe response frame."""
    return json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"eventtime": eventtime, "status": status}})


def _subscribe_count(ws: FakeWebSocket) -> int:
    """Count the subscribe requests ``ws`` has been sent."""
    return sum(1 for message in ws.sent if json.loads(message)["method"] == "printer.objects.subscribe")


class SnapshotEventTests(TestCase):
    """Tests for :func:`snapshot_event`."""

    def test_drops_print_stats_state_keeps_rest(self) -> None:
        result = {
            "eventtime": 5.0,
            "status": {"print_stats": {"state": "standby", "filename": "a.gcode"}, "virtual_sdcard": {"progress": 0.4}},
        }

        self.assertEqual(
            snapshot_event(result),
            {
                "jsonrpc": "2.0",
                "method": "notify_status_update",
                "params": [{"print_stats": {"filename": "a.gcode"}, "virtual_sdcard": {"progress": 0.4}}, 5.0],
            },
        )

    def test_does_not_mutate_input(self) -> None:
        result = {"eventtime": 1.0, "status": {"print_stats": {"state": "printing", "filename": "a"}}}

        snapshot_event(result)

        self.assertEqual(result["status"]["print_stats"]["state"], "printing")

    def test_nothing_left_returns_none(self) -> None:
        self.assertIsNone(snapshot_event({"eventtime": 1.0, "status": {}}))
        self.assertIsNone(snapshot_event({"eventtime": 1.0, "status": {"print_stats": {"state": "printing"}}}))

    def test_malformed_status_returns_none(self) -> None:
        self.assertIsNone(snapshot_event({}))
        self.assertIsNone(snapshot_event({"status": ["print_stats"]}))


class ResubscribeTests(IsolatedAsyncioTestCase):
    """Re-subscribe after Klippy restarts and forwarding of the subscribe snapshot."""

    def setUp(self) -> None:
        self.printer = PrinterProfile(name="ws-test", moonraker_url="http://printer.local:7125")
        self.events: list[dict[str, Any]] = []

        async def on_event(event: dict[str, Any]) -> None:
            self.events.append(event)

        self.client = MoonrakerWebSocketClient(self.printer, on_event)

    async def _listen(self, ws: FakeWebSocket) -> None:
        with (
            patch("core.services.moonraker_ws.fetch_oneshot_token", return_value=None),
            patch("core.services.moonraker_ws.websockets.connect", fake_connect(ws)),
        ):
            await asyncio.wait_for(self.client._connect_and_listen(), 2)

    async def test_klippy_ready_resubscribes(self) -> None:
        ws = FakeWebSocket([_frame("notify_klippy_ready")])

        with self.assertLogs("core.services.moonraker_ws", level="INFO") as logs:
            await self._listen(ws)

        self.assertEqual(len(ws.sent), 2)
        self.assertEqual(_subscribe_count(ws), 2)
        self.assertEqual([event["method"] for event in self.events], ["notify_klippy_ready"])
        self.assertTrue(any("klippy ready, re-subscribing" in line for line in logs.output))

    async def test_subscribe_response_forwarded_without_state(self) -> None:
        status = {"print_stats": {"state": "standby", "filename": "a.gcode"}, "virtual_sdcard": {"progress": 0.42}}

        await self._listen(FakeWebSocket([_subscribe_response(status)]))

        self.assertEqual(
            self.events,
            [
                {
                    "jsonrpc": "2.0",
                    "method": "notify_status_update",
                    "params": [{"print_stats": {"filename": "a.gcode"}, "virtual_sdcard": {"progress": 0.42}}, 5.0],
                }
            ],
        )

    async def test_subscribe_response_only_state_forwards_nothing(self) -> None:
        await self._listen(FakeWebSocket([_subscribe_response({"print_stats": {"state": "printing"}})]))

        self.assertEqual(self.events, [])

    async def test_response_to_other_request_ignored(self) -> None:
        frame = json.dumps({"jsonrpc": "2.0", "id": 7, "result": {"status": {"virtual_sdcard": {"progress": 0.1}}}})

        await self._listen(FakeWebSocket([frame]))

        self.assertEqual(self.events, [])

    async def test_subscribe_error_logged_not_forwarded(self) -> None:
        frame = json.dumps({"jsonrpc": "2.0", "id": 1, "error": {"code": 503, "message": "Klippy Host not connected"}})

        with self.assertLogs("core.services.moonraker_ws", level="WARNING") as logs:
            await self._listen(FakeWebSocket([frame]))

        self.assertEqual(self.events, [])
        self.assertTrue(any("subscribe failed" in line for line in logs.output))
        self.assertFalse(self.client._klippy_ready)

    async def test_malformed_frames_do_not_stop_listening(self) -> None:
        notify = {"jsonrpc": "2.0", "method": "notify_status_update", "params": [{}, 1.0]}
        frames = [
            json.dumps({"jsonrpc": "2.0", "id": 1, "result": ["not", "a", "dict"]}),
            "5",
            json.dumps(notify),
        ]

        with self.assertLogs("core.services.moonraker_ws", level="WARNING"):
            await self._listen(FakeWebSocket(frames))

        self.assertEqual(self.events, [notify])

    async def test_snapshot_handler_exception_does_not_stop_listening(self) -> None:
        received: list[str] = []

        async def failing_handler(event: dict[str, Any]) -> None:
            received.append(event["method"])
            if len(received) == 1:
                raise RuntimeError("boom")

        self.client.on_event = failing_handler
        frames = [_subscribe_response({"virtual_sdcard": {"progress": 0.3}}), _frame("notify_gcode_response")]

        with self.assertLogs("core.services.moonraker_ws", level="ERROR"):
            await self._listen(FakeWebSocket(frames))

        self.assertEqual(received, ["notify_status_update", "notify_gcode_response"])

    async def test_klippy_shutdown_marks_not_ready(self) -> None:
        for method in ("notify_klippy_shutdown", "notify_klippy_disconnected"):
            with self.subTest(method=method):
                self.events.clear()

                await self._listen(FakeWebSocket([_frame(method)]))

                self.assertFalse(self.client._klippy_ready)
                self.assertEqual([event["method"] for event in self.events], [method])

    async def test_reconnect_assumes_klippy_ready(self) -> None:
        self.client._klippy_ready = False

        await self._listen(FakeWebSocket([]))

        self.assertTrue(self.client._klippy_ready)


@patch("core.services.moonraker_ws.STATUS_WATCHDOG_INTERVAL", 0.2)
class StatusWatchdogTests(IsolatedAsyncioTestCase):
    """The watchdog re-subscribes, then reconnects, when status updates stop."""

    def setUp(self) -> None:
        self.printer = PrinterProfile(name="ws-test", moonraker_url="http://printer.local:7125")
        self.events: list[dict[str, Any]] = []

        async def on_event(event: dict[str, Any]) -> None:
            self.events.append(event)

        self.client = MoonrakerWebSocketClient(self.printer, on_event)

    async def _listen(self, ws: Any, run_for: float | None = None) -> None:
        """Listen on ``ws``; with ``run_for`` cancel after that many seconds."""
        with (
            patch("core.services.moonraker_ws.fetch_oneshot_token", return_value=None),
            patch("core.services.moonraker_ws.websockets.connect", fake_connect(ws)),
        ):
            task = asyncio.ensure_future(self.client._connect_and_listen())
            if run_for is None:
                await asyncio.wait_for(task, 2)
                return
            done, _ = await asyncio.wait({task}, timeout=run_for)
            self.assertFalse(done, "listen loop ended although it should keep running")
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

    async def test_watchdog_resubscribes_then_closes(self) -> None:
        ws = FakeWebSocket([], hang=True)

        with self.assertLogs("core.services.moonraker_ws", level="WARNING") as logs:
            await self._listen(ws)

        self.assertEqual(_subscribe_count(ws), 2)
        self.assertTrue(ws.closed)
        warnings = [line for line in logs.output if line.startswith("WARNING")]
        self.assertEqual(len(warnings), 2)
        self.assertIn("re-subscribing", warnings[0])
        self.assertIn("reconnecting", warnings[1])

    async def test_watchdog_paused_while_klippy_down(self) -> None:
        ws = FakeWebSocket([_frame("notify_klippy_shutdown")], hang=True)

        with self.assertNoLogs("core.services.moonraker_ws", level="WARNING"):
            await self._listen(ws, run_for=0.7)

        self.assertEqual(_subscribe_count(ws), 1)
        self.assertFalse(ws.closed)

    async def test_watchdog_retries_rejected_subscribe_without_closing(self) -> None:
        error = json.dumps({"jsonrpc": "2.0", "id": 1, "error": {"code": 503, "message": "Klippy Host not connected"}})

        class RejectingWebSocket(FakeWebSocket):
            """Rejects every subscribe, otherwise silent."""

            async def recv(self) -> str:
                while not self.frames:
                    await asyncio.sleep(0.005)
                return self.frames.pop(0)

            async def send(self, message: str) -> None:
                await super().send(message)
                self.frames.append(error)

        ws = RejectingWebSocket([])

        with self.assertLogs("core.services.moonraker_ws", level="WARNING") as logs:
            await self._listen(ws, run_for=0.7)

        # Retried once per window, never reconnects, warns only once per streak.
        self.assertGreater(_subscribe_count(ws), 2)
        self.assertFalse(ws.closed)
        self.assertEqual(len(logs.output), 1)
        self.assertIn("subscribe failed", logs.output[0])

    async def test_accepted_retry_after_rejected_subscribe_rearms_watchdog(self) -> None:
        error = json.dumps({"jsonrpc": "2.0", "id": 1, "error": {"code": 503, "message": "Klippy Host not connected"}})

        class RecoveringWebSocket(FakeWebSocket):
            """Rejects the first subscribe, accepts the second, then stays silent."""

            async def recv(self) -> str:
                while not self.frames:
                    await asyncio.sleep(0.005)
                return self.frames.pop(0)

            async def send(self, message: str) -> None:
                await super().send(message)
                if len(self.sent) == 1:
                    self.frames.append(error)
                elif len(self.sent) == 2:
                    self.frames.append(_subscribe_response({"virtual_sdcard": {"progress": 0.3}}))

        ws = RecoveringWebSocket([])

        with self.assertLogs("core.services.moonraker_ws", level="WARNING") as logs:
            await self._listen(ws)

        # reject -> retry accepted (Klippy ready again) -> silent: re-subscribe, then close.
        self.assertTrue(self.client._klippy_ready)
        self.assertEqual(_subscribe_count(ws), 3)
        self.assertTrue(ws.closed)
        self.assertEqual([event["method"] for event in self.events], ["notify_status_update"])
        self.assertTrue(any("reconnecting" in line for line in logs.output))

    async def test_watchdog_resumed_by_klippy_ready(self) -> None:
        ws = FakeWebSocket([_frame("notify_klippy_shutdown"), _frame("notify_klippy_ready")], hang=True)

        with self.assertLogs("core.services.moonraker_ws", level="INFO"):
            await self._listen(ws)

        # initial + klippy_ready + one watchdog re-subscribe, then close.
        self.assertEqual(_subscribe_count(ws), 3)
        self.assertTrue(ws.closed)

    async def test_gcode_responses_do_not_feed_watchdog(self) -> None:
        ws = FakeWebSocket([_frame("notify_gcode_response", ["ok"])] * 20, hang=True)

        with self.assertLogs("core.services.moonraker_ws", level="WARNING"):
            await self._listen(ws)

        self.assertEqual(_subscribe_count(ws), 2)
        self.assertTrue(ws.closed)

    async def test_status_update_resets_watchdog(self) -> None:
        class TickingWebSocket(FakeWebSocket):
            """Delivers a status update every 10 ms for about 0.3 s, then closes cleanly."""

            async def recv(self) -> str:
                if len(self.frames) >= 30:
                    raise ConnectionClosedOK(None, None)
                await asyncio.sleep(0.01)
                self.frames.append("tick")
                return _frame("notify_status_update", [{"extruder": {"temperature": 210.0}}, 1.0])

        ws = TickingWebSocket([])

        with self.assertNoLogs("core.services.moonraker_ws", level="WARNING"):
            await self._listen(ws)

        self.assertEqual(_subscribe_count(ws), 1)
        self.assertFalse(ws.closed)
        self.assertEqual(len(self.events), 30)

    async def test_subscribe_response_resets_watchdog(self) -> None:
        class AnsweringWebSocket(FakeWebSocket):
            """Answers every subscribe with a successful response, otherwise silent."""

            async def recv(self) -> str:
                while not self.frames:
                    await asyncio.sleep(0.005)
                return self.frames.pop(0)

            async def send(self, message: str) -> None:
                await super().send(message)
                self.frames.append(_subscribe_response({}))

        ws = AnsweringWebSocket([])

        with self.assertLogs("core.services.moonraker_ws", level="WARNING"):
            await self._listen(ws, run_for=0.7)

        # Each silent window re-subscribes once; the answer re-arms it, so it never closes.
        self.assertGreater(_subscribe_count(ws), 2)
        self.assertFalse(ws.closed)

    async def test_watchdog_close_triggers_reconnect(self) -> None:
        ws = FakeWebSocket([], hang=True)
        context = fake_connect(ws).return_value
        connect = MagicMock(side_effect=[context, asyncio.CancelledError()])
        sleep = AsyncMock()

        with (
            patch("core.services.moonraker_ws.fetch_oneshot_token", return_value=None),
            patch("core.services.moonraker_ws.websockets.connect", connect),
            patch("core.services.moonraker_ws.asyncio.sleep", sleep),
            self.assertLogs("core.services.moonraker_ws", level="INFO") as logs,
            self.assertRaises(asyncio.CancelledError),
        ):
            await asyncio.wait_for(self.client.run(), 2)

        self.assertEqual(connect.call_count, 2)
        self.assertTrue(ws.closed)
        sleep.assert_awaited_once_with(RECONNECT_INITIAL_DELAY)
        self.assertTrue(any("websocket closed cleanly" in line for line in logs.output))
