"""Lookup-read tests for the build API (spoolman filaments, print presets)."""

from __future__ import annotations

from django.contrib.auth.models import User
from rest_framework.authtoken.models import Token
from rest_framework.test import APITestCase

from core.models import OrcaPrintPreset, SpoolmanFilamentMapping


class ApiLookupTests(APITestCase):
    """The read-only lookup endpoints expose valid FK choices to API clients."""

    def setUp(self) -> None:
        """Authenticate a plain (read-only) user — lookups only need authentication."""
        self.user = User.objects.create_user("viewer", password="x")
        token = Token.objects.create(user=self.user)
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {token.key}")

    def test_spoolman_filaments_list(self) -> None:
        """GET /spoolman-filaments/ returns the created mapping's lookup fields."""
        SpoolmanFilamentMapping.objects.create(
            spoolman_filament_id=42,
            spoolman_filament_name="Prusament PLA Galaxy Black",
            spoolman_color_hex="1A1A1A",
        )
        resp = self.client.get("/api/v1/spoolman-filaments/")
        self.assertEqual(resp.status_code, 200, resp.data)
        results = resp.data["results"] if isinstance(resp.data, dict) and "results" in resp.data else resp.data
        self.assertEqual(len(results), 1)
        row = results[0]
        self.assertEqual(row["spoolman_filament_id"], 42)
        self.assertEqual(row["spoolman_filament_name"], "Prusament PLA Galaxy Black")
        self.assertEqual(row["spoolman_color_hex"], "1A1A1A")

    def test_print_presets_only_resolved_instantiable(self) -> None:
        """GET /print-presets/ returns only resolved + instantiable presets."""
        wanted = OrcaPrintPreset.objects.create(
            name="0.20mm Standard",
            state=OrcaPrintPreset.STATE_RESOLVED,
            instantiation=True,
        )
        # Non-instantiable (template only) — must be excluded.
        OrcaPrintPreset.objects.create(
            name="fdm_process_common",
            state=OrcaPrintPreset.STATE_RESOLVED,
            instantiation=False,
        )
        # Pending (unresolved) — must be excluded.
        OrcaPrintPreset.objects.create(
            name="0.28mm Draft",
            state=OrcaPrintPreset.STATE_PENDING,
            instantiation=True,
        )
        resp = self.client.get("/api/v1/print-presets/")
        self.assertEqual(resp.status_code, 200, resp.data)
        results = resp.data["results"] if isinstance(resp.data, dict) and "results" in resp.data else resp.data
        self.assertEqual([r["id"] for r in results], [wanted.pk])
        self.assertEqual(results[0]["name"], "0.20mm Standard")

    def test_lookups_require_authentication(self) -> None:
        """Anonymous clients are rejected from the lookup endpoints."""
        self.client.credentials()
        self.assertEqual(self.client.get("/api/v1/spoolman-filaments/").status_code, 401)
        self.assertEqual(self.client.get("/api/v1/print-presets/").status_code, 401)
