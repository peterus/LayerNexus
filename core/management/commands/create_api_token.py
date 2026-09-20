"""Management command to create or fetch a DRF API token for a user."""

from __future__ import annotations

from typing import Any

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from rest_framework.authtoken.models import Token


class Command(BaseCommand):
    """Create (or return the existing) API token for a given username.

    The printed key is used as ``Authorization: Token <key>`` against ``/api/v1/``.
    The token inherits the user's RBAC: writes require ``core.can_manage_projects``.
    """

    help = "Create or fetch the API token for a user and print the key."

    def add_arguments(self, parser: Any) -> None:
        """Register the positional ``username`` argument."""
        parser.add_argument("username", type=str, help="Username to issue a token for.")

    def handle(self, *args: Any, **options: Any) -> None:
        """Resolve the user, upsert their token via ``get_or_create`` and print the key."""
        username = options["username"]
        user_model = get_user_model()
        try:
            user = user_model.objects.get(username=username)
        except user_model.DoesNotExist as exc:
            raise CommandError(f"No user named {username!r} exists.") from exc

        token, created = Token.objects.get_or_create(user=user)
        verb = "Created" if created else "Existing"
        self.stdout.write(self.style.SUCCESS(f"{verb} token for {username}: {token.key}"))
