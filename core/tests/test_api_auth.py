"""Authentication & RBAC tests for the build API."""

from __future__ import annotations

from django.contrib.auth.models import Permission, User
from rest_framework.authtoken.models import Token
from rest_framework.test import APITestCase


def grant_manage(user: User) -> None:
    """Grant a user the ``core.can_manage_projects`` permission (UI-parity write RBAC)."""
    user.user_permissions.add(Permission.objects.get(codename="can_manage_projects"))


class ApiAuthTests(APITestCase):
    """Token authentication and manage-permission gating."""

    def test_unauthenticated_is_denied(self) -> None:
        """An anonymous request is rejected (401/403)."""
        resp = self.client.get("/api/v1/projects/")
        self.assertIn(resp.status_code, (401, 403))

    def test_token_auth_grants_read(self) -> None:
        """A valid token authenticates a read request."""
        user = User.objects.create_user("apiuser", password="x")
        token = Token.objects.create(user=user)
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {token.key}")
        resp = self.client.get("/api/v1/projects/")
        self.assertEqual(resp.status_code, 200)

    def test_write_requires_manage_perm(self) -> None:
        """A reader without ``can_manage_projects`` cannot create (403)."""
        user = User.objects.create_user("reader", password="x")  # no groups → no manage perm
        token = Token.objects.create(user=user)
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {token.key}")
        resp = self.client.post("/api/v1/projects/", {"name": "X"}, format="json")
        self.assertEqual(resp.status_code, 403)

    def test_write_allowed_with_manage_perm(self) -> None:
        """A user holding ``can_manage_projects`` can create (201)."""
        from core.models import OrcaPrintPreset

        user = User.objects.create_user("designer", password="x")
        grant_manage(user)
        token = Token.objects.create(user=user)
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {token.key}")
        preset = OrcaPrintPreset.objects.create(
            name="P", orca_name="P", state=OrcaPrintPreset.STATE_RESOLVED, instantiation=True
        )
        resp = self.client.post(
            "/api/v1/projects/", {"name": "Robot", "default_print_preset": preset.pk}, format="json"
        )
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(resp.data["name"], "Robot")

    def test_create_api_token_command(self) -> None:
        """The ``create_api_token`` command issues a usable token for a user."""
        from io import StringIO

        from django.core.management import call_command

        User.objects.create_user("cmduser", password="x")
        out = StringIO()
        call_command("create_api_token", "cmduser", stdout=out)
        self.assertIn("cmduser", out.getvalue())
        self.assertTrue(Token.objects.filter(user__username="cmduser").exists())
