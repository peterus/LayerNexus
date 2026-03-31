"""Comprehensive tests for the Spoolman API client."""

from unittest.mock import MagicMock, patch

import requests
from django.test import TestCase

from core.services.spoolman import SpoolmanClient, SpoolmanError


class SpoolmanRequestTests(TestCase):
    """Tests for the low-level _request() method."""

    def setUp(self):
        self.client = SpoolmanClient("http://spoolman:7912")

    @patch("requests.request")
    def test_request_success(self, mock_request):
        mock_response = MagicMock()
        mock_response.json.return_value = {"id": 1, "name": "PLA"}
        mock_response.raise_for_status.return_value = None
        mock_request.return_value = mock_response

        result = self.client._request("GET", "filament/1")

        self.assertEqual(result, {"id": 1, "name": "PLA"})
        url = mock_request.call_args[0][1]
        self.assertEqual(url, "http://spoolman:7912/api/v1/filament/1")

    @patch("requests.request")
    def test_request_sets_default_timeout(self, mock_request):
        mock_response = MagicMock()
        mock_response.json.return_value = {}
        mock_response.raise_for_status.return_value = None
        mock_request.return_value = mock_response

        self.client._request("GET", "spool")

        call_kwargs = mock_request.call_args[1]
        self.assertEqual(call_kwargs["timeout"], 10)

    @patch("requests.request")
    def test_request_connection_error(self, mock_request):
        mock_request.side_effect = requests.ConnectionError("refused")

        with self.assertRaises(SpoolmanError) as ctx:
            self.client._request("GET", "spool")

        self.assertIn("Cannot connect", str(ctx.exception))

    @patch("requests.request")
    def test_request_timeout(self, mock_request):
        mock_request.side_effect = requests.Timeout("timed out")

        with self.assertRaises(SpoolmanError) as ctx:
            self.client._request("GET", "spool")

        self.assertIn("timed out", str(ctx.exception))

    @patch("requests.request")
    def test_request_http_error(self, mock_request):
        mock_response = MagicMock()
        mock_response.status_code = 404
        mock_response.text = "Not Found"
        mock_response.raise_for_status.side_effect = requests.HTTPError(response=mock_response)
        mock_request.return_value = mock_response

        with self.assertRaises(SpoolmanError) as ctx:
            self.client._request("GET", "spool/999")

        self.assertIn("404", str(ctx.exception))

    @patch("requests.request")
    def test_request_generic_exception(self, mock_request):
        mock_request.side_effect = requests.RequestException("something broke")

        with self.assertRaises(SpoolmanError) as ctx:
            self.client._request("GET", "spool")

        self.assertIn("request failed", str(ctx.exception))

    @patch("requests.request")
    def test_request_strips_leading_slash(self, mock_request):
        mock_response = MagicMock()
        mock_response.json.return_value = []
        mock_response.raise_for_status.return_value = None
        mock_request.return_value = mock_response

        self.client._request("GET", "/spool")

        url = mock_request.call_args[0][1]
        self.assertEqual(url, "http://spoolman:7912/api/v1/spool")


class SpoolmanGetSpoolsTests(TestCase):
    """Tests for get_spools()."""

    def setUp(self):
        self.client = SpoolmanClient("http://spoolman:7912")

    @patch("requests.request")
    def test_get_spools_success(self, mock_request):
        expected = [{"id": 1, "remaining_weight": 800}, {"id": 2, "remaining_weight": 500}]
        mock_response = MagicMock()
        mock_response.json.return_value = expected
        mock_response.raise_for_status.return_value = None
        mock_request.return_value = mock_response

        result = self.client.get_spools()

        self.assertEqual(result, expected)
        self.assertEqual(len(result), 2)
        call_args = mock_request.call_args
        self.assertEqual(call_args[0][0], "GET")

    @patch("requests.request")
    def test_get_spools_error(self, mock_request):
        mock_request.side_effect = requests.ConnectionError("down")

        with self.assertRaises(SpoolmanError):
            self.client.get_spools()

    @patch("requests.request")
    def test_get_spools_empty(self, mock_request):
        mock_response = MagicMock()
        mock_response.json.return_value = []
        mock_response.raise_for_status.return_value = None
        mock_request.return_value = mock_response

        result = self.client.get_spools()

        self.assertEqual(result, [])


class SpoolmanGetFilamentsTests(TestCase):
    """Tests for get_filaments()."""

    def setUp(self):
        self.client = SpoolmanClient("http://spoolman:7912")

    @patch("requests.request")
    def test_get_filaments_success(self, mock_request):
        expected = [{"id": 1, "name": "PLA", "material": "PLA"}]
        mock_response = MagicMock()
        mock_response.json.return_value = expected
        mock_response.raise_for_status.return_value = None
        mock_request.return_value = mock_response

        result = self.client.get_filaments()

        self.assertEqual(result, expected)

    @patch("requests.request")
    def test_get_filaments_error(self, mock_request):
        mock_request.side_effect = requests.Timeout("slow")

        with self.assertRaises(SpoolmanError):
            self.client.get_filaments()


class SpoolmanUseFilamentTests(TestCase):
    """Tests for use_filament()."""

    def setUp(self):
        self.client = SpoolmanClient("http://spoolman:7912")

    @patch("requests.request")
    def test_use_filament_success(self, mock_request):
        expected = {"id": 1, "remaining_weight": 790.5}
        mock_response = MagicMock()
        mock_response.json.return_value = expected
        mock_response.raise_for_status.return_value = None
        mock_request.return_value = mock_response

        result = self.client.use_filament(spool_id=1, grams=9.5)

        self.assertEqual(result, expected)
        call_args = mock_request.call_args
        self.assertEqual(call_args[0][0], "PUT")
        self.assertIn("spool/1/use", call_args[0][1])
        self.assertEqual(call_args[1]["json"], {"use_weight": 9.5})

    @patch("requests.request")
    def test_use_filament_error(self, mock_request):
        mock_response = MagicMock()
        mock_response.status_code = 404
        mock_response.text = "Spool not found"
        mock_response.raise_for_status.side_effect = requests.HTTPError(response=mock_response)
        mock_request.return_value = mock_response

        with self.assertRaises(SpoolmanError) as ctx:
            self.client.use_filament(spool_id=999, grams=5.0)

        self.assertIn("404", str(ctx.exception))


class SpoolmanGetSpoolTests(TestCase):
    """Tests for get_spool() (single spool)."""

    def setUp(self):
        self.client = SpoolmanClient("http://spoolman:7912")

    @patch("requests.request")
    def test_get_spool_success(self, mock_request):
        expected = {"id": 42, "remaining_weight": 750}
        mock_response = MagicMock()
        mock_response.json.return_value = expected
        mock_response.raise_for_status.return_value = None
        mock_request.return_value = mock_response

        result = self.client.get_spool(42)

        self.assertEqual(result, expected)
        url = mock_request.call_args[0][1]
        self.assertIn("spool/42", url)


class SpoolmanGetFilamentTests(TestCase):
    """Tests for get_filament() (single filament)."""

    def setUp(self):
        self.client = SpoolmanClient("http://spoolman:7912")

    @patch("requests.request")
    def test_get_filament_success(self, mock_request):
        expected = {"id": 5, "name": "PETG", "material": "PETG"}
        mock_response = MagicMock()
        mock_response.json.return_value = expected
        mock_response.raise_for_status.return_value = None
        mock_request.return_value = mock_response

        result = self.client.get_filament(5)

        self.assertEqual(result, expected)

    @patch("requests.request")
    def test_get_filament_error(self, mock_request):
        mock_request.side_effect = requests.ConnectionError("down")

        with self.assertRaises(SpoolmanError):
            self.client.get_filament(1)


class SpoolmanGetVendorsTests(TestCase):
    """Tests for get_vendors()."""

    def setUp(self):
        self.client = SpoolmanClient("http://spoolman:7912")

    @patch("requests.request")
    def test_get_vendors_success(self, mock_request):
        expected = [{"id": 1, "name": "Bambu Lab"}, {"id": 2, "name": "Polymaker"}]
        mock_response = MagicMock()
        mock_response.json.return_value = expected
        mock_response.raise_for_status.return_value = None
        mock_request.return_value = mock_response

        result = self.client.get_vendors()

        self.assertEqual(result, expected)
        self.assertEqual(len(result), 2)
