"""Comprehensive tests for the Moonraker API client."""

from unittest.mock import MagicMock, mock_open, patch

import requests
from django.test import TestCase

from core.services.moonraker import MoonrakerClient, MoonrakerError
from core.services.printer_backend import NormalizedJobStatus, PrinterError


class MoonrakerHeaderTests(TestCase):
    """Tests for header generation with and without API key."""

    def test_headers_with_api_key(self):
        client = MoonrakerClient("http://printer:7125", api_key="secret-key")
        headers = client._get_headers()
        self.assertEqual(headers["X-Api-Key"], "secret-key")

    def test_headers_without_api_key(self):
        client = MoonrakerClient("http://printer:7125")
        headers = client._get_headers()
        self.assertEqual(headers, {})

    def test_headers_empty_api_key(self):
        client = MoonrakerClient("http://printer:7125", api_key="")
        headers = client._get_headers()
        self.assertNotIn("X-Api-Key", headers)


class MoonrakerRequestTests(TestCase):
    """Tests for the low-level _request() method."""

    def setUp(self):
        self.client = MoonrakerClient("http://printer:7125", api_key="test-key")

    @patch("requests.request")
    def test_request_success(self, mock_request):
        mock_response = MagicMock()
        mock_response.json.return_value = {"result": "ok"}
        mock_response.raise_for_status.return_value = None
        mock_request.return_value = mock_response

        result = self.client._request("GET", "/test/endpoint")

        self.assertEqual(result, {"result": "ok"})
        mock_request.assert_called_once()
        call_kwargs = mock_request.call_args
        self.assertEqual(call_kwargs[0][0], "GET")
        self.assertIn("http://printer:7125/test/endpoint", call_kwargs[0][1])

    @patch("requests.request")
    def test_request_includes_api_key_header(self, mock_request):
        mock_response = MagicMock()
        mock_response.json.return_value = {}
        mock_response.raise_for_status.return_value = None
        mock_request.return_value = mock_response

        self.client._request("GET", "/test")

        call_kwargs = mock_request.call_args[1]
        self.assertEqual(call_kwargs["headers"]["X-Api-Key"], "test-key")

    @patch("requests.request")
    def test_request_sets_default_timeout(self, mock_request):
        mock_response = MagicMock()
        mock_response.json.return_value = {}
        mock_response.raise_for_status.return_value = None
        mock_request.return_value = mock_response

        self.client._request("GET", "/test")

        call_kwargs = mock_request.call_args[1]
        self.assertEqual(call_kwargs["timeout"], 15)

    @patch("requests.request")
    def test_request_connection_error(self, mock_request):
        mock_request.side_effect = requests.ConnectionError("refused")

        with self.assertRaises(MoonrakerError) as ctx:
            self.client._request("GET", "/test")

        self.assertIn("Cannot connect", str(ctx.exception))

    @patch("requests.request")
    def test_request_timeout(self, mock_request):
        mock_request.side_effect = requests.Timeout("timed out")

        with self.assertRaises(MoonrakerError) as ctx:
            self.client._request("GET", "/test")

        self.assertIn("timed out", str(ctx.exception))

    @patch("requests.request")
    def test_request_http_error(self, mock_request):
        mock_response = MagicMock()
        mock_response.status_code = 500
        mock_response.text = "Internal Server Error"
        mock_response.raise_for_status.side_effect = requests.HTTPError(response=mock_response)
        mock_request.return_value = mock_response

        with self.assertRaises(MoonrakerError) as ctx:
            self.client._request("GET", "/test")

        self.assertIn("500", str(ctx.exception))

    @patch("requests.request")
    def test_request_generic_request_exception(self, mock_request):
        mock_request.side_effect = requests.RequestException("something broke")

        with self.assertRaises(MoonrakerError) as ctx:
            self.client._request("GET", "/test")

        self.assertIn("request failed", str(ctx.exception))

    @patch("requests.request")
    def test_request_strips_leading_slash(self, mock_request):
        mock_response = MagicMock()
        mock_response.json.return_value = {}
        mock_response.raise_for_status.return_value = None
        mock_request.return_value = mock_response

        self.client._request("GET", "/printer/status")

        url = mock_request.call_args[0][1]
        self.assertEqual(url, "http://printer:7125/printer/status")


class MoonrakerGetPrinterStatusTests(TestCase):
    """Tests for get_printer_status()."""

    def setUp(self):
        self.client = MoonrakerClient("http://printer:7125")

    @patch("requests.request")
    def test_get_printer_status_success(self, mock_request):
        expected = {"result": {"status": {"print_stats": {}, "heater_bed": {}, "extruder": {}}}}
        mock_response = MagicMock()
        mock_response.json.return_value = expected
        mock_response.raise_for_status.return_value = None
        mock_request.return_value = mock_response

        result = self.client.get_printer_status()

        self.assertEqual(result, expected)
        call_args = mock_request.call_args
        self.assertEqual(call_args[0][0], "GET")
        self.assertIn("printer/objects/query", call_args[0][1])

    @patch("requests.request")
    def test_get_printer_status_error(self, mock_request):
        mock_request.side_effect = requests.ConnectionError("down")

        with self.assertRaises(MoonrakerError):
            self.client.get_printer_status()


class MoonrakerUploadGcodeTests(TestCase):
    """Tests for upload_gcode()."""

    def setUp(self):
        self.client = MoonrakerClient("http://printer:7125", api_key="key")

    @patch("requests.post")
    @patch("builtins.open", mock_open(read_data=b"G28\nG1 X10 Y10\n"))
    @patch("pathlib.Path.exists", return_value=True)
    def test_upload_gcode_success(self, mock_exists, mock_post):
        mock_response = MagicMock()
        mock_response.json.return_value = {"item": {"path": "test.gcode"}}
        mock_response.raise_for_status.return_value = None
        mock_post.return_value = mock_response

        result = self.client.upload_gcode("/test.gcode")

        self.assertEqual(result["item"]["path"], "test.gcode")
        mock_post.assert_called_once()

    @patch("requests.post")
    @patch("builtins.open", mock_open(read_data=b"G28\n"))
    @patch("pathlib.Path.exists", return_value=True)
    def test_upload_gcode_custom_filename(self, mock_exists, mock_post):
        mock_response = MagicMock()
        mock_response.json.return_value = {"item": {"path": "custom.gcode"}}
        mock_response.raise_for_status.return_value = None
        mock_post.return_value = mock_response

        result = self.client.upload_gcode("/test.gcode", filename="custom.gcode")

        self.assertEqual(result["item"]["path"], "custom.gcode")

    def test_upload_gcode_file_not_found(self):
        with self.assertRaises(FileNotFoundError):
            self.client.upload_gcode("/nonexistent/path.gcode")

    @patch("requests.post")
    @patch("builtins.open", mock_open(read_data=b"G28\n"))
    @patch("pathlib.Path.exists", return_value=True)
    def test_upload_gcode_connection_error(self, mock_exists, mock_post):
        mock_post.side_effect = requests.ConnectionError("down")

        with self.assertRaises(MoonrakerError) as ctx:
            self.client.upload_gcode("/test.gcode")

        self.assertIn("Cannot connect", str(ctx.exception))

    @patch("requests.post")
    @patch("builtins.open", mock_open(read_data=b"G28\n"))
    @patch("pathlib.Path.exists", return_value=True)
    def test_upload_gcode_request_exception(self, mock_exists, mock_post):
        mock_post.side_effect = requests.RequestException("upload error")

        with self.assertRaises(MoonrakerError) as ctx:
            self.client.upload_gcode("/test.gcode")

        self.assertIn("Upload failed", str(ctx.exception))


class MoonrakerStartPrintTests(TestCase):
    """Tests for start_print()."""

    def setUp(self):
        self.client = MoonrakerClient("http://printer:7125")

    @patch("requests.request")
    def test_start_print_success(self, mock_request):
        mock_response = MagicMock()
        mock_response.json.return_value = {"result": "ok"}
        mock_response.raise_for_status.return_value = None
        mock_request.return_value = mock_response

        result = self.client.start_print("test.gcode")

        self.assertEqual(result, {"result": "ok"})
        call_args = mock_request.call_args
        self.assertEqual(call_args[0][0], "POST")
        self.assertIn("printer/print/start", call_args[0][1])

    @patch("requests.request")
    def test_start_print_error(self, mock_request):
        mock_request.side_effect = requests.ConnectionError("down")

        with self.assertRaises(MoonrakerError):
            self.client.start_print("test.gcode")


class MoonrakerCancelPrintTests(TestCase):
    """Tests for cancel_print()."""

    def setUp(self):
        self.client = MoonrakerClient("http://printer:7125")

    @patch("requests.request")
    def test_cancel_print_success(self, mock_request):
        mock_response = MagicMock()
        mock_response.json.return_value = {"result": "ok"}
        mock_response.raise_for_status.return_value = None
        mock_request.return_value = mock_response

        result = self.client.cancel_print()

        self.assertEqual(result, {"result": "ok"})
        call_args = mock_request.call_args
        self.assertEqual(call_args[0][0], "POST")
        self.assertIn("printer/print/cancel", call_args[0][1])

    @patch("requests.request")
    def test_cancel_print_error(self, mock_request):
        mock_request.side_effect = requests.Timeout("timed out")

        with self.assertRaises(MoonrakerError):
            self.client.cancel_print()


class MoonrakerGetJobStatusTests(TestCase):
    """Tests for get_job_status()."""

    def setUp(self):
        self.client = MoonrakerClient("http://printer:7125")

    @patch("requests.request")
    def test_get_job_status_success(self, mock_request):
        raw = {
            "result": {
                "status": {
                    "print_stats": {"state": "printing", "filename": "test.gcode"},
                    "virtual_sdcard": {"progress": 0.42},
                }
            }
        }
        mock_response = MagicMock()
        mock_response.json.return_value = raw
        mock_response.raise_for_status.return_value = None
        mock_request.return_value = mock_response

        result = self.client.get_job_status()

        self.assertIsInstance(result, NormalizedJobStatus)
        self.assertEqual(result.state, NormalizedJobStatus.STATE_PRINTING)
        self.assertAlmostEqual(result.progress, 0.42)
        self.assertEqual(result.filename, "test.gcode")
        self.assertFalse(result.is_terminal)
        call_args = mock_request.call_args
        self.assertIn("printer/objects/query", call_args[0][1])

    @patch("requests.request")
    def test_get_job_status_complete_is_terminal(self, mock_request):
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "result": {
                "status": {
                    "print_stats": {"state": "complete", "filename": "done.gcode"},
                    "virtual_sdcard": {"progress": 1.0},
                }
            }
        }
        mock_response.raise_for_status.return_value = None
        mock_request.return_value = mock_response

        result = self.client.get_job_status()

        self.assertEqual(result.state, NormalizedJobStatus.STATE_COMPLETE)
        self.assertTrue(result.is_terminal)

    @patch("requests.request")
    def test_get_job_status_paused_maps_to_printing(self, mock_request):
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "result": {
                "status": {
                    "print_stats": {"state": "paused", "filename": "x.gcode"},
                    "virtual_sdcard": {"progress": 0.5},
                }
            }
        }
        mock_response.raise_for_status.return_value = None
        mock_request.return_value = mock_response

        result = self.client.get_job_status()

        self.assertEqual(result.state, NormalizedJobStatus.STATE_PRINTING)

    @patch("requests.request")
    def test_get_job_status_raw(self, mock_request):
        raw = {
            "result": {
                "status": {
                    "print_stats": {"state": "printing"},
                    "virtual_sdcard": {"progress": 0.1},
                }
            }
        }
        mock_response = MagicMock()
        mock_response.json.return_value = raw
        mock_response.raise_for_status.return_value = None
        mock_request.return_value = mock_response

        result = self.client.get_job_status_raw()

        self.assertEqual(result, raw)

    @patch("requests.request")
    def test_get_job_status_error(self, mock_request):
        mock_request.side_effect = requests.ConnectionError("down")

        with self.assertRaises(MoonrakerError):
            self.client.get_job_status()

    def test_moonraker_error_is_printer_error(self):
        """MoonrakerError must extend PrinterError so views can catch uniformly."""
        self.assertTrue(issubclass(MoonrakerError, PrinterError))
