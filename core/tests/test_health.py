"""Tests for the ``/health/`` endpoint."""

from django.test import SimpleTestCase, override_settings
from django.urls import reverse


class HealthCheckTests(SimpleTestCase):
    """``/health/`` reports liveness and the running app version."""

    @override_settings(APP_VERSION="0.3.1+abc1234")
    def test_reports_status_and_version(self) -> None:
        response = self.client.get(reverse("health_check"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok", "version": "0.3.1+abc1234"})

    def test_needs_no_login(self) -> None:
        response = self.client.get("/health/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("version", response.json())
