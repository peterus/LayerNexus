"""Tests for downloading imported OrcaSlicer profiles as original JSON."""

import json

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from core.models import OrcaFilamentProfile, OrcaMachineProfile, OrcaPrintPreset

CASES = [
    (OrcaMachineProfile, "orcamachineprofile"),
    (OrcaFilamentProfile, "orcafilamentprofile"),
    (OrcaPrintPreset, "orcaprintpreset"),
]
PAYLOAD = {"name": "Bambu ä", "nozzle_diameter": ["0.4"], "inherits": "base"}


class OrcaProfileDownloadTests(TestCase):
    """Download returns uploaded_json unchanged for all three profile types."""

    def setUp(self) -> None:
        self.user = User.objects.create_user(username="dl", password="pw")

    def _create(self, model, uploaded_json=None, orca_name="My Profile/../x"):
        return model.objects.create(
            name="Profile",
            orca_name=orca_name,
            state=model.STATE_RESOLVED,
            uploaded_json=PAYLOAD if uploaded_json is None else uploaded_json,
        )

    def test_download_returns_original_json(self) -> None:
        self.client.force_login(self.user)
        for model, prefix in CASES:
            with self.subTest(model=model.__name__):
                profile = self._create(model)
                response = self.client.get(reverse(f"core:{prefix}_download", args=[profile.pk]))
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response["Content-Type"], "application/json")
                self.assertEqual(json.loads(response.content), PAYLOAD)
                disposition = response["Content-Disposition"]
                self.assertTrue(disposition.startswith("attachment"))
                self.assertIn(".json", disposition)
                self.assertNotIn("/", disposition)

    def test_login_required(self) -> None:
        for model, prefix in CASES:
            with self.subTest(model=model.__name__):
                profile = self._create(model)
                response = self.client.get(reverse(f"core:{prefix}_download", args=[profile.pk]))
                self.assertEqual(response.status_code, 302)

    def test_empty_json_is_404(self) -> None:
        self.client.force_login(self.user)
        for model, prefix in CASES:
            with self.subTest(model=model.__name__):
                profile = self._create(model, uploaded_json={})
                response = self.client.get(reverse(f"core:{prefix}_download", args=[profile.pk]))
                self.assertEqual(response.status_code, 404)

    def test_detail_shows_button_only_with_json(self) -> None:
        self.client.force_login(self.user)
        for model, prefix in CASES:
            with self.subTest(model=model.__name__):
                full = self._create(model)
                empty = self._create(model, uploaded_json={})
                url = reverse(f"core:{prefix}_download", args=[full.pk])
                self.assertContains(self.client.get(reverse(f"core:{prefix}_detail", args=[full.pk])), url)
                body = self.client.get(reverse(f"core:{prefix}_detail", args=[empty.pk])).content.decode()
                self.assertNotIn("/download/", body)
