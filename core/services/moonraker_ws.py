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
* Re-subscribing after Klippy restarts (Moonraker drops object
  subscriptions while the WebSocket stays open) and, as a safety net,
  whenever no status message arrived for :data:`STATUS_WATCHDOG_INTERVAL`.
* Forwarding the subscribe response's status snapshot as a synthetic
  ``notify_status_update`` without ``print_stats.state`` (see
  :func:`snapshot_event`).

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
from websockets.exceptions import ConnectionClosed, ConnectionClosedOK, WebSocketException

from core.models import PrinterProfile
from core.services.moonraker import DEFAULT_TIMEOUT, MoonrakerError

logger = logging.getLogger(__name__)

#: Objects we subscribe to for live status updates.
SUBSCRIBE_OBJECTS: dict[str, list[str] | None] = {
    # Klippy's own state (ready/startup/shutdown/error) drives the watchdog.
    "webhooks": ["state"],
    "print_stats": None,
    "virtual_sdcard": None,
    "display_status": None,
    "heater_bed": None,
    "extruder": None,
}

#: JSON-RPC request id of our ``printer.objects.subscribe`` call.
SUBSCRIBE_REQUEST_ID = 1

#: Reconnect backoff bounds in seconds.
RECONNECT_INITIAL_DELAY = 1.0
RECONNECT_MAX_DELAY = 30.0

#: Seconds without a status message (while Klippy is ready) before the
#: client re-subscribes; after a second silent window it reconnects.
STATUS_WATCHDOG_INTERVAL = 60.0

#: Notifications after which Moonraker has dropped our subscription and
#: no status updates are expected until ``notify_klippy_ready``.
KLIPPY_DOWN_METHODS = frozenset({"notify_klippy_shutdown", "notify_klippy_disconnected"})


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


def snapshot_event(result: dict[str, Any]) -> dict[str, Any] | None:
    """Turn a subscribe response result into a synthetic status update.

    Moonraker answers ``printer.objects.subscribe`` with the current
    status of all subscribed objects. Forwarding it writes progress
    right after every (re)subscribe. ``print_stats.state`` is dropped:
    right after a Klippy restart it reads ``standby``/``error``, which
    :mod:`core.services.printer_status_sync` treats as terminal — the
    snapshot must never end a print. Completion keeps coming from live
    state changes and ``notify_history_changed``.

    Args:
        result: The ``result`` member of the subscribe response.

    Returns:
        A ``notify_status_update`` event with params
        ``[status, eventtime]``, or ``None`` if nothing is left to forward.
    """
    status = result.get("status")
    if not isinstance(status, dict):
        return None
    snapshot: dict[str, Any] = {}
    for name, value in status.items():
        if name == "print_stats" and isinstance(value, dict):
            value = {key: item for key, item in value.items() if key != "state"}
            if not value:
                continue
        snapshot[name] = value
    if not snapshot:
        return None
    return {
        "jsonrpc": "2.0",
        "method": "notify_status_update",
        "params": [snapshot, result.get("eventtime")],
    }


def klippy_state(status: Any) -> str | None:
    """Return ``webhooks.state`` from a status block, if present.

    Args:
        status: A status block (subscribe result or status update params[0]).

    Returns:
        Klippy's state (``"ready"``, ``"startup"``, ``"shutdown"``,
        ``"error"``), or ``None`` if the block does not carry it.
    """
    if not isinstance(status, dict):
        return None
    webhooks = status.get("webhooks")
    if not isinstance(webhooks, dict):
        return None
    state = webhooks.get("state")
    return state if isinstance(state, str) else None


class MoonrakerWebSocketClient:
    """One persistent WebSocket connection to a Moonraker instance."""

    def __init__(self, printer: PrinterProfile, on_event: EventHandler):
        self.printer = printer
        self.on_event = on_event
        self._ws_url = build_ws_url(printer.moonraker_url)
        self._reconnect_delay = RECONNECT_INITIAL_DELAY
        self._klippy_ready = True
        # Set when our last subscribe was rejected; Klippy's real state is
        # unknown then, so the watchdog keeps probing quietly.
        self._subscribe_failed = False

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
            self._klippy_ready = True
            self._subscribe_failed = False
            await self._listen(ws)

    async def _listen(self, ws: Any) -> None:
        """Pump frames from ``ws`` and run the status watchdog.

        Returns normally on a clean server close or after the watchdog
        closed a connection that stayed silent despite a re-subscribe,
        so :meth:`run` reconnects with backoff.

        Args:
            ws: The open websocket connection.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + STATUS_WATCHDOG_INTERVAL
        resubscribed = False
        while True:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=max(deadline - loop.time(), 0.0))
            except ConnectionClosedOK:
                return
            except TimeoutError:
                if not self._klippy_ready:
                    # Klippy is down: no updates expected, never reconnect.
                    # After a rejected subscribe (no klippy_ready may follow
                    # if Klippy was in fact ready) keep probing once per window.
                    if self._subscribe_failed:
                        logger.debug("%s: retrying rejected subscribe", self.printer.name)
                        await self._subscribe(ws)
                elif not resubscribed:
                    logger.warning(
                        "%s: no status for %.0fs, re-subscribing", self.printer.name, STATUS_WATCHDOG_INTERVAL
                    )
                    await self._subscribe(ws)
                    resubscribed = True
                else:
                    logger.warning("%s: still no status after re-subscribe, reconnecting", self.printer.name)
                    await ws.close()
                    return
                deadline = loop.time() + STATUS_WATCHDOG_INTERVAL
                continue

            try:
                event = json.loads(raw)
            except json.JSONDecodeError:
                logger.warning("%s: non-JSON frame received", self.printer.name)
                continue
            if not isinstance(event, dict):
                logger.warning("%s: unexpected JSON frame received", self.printer.name)
                continue
            if await self._handle_frame(ws, event):
                deadline = loop.time() + STATUS_WATCHDOG_INTERVAL
                resubscribed = False

    async def _handle_frame(self, ws: Any, event: dict[str, Any]) -> bool:
        """Handle one decoded JSON-RPC frame.

        Args:
            ws: The open websocket connection (for re-subscribing).
            event: The decoded frame.

        Returns:
            ``True`` if the frame resets the status watchdog: a
            ``notify_status_update``, a successful subscribe response or
            ``notify_klippy_ready`` (which re-subscribes).
        """
        method = event.get("method")
        if method is None:
            return await self._handle_response(event)

        if method == "notify_status_update":
            params = event.get("params")
            state = klippy_state(params[0] if isinstance(params, list) and params else None)
            if state is not None:
                self._klippy_ready = state == "ready"

        if method in KLIPPY_DOWN_METHODS or method == "notify_klippy_ready":
            self._subscribe_failed = False
        if method == "notify_klippy_ready":
            self._klippy_ready = True
            logger.info("%s: klippy ready, re-subscribing", self.printer.name)
            await self._subscribe(ws)
        elif method in KLIPPY_DOWN_METHODS:
            self._klippy_ready = False
        await self._dispatch(event)
        return method in ("notify_status_update", "notify_klippy_ready")

    async def _handle_response(self, event: dict[str, Any]) -> bool:
        """Handle a JSON-RPC response; only our subscribe response matters.

        Args:
            event: The decoded response frame.

        Returns:
            ``True`` if it was a successful subscribe response.
        """
        if event.get("id") != SUBSCRIBE_REQUEST_ID:
            return False
        if "error" in event:
            # Typically "Klippy Host not connected": pause the watchdog's
            # reconnect; it keeps re-trying the subscribe once per window.
            # Warn once per failure streak, not on every retry.
            log = logger.debug if self._subscribe_failed else logger.warning
            log("%s: subscribe failed: %s", self.printer.name, event["error"])
            self._klippy_ready = False
            self._subscribe_failed = True
            return False
        result = event.get("result")
        if not isinstance(result, dict):
            logger.warning("%s: malformed subscribe response: %r", self.printer.name, result)
            return False
        # An accepted subscribe proves the Klippy host is connected, but it
        # may still be in startup/shutdown/error (state predating this
        # connection, so no notify_klippy_shutdown follows): only "ready"
        # arms the watchdog; otherwise wait for notify_klippy_ready.
        state = klippy_state(result.get("status"))
        self._klippy_ready = state is None or state == "ready"
        self._subscribe_failed = False
        snapshot = snapshot_event(result)
        if snapshot is not None:
            await self._dispatch(snapshot)
        return True

    async def _dispatch(self, event: dict[str, Any]) -> None:
        """Pass ``event`` to the handler; a handler failure never stops the pump.

        Args:
            event: The (possibly synthetic) notification to forward.
        """
        try:
            await self.on_event(event)
        except Exception:  # noqa: BLE001
            logger.exception(
                "%s: event handler failed for %s",
                self.printer.name,
                event.get("method"),
            )

    async def _subscribe(self, ws: Any) -> None:
        """Send the ``printer.objects.subscribe`` request on ``ws``.

        Args:
            ws: The open websocket connection.
        """
        request = {
            "jsonrpc": "2.0",
            "method": "printer.objects.subscribe",
            "params": {"objects": SUBSCRIBE_OBJECTS},
            "id": SUBSCRIBE_REQUEST_ID,
        }
        await ws.send(json.dumps(request))
