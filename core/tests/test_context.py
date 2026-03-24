"""Tests for context processors."""

from django.test import TestCase


class ContextProcessorTests(TestCase):
    """Tests for context processors."""

    def test_app_name(self):
        from django.test import RequestFactory

        from core.context_processors import app_name

        request = RequestFactory().get("/")
        ctx = app_name(request)
        self.assertEqual(ctx["APP_NAME"], "LayerNexus")

    def test_allow_registration_default(self):
        from django.test import RequestFactory

        from core.context_processors import allow_registration

        request = RequestFactory().get("/")
        ctx = allow_registration(request)
        self.assertIn("ALLOW_REGISTRATION", ctx)
        self.assertIsInstance(ctx["ALLOW_REGISTRATION"], bool)
