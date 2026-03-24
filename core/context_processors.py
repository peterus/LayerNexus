"""Context processors for the LayerNexus application."""

from django.conf import settings
from django.http import HttpRequest


def app_name(request: HttpRequest) -> dict[str, str]:
    """Add the application name to the template context."""
    return {"APP_NAME": getattr(settings, "APP_NAME", "LayerNexus")}


def allow_registration(request: HttpRequest) -> dict[str, bool]:
    """Add the registration toggle to the template context."""
    return {"ALLOW_REGISTRATION": getattr(settings, "ALLOW_REGISTRATION", True)}
