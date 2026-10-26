"""Shared helpers to label and colour filaments shown in the UI."""

import re

from core.models import Part, SpoolmanFilamentMapping

# Strict 6-digit hex colour, with or without leading "#". Anything else is never rendered.
_HEX_RE = re.compile(r"#?[0-9a-fA-F]{6}")


def safe_hex(value: str | None) -> str:
    """Return ``value`` as a ``#RRGGBB`` string, or ``""`` if it is not a strict 6-digit hex colour."""
    if not value or not _HEX_RE.fullmatch(value):
        return ""
    return value if value.startswith("#") else f"#{value}"


def filament_label(filament_id: int, mapping_name: str | None, material: str | None) -> str:
    """Return the display label of a Spoolman filament: mapping name, else material, else ``Filament #id``."""
    return mapping_name or material or f"Filament #{filament_id}"


def mapping_lookup(filament_ids: set[int]) -> dict[int, SpoolmanFilamentMapping]:
    """Fetch the mappings for ``filament_ids`` in one query, keyed by Spoolman filament id."""
    if not filament_ids:
        return {}
    return {
        m.spoolman_filament_id: m for m in SpoolmanFilamentMapping.objects.filter(spoolman_filament_id__in=filament_ids)
    }


def filament_badges(parts: list[Part], mappings: dict[int, SpoolmanFilamentMapping]) -> list[dict[str, str]]:
    """Return the distinct filaments of ``parts`` as ``{"label", "hex"}`` dicts (``hex`` may be empty).

    Parts with a Spoolman filament id are grouped by it; parts without one are grouped
    by material and colour and skipped entirely when both are empty. The result is sorted by label.
    """
    badges: dict[object, dict[str, str]] = {}
    for part in parts:
        if part.spoolman_filament_id:
            key: object = part.spoolman_filament_id
            if key in badges:
                continue
            mapping = mappings.get(part.spoolman_filament_id)
            label = filament_label(
                part.spoolman_filament_id, mapping.spoolman_filament_name if mapping else "", part.material
            )
            hex_color = safe_hex(mapping.spoolman_color_hex if mapping else "") or safe_hex(part.color)
        else:
            key = (part.material, part.color)
            hex_color = safe_hex(part.color)
            # A hex colour is shown as the swatch; a free-text colour must stay in the label to tell badges apart.
            label = " · ".join(v for v in (part.material, "" if hex_color else part.color) if v)
            if not label or key in badges:
                continue
        badges[key] = {"label": label, "hex": hex_color}
    return sorted(badges.values(), key=lambda badge: badge["label"].lower())
