"""Tests for the Moonraker WebSocket helpers."""

from unittest.mock import MagicMock, patch

import requests
from django.test import TestCase

from core.services.moonraker_ws import build_ws_url, fetch_oneshot_token
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
