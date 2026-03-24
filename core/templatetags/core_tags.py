"""Custom template tags and filters for the LayerNexus application."""

import datetime

import bleach
import markdown as md
from django import template
from django.utils.safestring import mark_safe

register = template.Library()


ALLOWED_TAGS = [
    "p",
    "br",
    "strong",
    "em",
    "b",
    "i",
    "u",
    "s",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "ul",
    "ol",
    "li",
    "a",
    "code",
    "pre",
    "blockquote",
    "hr",
    "img",
    "table",
    "thead",
    "tbody",
    "tr",
    "th",
    "td",
]

ALLOWED_ATTRIBUTES = {
    "a": ["href", "title", "target", "rel"],
    "img": ["src", "alt", "title", "width", "height"],
    "td": ["align"],
    "th": ["align"],
}


@register.filter(name="markdown")
def render_markdown(value: str) -> str:
    """Render a Markdown string as sanitised HTML.

    Usage: {{ object.description|markdown }}
    """
    if not value:
        return ""
    html = md.markdown(
        value,
        extensions=["tables", "fenced_code", "nl2br"],
    )
    clean = bleach.clean(html, tags=ALLOWED_TAGS, attributes=ALLOWED_ATTRIBUTES)
    return mark_safe(clean)  # noqa: S308 — safe because bleach.clean() sanitises the HTML


@register.filter
def duration_format(value):
    """Format a timedelta as a human-readable string (e.g. '2h 15m').

    Usage: {{ part.estimated_print_time|duration_format }}
    """
    if not isinstance(value, datetime.timedelta):
        return value

    total_seconds = int(value.total_seconds())
    if total_seconds < 0:
        return "0m"

    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)

    parts = []
    if hours:
        parts.append(f"{hours}h")
    if minutes:
        parts.append(f"{minutes}m")
    if not parts:
        parts.append(f"{seconds}s")
    return " ".join(parts)


@register.filter
def file_size(value):
    """Format a file size in bytes as a human-readable string.

    Usage: {{ file.size|file_size }}
    """
    try:
        size = int(value)
    except (TypeError, ValueError):
        return value

    for unit in ("B", "KB", "MB", "GB"):
        if abs(size) < 1024:
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


@register.filter
def percentage(value, total):
    """Calculate percentage of value / total.

    Usage: {{ completed|percentage:total }}
    """
    try:
        return int(float(value) / float(total) * 100)
    except (TypeError, ValueError, ZeroDivisionError):
        return 0


@register.filter
def grams_to_kg(value):
    """Convert grams to kilograms with two decimal places.

    Usage: {{ weight_grams|grams_to_kg }}
    """
    try:
        kg = float(value) / 1000
        return f"{kg:.2f} kg"
    except (TypeError, ValueError):
        return value


@register.filter
def widget_class(field):
    """Return the widget class name for a form field.

    Usage: {{ field|widget_class }} returns e.g. 'TextInput', 'Select', etc.
    Django 6.0 disallows double-underscore attribute access in templates,
    so this filter replaces ``field.field.widget.__class__.__name__``.
    """
    try:
        return field.field.widget.__class__.__name__
    except AttributeError:
        return ""


@register.filter
def meters_format(value):
    """Format meters with one decimal place.

    Usage: {{ length_meters|meters_format }}
    """
    try:
        return f"{float(value):.1f} m"
    except (TypeError, ValueError):
        return value


@register.filter
def mapping_profile_id(mappings_dict, filament_id):
    """Return the orca_filament_profile PK for a given Spoolman filament ID.

    Usage: {{ mappings_by_filament_id|mapping_profile_id:filament.id }}
    Returns the profile PK (int) or empty string if no mapping exists.
    """
    if not isinstance(mappings_dict, dict):
        return ""
    mapping = mappings_dict.get(filament_id)
    if mapping and mapping.orca_filament_profile_id:
        return mapping.orca_filament_profile_id
    return ""


@register.filter
def strip_port(url):
    """Remove the port from a URL so the browser defaults to port 80/443.

    Example: ``http://192.168.1.100:7125`` → ``http://192.168.1.100``

    Usage: ``{{ printer.moonraker_url|strip_port }}``
    """
    if not url:
        return ""
    from urllib.parse import urlparse, urlunparse

    parsed = urlparse(str(url))
    # Rebuild without port (_replace sets netloc hostname only)
    netloc = parsed.hostname or ""
    return urlunparse(parsed._replace(netloc=netloc))


@register.filter
def dict_get(d: dict, key) -> str:
    """Look up a key in a dictionary, returning empty string if not found.

    Useful for dict lookups with variable keys in templates where
    ``dict.key`` syntax cannot be used.

    Usage: ``{{ filament_names|dict_get:part.spoolman_filament_id }}``

    Args:
        d: Dictionary to look up.
        key: Key to search for.

    Returns:
        The value for the key or empty string.
    """
    if not isinstance(d, dict):
        return ""
    return d.get(key, "")
