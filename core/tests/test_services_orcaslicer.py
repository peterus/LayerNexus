"""Comprehensive tests for the OrcaSlicer API client."""

from unittest.mock import MagicMock, patch

import requests
from django.test import TestCase

from core.services.orcaslicer import (
    MultiPlateSliceResult,
    OrcaSlicerAPIClient,
    OrcaSlicerError,
    PlateResult,
    SliceResult,
)


class ParseTimeStringTests(TestCase):
    """Tests for _parse_time_string()."""

    def test_hours_minutes_seconds(self):
        result = OrcaSlicerAPIClient._parse_time_string("1h 23m 45s")
        self.assertAlmostEqual(result, 1 * 3600 + 23 * 60 + 45)

    def test_minutes_seconds_only(self):
        result = OrcaSlicerAPIClient._parse_time_string("5m 30s")
        self.assertAlmostEqual(result, 5 * 60 + 30)

    def test_days_and_hours(self):
        result = OrcaSlicerAPIClient._parse_time_string("1d 2h")
        self.assertAlmostEqual(result, 86400 + 7200)

    def test_seconds_only(self):
        result = OrcaSlicerAPIClient._parse_time_string("45s")
        self.assertAlmostEqual(result, 45)

    def test_empty_string(self):
        result = OrcaSlicerAPIClient._parse_time_string("")
        self.assertIsNone(result)

    def test_invalid_input(self):
        result = OrcaSlicerAPIClient._parse_time_string("no time here")
        self.assertIsNone(result)

    def test_decimal_values(self):
        result = OrcaSlicerAPIClient._parse_time_string("1.5h 30.5m")
        self.assertAlmostEqual(result, 1.5 * 3600 + 30.5 * 60)

    def test_case_insensitive(self):
        result = OrcaSlicerAPIClient._parse_time_string("2H 30M 10S")
        self.assertAlmostEqual(result, 2 * 3600 + 30 * 60 + 10)

    def test_days_hours_minutes_seconds(self):
        result = OrcaSlicerAPIClient._parse_time_string("1d 2h 30m 15s")
        self.assertAlmostEqual(result, 86400 + 7200 + 1800 + 15)


class ParseGcodeMetadataTests(TestCase):
    """Tests for _parse_gcode_metadata() with realistic G-code snippets."""

    def test_parse_filament_grams_and_time(self):
        gcode = b"""; --- Some earlier gcode ---
G1 X100 Y100 E10
; total filament used [g] = 12.34
; filament used [mm] = 4567.89
; total estimated time: 1h 23m 45s
"""
        result = OrcaSlicerAPIClient._parse_gcode_metadata(gcode)

        self.assertAlmostEqual(result["filament_used_grams"], 12.34)
        self.assertAlmostEqual(result["filament_used_mm"], 4567.89)
        self.assertAlmostEqual(result["print_time_seconds"], 1 * 3600 + 23 * 60 + 45)

    def test_parse_alternative_time_format(self):
        gcode = b"""; estimated printing time (normal mode) = 2h 15m 30s
"""
        result = OrcaSlicerAPIClient._parse_gcode_metadata(gcode)
        self.assertAlmostEqual(result["print_time_seconds"], 2 * 3600 + 15 * 60 + 30)

    def test_parse_empty_gcode(self):
        result = OrcaSlicerAPIClient._parse_gcode_metadata(b"")
        self.assertEqual(result, {})

    def test_parse_gcode_without_metadata(self):
        gcode = b"G28\nG1 X10 Y10 Z0.2\nG1 X50 E5\n"
        result = OrcaSlicerAPIClient._parse_gcode_metadata(gcode)
        self.assertEqual(result, {})

    def test_parse_filament_mm_only(self):
        gcode = b"; filament used [m] = 1234.56\n"
        result = OrcaSlicerAPIClient._parse_gcode_metadata(gcode)
        self.assertAlmostEqual(result["filament_used_mm"], 1234.56)


class IsZipTests(TestCase):
    """Tests for _is_zip() static method."""

    def test_zip_magic_bytes(self):
        data = b"PK\x03\x04" + b"\x00" * 100
        self.assertTrue(OrcaSlicerAPIClient._is_zip(data))

    def test_not_zip(self):
        data = b"; G-code file\nG28\nG1 X10\n"
        self.assertFalse(OrcaSlicerAPIClient._is_zip(data))

    def test_empty_data(self):
        self.assertFalse(OrcaSlicerAPIClient._is_zip(b""))

    def test_short_data(self):
        self.assertFalse(OrcaSlicerAPIClient._is_zip(b"PK"))


class SliceResultTests(TestCase):
    """Tests for SliceResult dataclass."""

    def test_defaults(self):
        result = SliceResult(gcode_content=b"G28\n")
        self.assertEqual(result.gcode_content, b"G28\n")
        self.assertIsNone(result.filament_used_grams)
        self.assertIsNone(result.filament_used_mm)
        self.assertIsNone(result.print_time_seconds)

    def test_with_values(self):
        result = SliceResult(
            gcode_content=b"G28\n",
            filament_used_grams=12.5,
            filament_used_mm=4500.0,
            print_time_seconds=3600.0,
        )
        self.assertAlmostEqual(result.filament_used_grams, 12.5)
        self.assertAlmostEqual(result.filament_used_mm, 4500.0)
        self.assertAlmostEqual(result.print_time_seconds, 3600.0)


class MultiPlateSliceResultTests(TestCase):
    """Tests for MultiPlateSliceResult dataclass properties."""

    def test_total_filament_grams_from_plates(self):
        result = MultiPlateSliceResult(
            plates=[
                PlateResult(plate_number=1, gcode_content=b"", filament_used_grams=10.0),
                PlateResult(plate_number=2, gcode_content=b"", filament_used_grams=15.0),
            ],
            header_filament_grams=20.0,
        )
        self.assertAlmostEqual(result.total_filament_grams, 25.0)

    def test_total_filament_grams_fallback_to_header(self):
        result = MultiPlateSliceResult(
            plates=[
                PlateResult(plate_number=1, gcode_content=b"", filament_used_grams=0.0),
            ],
            header_filament_grams=20.0,
        )
        self.assertAlmostEqual(result.total_filament_grams, 20.0)

    def test_total_filament_grams_no_data(self):
        result = MultiPlateSliceResult(plates=[])
        self.assertIsNone(result.total_filament_grams)

    def test_total_filament_mm_from_plates(self):
        result = MultiPlateSliceResult(
            plates=[
                PlateResult(plate_number=1, gcode_content=b"", filament_used_mm=1000.0),
                PlateResult(plate_number=2, gcode_content=b"", filament_used_mm=2000.0),
            ],
        )
        self.assertAlmostEqual(result.total_filament_mm, 3000.0)

    def test_total_filament_mm_fallback_to_header(self):
        result = MultiPlateSliceResult(
            plates=[PlateResult(plate_number=1, gcode_content=b"")],
            header_filament_mm=5000.0,
        )
        self.assertAlmostEqual(result.total_filament_mm, 5000.0)

    def test_total_print_time_from_plates(self):
        result = MultiPlateSliceResult(
            plates=[
                PlateResult(plate_number=1, gcode_content=b"", print_time_seconds=3600.0),
                PlateResult(plate_number=2, gcode_content=b"", print_time_seconds=1800.0),
            ],
        )
        self.assertAlmostEqual(result.total_print_time_seconds, 5400.0)

    def test_total_print_time_fallback_to_header(self):
        result = MultiPlateSliceResult(
            plates=[PlateResult(plate_number=1, gcode_content=b"")],
            header_print_time_seconds=7200.0,
        )
        self.assertAlmostEqual(result.total_print_time_seconds, 7200.0)

    def test_total_print_time_no_data(self):
        result = MultiPlateSliceResult(plates=[])
        self.assertIsNone(result.total_print_time_seconds)


class SliceMethodTests(TestCase):
    """Tests for the slice() method with mocked HTTP requests."""

    def setUp(self):
        self.client = OrcaSlicerAPIClient("http://orcaslicer:3000")

    @patch("requests.post")
    def test_slice_with_model_content_success(self, mock_post):
        mock_response = MagicMock()
        mock_response.content = b"G28\nG1 X10\n"
        mock_response.raise_for_status.return_value = None
        mock_response.headers = {
            "X-Filament-Used-g": "12.5",
            "X-Filament-Used-mm": "4500",
            "X-Print-Time-Seconds": "3600",
        }
        mock_post.return_value = mock_response

        result = self.client.slice(
            model_content=b"fake-3mf-content",
            model_filename="test.3mf",
            printer_profile_name="my_printer",
        )

        self.assertIsInstance(result, SliceResult)
        self.assertEqual(result.gcode_content, b"G28\nG1 X10\n")
        self.assertAlmostEqual(result.filament_used_grams, 12.5)
        self.assertAlmostEqual(result.filament_used_mm, 4500.0)
        self.assertAlmostEqual(result.print_time_seconds, 3600.0)
        mock_post.assert_called_once()

    @patch("requests.post")
    def test_slice_with_model_content_no_headers(self, mock_post):
        mock_response = MagicMock()
        mock_response.content = b"G28\n"
        mock_response.raise_for_status.return_value = None
        mock_response.headers = {}
        mock_post.return_value = mock_response

        result = self.client.slice(model_content=b"data", model_filename="m.stl")

        self.assertIsNone(result.filament_used_grams)
        self.assertIsNone(result.print_time_seconds)

    @patch("requests.post")
    def test_slice_connection_error(self, mock_post):
        mock_post.side_effect = requests.ConnectionError("refused")

        with self.assertRaises(OrcaSlicerError) as ctx:
            self.client.slice(model_content=b"data", model_filename="m.stl")

        self.assertIn("Cannot connect", str(ctx.exception))

    @patch("requests.post")
    def test_slice_timeout(self, mock_post):
        mock_post.side_effect = requests.Timeout("timed out")

        with self.assertRaises(OrcaSlicerError) as ctx:
            self.client.slice(model_content=b"data", model_filename="m.stl")

        self.assertIn("timed out", str(ctx.exception))

    @patch("requests.post")
    def test_slice_http_error(self, mock_post):
        mock_response = MagicMock()
        mock_response.text = "Slicing failed: bad profile"
        mock_response.raise_for_status.side_effect = requests.HTTPError(response=mock_response)
        mock_post.return_value = mock_response

        with self.assertRaises(OrcaSlicerError) as ctx:
            self.client.slice(model_content=b"data", model_filename="m.stl")

        self.assertIn("Slicing failed", str(ctx.exception))

    def test_slice_no_model_raises_error(self):
        with self.assertRaises(OrcaSlicerError) as ctx:
            self.client.slice()

        self.assertIn("model_path or model_content", str(ctx.exception))

    @patch("pathlib.Path.exists", return_value=False)
    def test_slice_model_path_not_found(self, mock_exists):
        with self.assertRaises(FileNotFoundError):
            self.client.slice(model_path="/nonexistent/model.stl")

    @patch("requests.post")
    @patch("pathlib.Path.read_bytes", return_value=b"stl-content")
    @patch("pathlib.Path.exists", return_value=True)
    def test_slice_with_model_path(self, mock_exists, mock_read, mock_post):
        mock_response = MagicMock()
        mock_response.content = b"G28\n"
        mock_response.raise_for_status.return_value = None
        mock_response.headers = {}
        mock_post.return_value = mock_response

        result = self.client.slice(model_path="/tmp/model.stl")

        self.assertEqual(result.gcode_content, b"G28\n")

    @patch("requests.post")
    def test_slice_with_inline_profiles(self, mock_post):
        mock_response = MagicMock()
        mock_response.content = b"G28\n"
        mock_response.raise_for_status.return_value = None
        mock_response.headers = {}
        mock_post.return_value = mock_response

        result = self.client.slice(
            model_content=b"data",
            model_filename="m.3mf",
            printer_profile_json=b'{"name": "printer"}',
            preset_profile_json=b'{"name": "preset"}',
            filament_profile_json=b'{"name": "filament"}',
        )

        self.assertEqual(result.gcode_content, b"G28\n")
        call_kwargs = mock_post.call_args[1]
        self.assertIn("printerProfile", call_kwargs["files"])
        self.assertIn("presetProfile", call_kwargs["files"])
        self.assertIn("filamentProfile", call_kwargs["files"])

    @patch("requests.post")
    def test_slice_invalid_printer_profile_json(self, mock_post):
        with self.assertRaises(OrcaSlicerError) as ctx:
            self.client.slice(
                model_content=b"data",
                model_filename="m.3mf",
                printer_profile_json=b"not valid json{{{",
            )

        self.assertIn("Invalid printer profile JSON", str(ctx.exception))

    @patch("requests.post")
    def test_slice_invalid_preset_profile_json(self, mock_post):
        with self.assertRaises(OrcaSlicerError) as ctx:
            self.client.slice(
                model_content=b"data",
                model_filename="m.3mf",
                preset_profile_json=b"broken json",
            )

        self.assertIn("Invalid preset profile JSON", str(ctx.exception))

    @patch("requests.post")
    def test_slice_with_invalid_header_values(self, mock_post):
        """Non-numeric header values should be silently ignored."""
        mock_response = MagicMock()
        mock_response.content = b"G28\n"
        mock_response.raise_for_status.return_value = None
        mock_response.headers = {
            "X-Filament-Used-g": "not-a-number",
            "X-Filament-Used-mm": "also-bad",
            "X-Print-Time-Seconds": "nope",
        }
        mock_post.return_value = mock_response

        result = self.client.slice(model_content=b"data", model_filename="m.stl")

        self.assertIsNone(result.filament_used_grams)
        self.assertIsNone(result.filament_used_mm)
        self.assertIsNone(result.print_time_seconds)
