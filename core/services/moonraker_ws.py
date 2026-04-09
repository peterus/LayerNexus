"""Moonraker JSON-RPC WebSocket client for live printer status.

The :class:`MoonrakerWebSocketClient` holds one persistent WebSocket
connection to a Moonraker instance, subscribes to the objects we care
about (``print_stats``, ``virtual_sdcard``, …) and invokes a caller
supplied async callback for every JSON-RPC notification it receives.

The client is responsible for:

* Building the ws(s):// URL from a Moonraker REST URL.
* Optionally obtaining a one-shot token when an API key is configured.
* Reconnecting with exponential backoff on network failures or
  unexpected disconnects.

The client is **not** responsible for interpreting events — that is
the job of :mod:`core.services.printer_status_sync`.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import urlparse, urlunparse

import requests
import websockets
from websockets.exceptions import ConnectionClosed, WebSocketException

from core.models import PrinterProfile
from core.services.moonraker import DEFAULT_TIMEOUT, MoonrakerError

logger = logging.getLogger(__name__)

#: Objects we subscribe to for live status updates.
SUBSCRIBE_OBJECTS: dict[str, None] = {
    "print_stats": None,
    "virtual_sdcard": None,
    "display_status": None,
    "heater_bed": None,
    "extruder": None,
}

#: Reconnect backoff bounds in seconds.
RECONNECT_INITIAL_DELAY = 1.0
RECONNECT_MAX_DELAY = 30.0


EventHandler = Callable[[dict[str, Any]], Awaitable[None]]


def build_ws_url(moonraker_url: str) -> str:
    """Translate a Moonraker REST URL into its ``/websocket`` WS URL.

    Examples:
        >>> build_ws_url("http://printer.local:7125")
        'ws://printer.local:7125/websocket'
        >>> build_ws_url("https://printer.local/moonraker/")
        'wss://printer.local/moonraker/websocket'
    """
    parsed = urlparse(moonraker_url)
    scheme = {"http": "ws", "https": "wss"}.get(parsed.scheme, "ws")
    path = (parsed.path or "").rstrip("/") + "/websocket"
    return urlunparse((scheme, parsed.netloc, path, "", "", ""))


def fetch_oneshot_token(printer: PrinterProfile) -> str | None:
    """Request a one-shot WebSocket token from Moonraker.

    Returns ``None`` if the printer has no API key configured, or if
    the ``/access/oneshot_token`` endpoint is unavailable (e.g. no
    ``[authorization]`` section in moonraker.conf → Moonraker happily
    accepts unauthenticated websocket connections).
    """
    if not printer.moonraker_api_key:
        return None

    url = printer.moonraker_url.rstrip("/") + "/access/oneshot_token"
    try:
        response = requests.get(
            url,
            headers={"X-Api-Key": printer.moonraker_api_key},
            timeout=DEFAULT_TIMEOUT,
        )
        response.raise_for_status()
        data = response.json()
    except requests.RequestException as exc:
        logger.warning(
            "Oneshot token request failed for %s: %s — connecting without token",
            printer.name,
            exc,
        )
        return None

    token = data.get("result")
    if not isinstance(token, str):
        logger.warning("Unexpected oneshot token payload for %s: %r", printer.name, data)
        return None
    return token


class MoonrakerWebSocketClient:
    """One persistent WebSocket connection to a Moonraker instance."""

    def __init__(self, printer: PrinterProfile, on_event: EventHandler):
        self.printer = printer
        self.on_event = on_event
        self._ws_url = build_ws_url(printer.moonraker_url)
        self._reconnect_delay = RECONNECT_INITIAL_DELAY

    async def run(self) -> None:
        """Connect, subscribe, and pump events until cancelled.

        Reconnects on unexpected disconnects with exponential backoff.
        The backoff is reset to :data:`RECONNECT_INITIAL_DELAY` after
        every successful subscription so that a transient outage
        doesn't permanently increase reconnect sleeps.  Cancellation
        (``asyncio.CancelledError``) propagates out cleanly.
        """
        while True:
            try:
                await self._connect_and_listen()
                # Clean close from the server -> retry with backoff.
                logger.info("%s: websocket closed cleanly, reconnecting", self.printer.name)
            except asyncio.CancelledError:
                raise
            except (ConnectionClosed, WebSocketException, OSError, MoonrakerError) as exc:
                logger.warning("%s: websocket error: %s", self.printer.name, exc)
            except Exception:  # noqa: BLE001
                logger.exception("%s: unexpected worker error", self.printer.name)

            await asyncio.sleep(self._reconnect_delay)
            self._reconnect_delay = min(self._reconnect_delay * 2, RECONNECT_MAX_DELAY)

    async def _connect_and_listen(self) -> None:
        # Run the blocking token fetch in the default executor so we
        # don't stall the event loop.
        loop = asyncio.get_running_loop()
        token = await loop.run_in_executor(None, fetch_oneshot_token, self.printer)
        url = self._ws_url + (f"?token={token}" if token else "")

        logger.info("%s: connecting to %s", self.printer.name, self._ws_url)
        async with websockets.connect(url, ping_interval=20, ping_timeout=20) as ws:
            await self._subscribe(ws)
            logger.info("%s: subscribed, listening for events", self.printer.name)
            # A successful subscribe means the connection is healthy;
            # reset the backoff so the next outage starts fresh.
            self._reconnect_delay = RECONNECT_INITIAL_DELAY
            async for raw in ws:
                try:
                    event = json.loads(raw)
                except json.JSONDecodeError:
                    logger.warning("%s: non-JSON frame received", self.printer.name)
                    continue
                if "method" not in event:
                    # JSON-RPC response to our subscribe call etc. — ignore.
                    continue
                try:
                    await self.on_event(event)
                except Exception:  # noqa: BLE001
                    logger.exception(
                        "%s: event handler failed for %s",
                        self.printer.name,
                        event.get("method"),
                    )

    async def _subscribe(self, ws: Any) -> None:
        request = {
            "jsonrpc": "2.0",
            "method": "printer.objects.subscribe",
            "params": {"objects": SUBSCRIBE_OBJECTS},
            "id": 1,
        }
        await ws.send(json.dumps(request))
