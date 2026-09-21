"""Smoke test for drf-spectacular OpenAPI schema generation.

Verifies that the schema can be generated without raising an exception,
and that the /api/schema/ endpoint returns a valid YAML response.
"""

from django.test import TestCase


class SchemaGenerationTest(TestCase):
    """Ensure drf-spectacular can generate the OpenAPI schema without errors."""

    def test_schema_generation_succeeds(self) -> None:
        """``SpectacularAPIView`` must return 200 with YAML content for an authenticated user."""
        from django.contrib.auth.models import User
        from rest_framework.authtoken.models import Token

        user = User.objects.create_user(username="schematest", password="pw")
        token = Token.objects.create(user=user)
        response = self.client.get(
            "/api/schema/",
            HTTP_AUTHORIZATION=f"Token {token.key}",
            HTTP_ACCEPT="application/vnd.oai.openapi",
        )
        self.assertEqual(response.status_code, 200)
        content = response.content.decode()
        self.assertIn("LayerNexus API", content)
        self.assertIn("/api/v1/", content)
