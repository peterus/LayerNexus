"""Tests for external service client instantiation."""

from django.test import TestCase

from core.services.moonraker import MoonrakerClient
from core.services.orcaslicer import OrcaSlicerAPIClient
from core.services.spoolman import SpoolmanClient


class OrcaSlicerAPIClientTests(TestCase):
    """Tests for OrcaSlicerAPIClient basic functionality."""

    def test_init_strips_trailing_slash(self):
        client = OrcaSlicerAPIClient("http://localhost:3000/")
        self.assertEqual(client.base_url, "http://localhost:3000")

    def test_init_default_url(self):
        client = OrcaSlicerAPIClient()
        self.assertEqual(client.base_url, "http://orcaslicer:3000")

    def test_url_helper(self):
        client = OrcaSlicerAPIClient("http://localhost:3000")
        self.assertEqual(client._url("/slice"), "http://localhost:3000/slice")


class SpoolmanClientTests(TestCase):
    """Tests for SpoolmanClient instantiation."""

    def test_init_strips_trailing_slash(self):
        client = SpoolmanClient("http://localhost:7912/")
        self.assertEqual(client.base_url, "http://localhost:7912")

    def test_init_no_trailing_slash(self):
        client = SpoolmanClient("http://localhost:7912")
        self.assertEqual(client.base_url, "http://localhost:7912")


class MoonrakerClientTests(TestCase):
    """Tests for MoonrakerClient instantiation."""

    def test_init(self):
        client = MoonrakerClient("http://192.168.1.100:7125", "mykey")
        self.assertEqual(client.base_url, "http://192.168.1.100:7125")
        self.assertEqual(client.api_key, "mykey")

    def test_init_strips_trailing_slash(self):
        client = MoonrakerClient("http://192.168.1.100:7125/")
        self.assertEqual(client.base_url, "http://192.168.1.100:7125")

    def test_headers_with_api_key(self):
        client = MoonrakerClient("http://host", "secret")
        headers = client._get_headers()
        self.assertEqual(headers["X-Api-Key"], "secret")

    def test_headers_without_api_key(self):
        client = MoonrakerClient("http://host")
        headers = client._get_headers()
        self.assertNotIn("X-Api-Key", headers)
