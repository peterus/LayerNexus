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

    Records sent frames and yields the given raw frames when iterated,
    mirroring ``async for raw in ws`` in the client.
    """

    def __init__(self, frames: Iterable[str], client: MoonrakerWebSocketClient | None = None) -> None:
        self.frames = list(frames)
        self.sent: list[str] = []
        self.client = client
        self.delay_during_iteration: list[float] = []

    async def send(self, message: str) -> None:
        self.sent.append(message)

    async def __aiter__(self):
        for frame in self.frames:
            if self.client is not None:
                self.delay_during_iteration.append(self.client._reconnect_delay)
            yield frame


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

    async def test_connect_appends_token_when_api_key(self) -> None:
        connect = await self._listen(FakeWebSocket([]), token="abc")

        url = connect.call_args.args[0]
        self.assertEqual(url, "ws://printer.local:7125/websocket?token=abc")
        self.assertEqual(connect.call_args.kwargs, {"ping_interval": 20, "ping_timeout": 20})

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
