"""Tests for API token self-service view."""

from unittest import mock

from django.contrib.auth.models import User
from django.db import IntegrityError
from django.test import TestCase, override_settings
from django.urls import reverse
from rest_framework.authtoken.models import Token


@override_settings(ALLOWED_HOSTS=["testserver"])
class ApiTokenViewAccessTests(TestCase):
    """Unauthenticated access is redirected; authenticated access returns 200."""

    def setUp(self):
        self.user = User.objects.create_user(username="tokenuser", password="pass123")
        self.url = reverse("core:api_token")

    def test_unauthenticated_redirects_to_login(self):
        resp = self.client.get(self.url)
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/accounts/login/", resp.url)

    def test_authenticated_get_200(self):
        self.client.login(username="tokenuser", password="pass123")
        resp = self.client.get(self.url)
        self.assertEqual(resp.status_code, 200)

    def test_no_token_shows_generate_button(self):
        self.client.login(username="tokenuser", password="pass123")
        resp = self.client.get(self.url)
        self.assertContains(resp, "Generate Token")
        self.assertNotContains(resp, "Rotate")


@override_settings(ALLOWED_HOSTS=["testserver"])
class ApiTokenGenerateTests(TestCase):
    """POST action=generate creates a token and flashes the key."""

    def setUp(self):
        self.user = User.objects.create_user(username="tokenuser", password="pass123")
        self.client.login(username="tokenuser", password="pass123")
        self.url = reverse("core:api_token")

    def test_generate_creates_token(self):
        self.client.post(self.url, {"action": "generate"})
        self.assertTrue(Token.objects.filter(user=self.user).exists())

    def test_generate_redirects(self):
        resp = self.client.post(self.url, {"action": "generate"})
        self.assertRedirects(resp, self.url)

    def test_generate_shows_key_on_next_get(self):
        self.client.post(self.url, {"action": "generate"})
        token = Token.objects.get(user=self.user)
        resp = self.client.get(self.url)
        self.assertContains(resp, token.key)

    def test_generate_key_consumed_after_one_get(self):
        self.client.post(self.url, {"action": "generate"})
        token = Token.objects.get(user=self.user)
        self.client.get(self.url)  # consumes flash
        resp = self.client.get(self.url)  # second GET — key must not appear
        self.assertNotContains(resp, token.key)

    def test_generate_idempotent_no_duplicate(self):
        self.client.post(self.url, {"action": "generate"})
        self.client.post(self.url, {"action": "generate"})
        self.assertEqual(Token.objects.filter(user=self.user).count(), 1)

    def test_existing_token_shows_rotate_and_revoke(self):
        Token.objects.create(user=self.user)
        resp = self.client.get(self.url)
        self.assertContains(resp, "Rotate")
        self.assertContains(resp, "Revoke")
        self.assertNotContains(resp, "Generate Token")


@override_settings(ALLOWED_HOSTS=["testserver"])
class ApiTokenRotateTests(TestCase):
    """POST action=rotate replaces the token with a new one."""

    def setUp(self):
        self.user = User.objects.create_user(username="tokenuser", password="pass123")
        self.client.login(username="tokenuser", password="pass123")
        self.url = reverse("core:api_token")
        self.old_token = Token.objects.create(user=self.user)

    def test_rotate_deletes_old_token(self):
        self.client.post(self.url, {"action": "rotate"})
        self.assertFalse(Token.objects.filter(key=self.old_token.key).exists())

    def test_rotate_creates_new_token(self):
        self.client.post(self.url, {"action": "rotate"})
        self.assertTrue(Token.objects.filter(user=self.user).exists())
        new_token = Token.objects.get(user=self.user)
        self.assertNotEqual(new_token.key, self.old_token.key)

    def test_rotate_redirects(self):
        resp = self.client.post(self.url, {"action": "rotate"})
        self.assertRedirects(resp, self.url)

    def test_rotate_shows_new_key_on_next_get(self):
        self.client.post(self.url, {"action": "rotate"})
        new_token = Token.objects.get(user=self.user)
        resp = self.client.get(self.url)
        self.assertContains(resp, new_token.key)

    def test_rotate_key_consumed_after_one_get(self):
        self.client.post(self.url, {"action": "rotate"})
        new_token = Token.objects.get(user=self.user)
        self.client.get(self.url)  # consumes flash
        resp = self.client.get(self.url)
        self.assertNotContains(resp, new_token.key)

    def test_rotate_is_atomic_on_create_failure(self):
        """A failed replacement create rolls back the delete and reports gracefully."""
        with mock.patch(
            "core.views.auth.Token.objects.create",
            side_effect=IntegrityError("boom"),
        ):
            resp = self.client.post(self.url, {"action": "rotate"})

        # No 500: the IntegrityError is caught and the user is redirected with a message.
        self.assertRedirects(resp, self.url)
        # The atomic block rolls back the delete, so the original token survives intact.
        self.assertTrue(Token.objects.filter(key=self.old_token.key).exists())
        self.assertEqual(Token.objects.filter(user=self.user).count(), 1)


@override_settings(ALLOWED_HOSTS=["testserver"])
class ApiTokenRevokeTests(TestCase):
    """POST action=revoke deletes the token."""

    def setUp(self):
        self.user = User.objects.create_user(username="tokenuser", password="pass123")
        self.client.login(username="tokenuser", password="pass123")
        self.url = reverse("core:api_token")
        self.token = Token.objects.create(user=self.user)

    def test_revoke_deletes_token(self):
        self.client.post(self.url, {"action": "revoke"})
        self.assertFalse(Token.objects.filter(user=self.user).exists())

    def test_revoke_redirects(self):
        resp = self.client.post(self.url, {"action": "revoke"})
        self.assertRedirects(resp, self.url)

    def test_after_revoke_shows_generate_button(self):
        self.client.post(self.url, {"action": "revoke"})
        resp = self.client.get(self.url)
        self.assertContains(resp, "Generate Token")


@override_settings(ALLOWED_HOSTS=["testserver"])
class ApiTokenIsolationTests(TestCase):
    """A user can only manage their own token, never another user's."""

    def setUp(self):
        self.user_a = User.objects.create_user(username="user_a", password="pass123")
        self.user_b = User.objects.create_user(username="user_b", password="pass123")
        self.token_b = Token.objects.create(user=self.user_b)
        self.url = reverse("core:api_token")

    def test_user_a_cannot_see_user_b_token_key(self):
        self.client.login(username="user_a", password="pass123")
        # Generate a token for user A so the page shows a token
        self.client.post(self.url, {"action": "generate"})
        resp = self.client.get(self.url)
        self.assertNotContains(resp, self.token_b.key)

    def test_user_a_generate_does_not_affect_user_b(self):
        self.client.login(username="user_a", password="pass123")
        self.client.post(self.url, {"action": "generate"})
        # user_b's token must still exist and be unchanged
        self.token_b.refresh_from_db()
        self.assertEqual(Token.objects.get(user=self.user_b).key, self.token_b.key)

    def test_user_a_rotate_does_not_affect_user_b(self):
        Token.objects.create(user=self.user_a)
        self.client.login(username="user_a", password="pass123")
        self.client.post(self.url, {"action": "rotate"})
        self.token_b.refresh_from_db()
        self.assertEqual(Token.objects.get(user=self.user_b).key, self.token_b.key)

    def test_user_a_revoke_does_not_affect_user_b(self):
        Token.objects.create(user=self.user_a)
        self.client.login(username="user_a", password="pass123")
        self.client.post(self.url, {"action": "revoke"})
        self.assertTrue(Token.objects.filter(user=self.user_b).exists())
