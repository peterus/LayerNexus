"""OrcaSlicer profile import and inheritance resolution service.

Handles importing hierarchical OrcaSlicer JSON profiles into the database.
Profiles may use an ``inherits`` field referencing a parent profile; this
service resolves the full inheritance chain and flattens the result into
dedicated DB columns plus a catch-all ``extra_settings`` JSONField.

OrcaSlicer stores **all** values as JSON strings (even numbers and booleans).
This service parses them into native Python types for the DB columns.

Supports **machine** (printer), **filament**, and **process** (print preset)
profile types.
"""

import logging
from dataclasses import dataclass
from typing import Any

from django.contrib.auth.models import User
from django.db import models
from django.db.models import Q

from core.models import OrcaFilamentProfile, OrcaMachineProfile, OrcaPrintPreset

logger = logging.getLogger(__name__)

# ── Metadata fields (excluded from settings) ──────────────────────────────

METADATA_FIELDS: set[str] = {
    "type",
    "name",
    "inherits",
    "from",
    "setting_id",
    "instantiation",
    "description",
    "version",
    "printer_settings_id",
    "filament_settings_id",
    "print_settings_id",
    "compatible_printers",
    "compatible_printers_condition",
    "compatible_prints",
    "compatible_prints_condition",
    "is_custom_defined",
    "renamed_from",
}

# ── Machine profile: DB column ↔ OrcaSlicer JSON key mapping ─────────────
#
# Each entry maps a Django model field name to its OrcaSlicer JSON key and
# the target Python type used for parsing.
#
# Supported type tags:
#   "float", "int", "bool", "str"          – scalar
#   "float_list", "int_list", "bool_list", "str_list"  – JSON arrays

MACHINE_FIELD_MAP: dict[str, tuple[str, str]] = {
    # (model_field, (orca_json_key, type_tag))
    "nozzle_diameter": ("nozzle_diameter", "float_list"),
    "printable_area": ("printable_area", "str_list"),
    "printable_height": ("printable_height", "float"),
    "bed_shape": ("bed_shape", "str"),
    "extruders_count": ("extruders_count", "int"),
    "gcode_flavor": ("gcode_flavor", "str"),
    "printer_structure": ("printer_structure", "str"),
    "printer_technology": ("printer_technology", "str"),
    "printer_model": ("printer_model", "str"),
    "printer_variant": ("printer_variant", "str"),
    "machine_max_speed_x": ("machine_max_speed_x", "int"),
    "machine_max_speed_y": ("machine_max_speed_y", "int"),
    "machine_max_speed_z": ("machine_max_speed_z", "int"),
    "machine_max_acceleration_x": ("machine_max_acceleration_x", "int"),
    "machine_max_acceleration_y": ("machine_max_acceleration_y", "int"),
    "machine_max_acceleration_z": ("machine_max_acceleration_z", "int"),
    "retraction_length": ("retraction_length", "float_list"),
    "retraction_speed": ("retraction_speed", "float_list"),
    "z_hop": ("z_hop", "float_list"),
    "machine_start_gcode": ("machine_start_gcode", "str"),
    "machine_end_gcode": ("machine_end_gcode", "str"),
    "default_bed_type": ("default_bed_type", "str"),
    "default_filament_profile": ("default_filament_profile", "str_list"),
    "default_print_profile": ("default_print_profile", "str"),
    "single_extruder_multi_material": ("single_extruder_multi_material", "bool"),
    "use_relative_e_distances": ("use_relative_e_distances", "bool"),
    "use_firmware_retraction": ("use_firmware_retraction", "bool"),
}

# Reverse lookup: OrcaSlicer JSON key → model field name
_MACHINE_ORCA_KEY_TO_FIELD: dict[str, str] = {
    orca_key: field_name for field_name, (orca_key, _) in MACHINE_FIELD_MAP.items()
}

# ── Filament profile: DB column ↔ OrcaSlicer JSON key mapping ────────────

FILAMENT_FIELD_MAP: dict[str, tuple[str, str]] = {
    # Material identification
    "filament_type": ("filament_type", "str_list"),
    "filament_vendor": ("filament_vendor", "str_list"),
    "filament_density": ("filament_density", "float_list"),
    "filament_diameter": ("filament_diameter", "float_list"),
    "filament_cost": ("filament_cost", "float_list"),
    "filament_flow_ratio": ("filament_flow_ratio", "float_list"),
    "filament_max_volumetric_speed": ("filament_max_volumetric_speed", "float_list"),
    # Nozzle temperatures
    "nozzle_temperature": ("nozzle_temperature", "int_list"),
    "nozzle_temperature_initial_layer": (
        "nozzle_temperature_initial_layer",
        "int_list",
    ),
    "nozzle_temperature_range_low": ("nozzle_temperature_range_low", "int_list"),
    "nozzle_temperature_range_high": ("nozzle_temperature_range_high", "int_list"),
    # Bed temperatures
    "bed_temperature": ("bed_temperature", "int_list"),
    "bed_temperature_initial_layer": ("bed_temperature_initial_layer", "int_list"),
    "hot_plate_temp": ("hot_plate_temp", "int_list"),
    "hot_plate_temp_initial_layer": ("hot_plate_temp_initial_layer", "int_list"),
    "cool_plate_temp": ("cool_plate_temp", "int_list"),
    "cool_plate_temp_initial_layer": ("cool_plate_temp_initial_layer", "int_list"),
    "temperature_vitrification": ("temperature_vitrification", "int_list"),
    # Cooling / fan
    "fan_min_speed": ("fan_min_speed", "float_list"),
    "fan_max_speed": ("fan_max_speed", "float_list"),
    "overhang_fan_speed": ("overhang_fan_speed", "int_list"),
    "close_fan_the_first_x_layers": ("close_fan_the_first_x_layers", "int_list"),
    # Pressure advance
    "pressure_advance": ("pressure_advance", "float_list"),
    "enable_pressure_advance": ("enable_pressure_advance", "bool_list"),
    # G-code
    "filament_start_gcode": ("filament_start_gcode", "str_list"),
    "filament_end_gcode": ("filament_end_gcode", "str_list"),
    # Material properties
    "filament_soluble": ("filament_soluble", "bool_list"),
    "filament_is_support": ("filament_is_support", "bool_list"),
}

_FILAMENT_ORCA_KEY_TO_FIELD: dict[str, str] = {
    orca_key: field_name for field_name, (orca_key, _) in FILAMENT_FIELD_MAP.items()
}

# ── Process (print preset) profile: DB column ↔ OrcaSlicer JSON key mapping

PROCESS_FIELD_MAP: dict[str, tuple[str, str]] = {
    # Layer height
    "layer_height": ("layer_height", "float"),
    "initial_layer_print_height": ("initial_layer_print_height", "float"),
    # Line widths (can be float or percent string — store as str)
    "line_width": ("line_width", "str"),
    "outer_wall_line_width": ("outer_wall_line_width", "str"),
    "inner_wall_line_width": ("inner_wall_line_width", "str"),
    "initial_layer_line_width": ("initial_layer_line_width", "str"),
    "top_surface_line_width": ("top_surface_line_width", "str"),
    "sparse_infill_line_width": ("sparse_infill_line_width", "str"),
    # Walls
    "wall_loops": ("wall_loops", "int"),
    # Shell layers
    "top_shell_layers": ("top_shell_layers", "int"),
    "bottom_shell_layers": ("bottom_shell_layers", "int"),
    # Infill
    "sparse_infill_density": ("sparse_infill_density", "str"),
    "sparse_infill_pattern": ("sparse_infill_pattern", "str"),
    "top_surface_pattern": ("top_surface_pattern", "str"),
    # Speeds
    "outer_wall_speed": ("outer_wall_speed", "float"),
    "inner_wall_speed": ("inner_wall_speed", "float"),
    "sparse_infill_speed": ("sparse_infill_speed", "float"),
    "internal_solid_infill_speed": ("internal_solid_infill_speed", "float"),
    "top_surface_speed": ("top_surface_speed", "float"),
    "travel_speed": ("travel_speed", "float"),
    "bridge_speed": ("bridge_speed", "float"),
    "gap_infill_speed": ("gap_infill_speed", "float"),
    "initial_layer_speed": ("initial_layer_speed", "float"),
    # Acceleration
    "default_acceleration": ("default_acceleration", "float"),
    "outer_wall_acceleration": ("outer_wall_acceleration", "float"),
    "inner_wall_acceleration": ("inner_wall_acceleration", "float"),
    "travel_acceleration": ("travel_acceleration", "float"),
    "initial_layer_acceleration": ("initial_layer_acceleration", "float"),
    # Support
    "enable_support": ("enable_support", "bool"),
    "support_type": ("support_type", "str"),
    "support_threshold_angle": ("support_threshold_angle", "int"),
    "support_style": ("support_style", "str"),
    # Brim / adhesion
    "brim_type": ("brim_type", "str"),
    "brim_width": ("brim_width", "float"),
    # Seam
    "seam_position": ("seam_position", "str"),
    # Quality / detail
    "ironing_type": ("ironing_type", "str"),
    "detect_overhang_wall": ("detect_overhang_wall", "bool"),
    "elefant_foot_compensation": ("elefant_foot_compensation", "float"),
    # Sequence / multi-object
    "print_sequence": ("print_sequence", "str"),
    "enable_prime_tower": ("enable_prime_tower", "bool"),
    # Filename
    "filename_format": ("filename_format", "str"),
}

_PROCESS_ORCA_KEY_TO_FIELD: dict[str, str] = {
    orca_key: field_name for field_name, (orca_key, _) in PROCESS_FIELD_MAP.items()
}


# ── Value parsing helpers ────────────────────────────────────────────────


def _parse_value(value: Any, type_tag: str) -> Any:
    """Parse an OrcaSlicer value (often a string) into a native Python type.

    Args:
        value: The raw value from the JSON file.
        type_tag: One of 'float', 'int', 'bool', 'str', 'float_list', 'str_list'.

    Returns:
        The parsed value, or None if parsing fails.
    """
    if value is None:
        return None

    try:
        if type_tag == "float":
            return float(str(value))
        elif type_tag == "int":
            return int(float(str(value)))  # int(float()) handles "200.0"
        elif type_tag == "bool":
            return str(value).lower() in ("1", "true")
        elif type_tag == "str":
            return str(value)
        elif type_tag == "float_list":
            if isinstance(value, list):
                return [float(str(v)) for v in value]
            return [float(str(value))]
        elif type_tag == "int_list":
            if isinstance(value, list):
                return [int(float(str(v))) for v in value]
            return [int(float(str(value)))]
        elif type_tag == "bool_list":
            if isinstance(value, list):
                return [str(v).lower() in ("1", "true") for v in value]
            return [str(value).lower() in ("1", "true")]
        elif type_tag == "str_list":
            if isinstance(value, list):
                return [str(v) for v in value]
            return [str(value)]
    except (ValueError, TypeError) as exc:
        logger.warning(f"Failed to parse value {value!r} as {type_tag}: {exc}")
        return None

    return value


def _to_orca_string(value: Any) -> Any:
    """Convert a native Python value back to OrcaSlicer string format.

    Args:
        value: The Python-typed value.

    Returns:
        The OrcaSlicer-compatible string representation.
    """
    if isinstance(value, bool):
        return "1" if value else "0"
    elif isinstance(value, float):
        # 0.4 → "0.4", 200.0 → "200"
        s = f"{value:g}"
        return s
    elif isinstance(value, int):
        return str(value)
    elif isinstance(value, list):
        return [_to_orca_string(v) for v in value]
    return value  # strings stay as-is


def _rebuild_full_json_generic(
    profile: models.Model,
    model_class: type[models.Model],
) -> dict[str, Any]:
    """Rebuild complete OrcaSlicer JSON from ``uploaded_json`` up the parent chain.

    Unlike ``_get_resolved_settings_generic`` (which reconstructs from parsed
    DB columns), this function walks the inheritance chain and merges the
    **original** JSON values.  This preserves the exact value types and
    formats that OrcaSlicer expects (arrays of strings, percent strings,
    multi-value arrays like ``["500", "200"]``, etc.).

    Args:
        profile: A resolved profile instance.
        model_class: The Django model class (e.g. ``OrcaMachineProfile``).

    Returns:
        Flat dictionary with all settings and metadata in OrcaSlicer format.
    """
    # Map model class → OrcaSlicer type string (fallback for missing 'type')
    _model_type_map: dict[type, str] = {
        OrcaMachineProfile: "machine",
        OrcaFilamentProfile: "filament",
        OrcaPrintPreset: "process",
    }

    # Self-inheriting or base profile → uploaded_json has everything.
    # These are typically system presets – set ``from`` to ``"system"``
    # if not already present so the CLI recognises them correctly.
    if not profile.inherits_name or profile.inherits_name == profile.orca_name:
        result = dict(profile.uploaded_json)
        result.pop("inherits", None)
        result.setdefault("from", "system")
        result.setdefault("type", _model_type_map.get(model_class, ""))
        return result

    # Build chain from leaf (this profile) to root
    chain: list[dict[str, Any]] = [profile.uploaded_json]
    visited: set[str] = {profile.orca_name}
    current = profile

    while current.inherits_name and current.inherits_name not in visited:
        parent = _find_parent_by_name(model_class, current.inherits_name, current.created_by)
        if parent is None or not parent.is_resolved:
            break
        visited.add(parent.orca_name)
        chain.append(parent.uploaded_json)
        current = parent

    # Merge **all** keys from root (last in chain) to leaf (first).
    # Each layer overrides its parent, producing a complete flat profile.
    # This includes metadata fields like ``description``, ``setting_id``,
    # ``compatible_printers``, etc. which OrcaSlicer expects to find.
    chain.reverse()
    merged: dict[str, Any] = {}
    for layer_json in chain:
        for key, value in layer_json.items():
            merged[key] = value

    # Ensure the name matches our stored orca_name
    merged["name"] = profile.orca_name

    # Keep ``inherits`` pointing to the leaf profile's **direct** parent.
    # The OrcaSlicer CLI reads this value to determine the "system preset
    # name" of the printer (``new_printer_system_name``) or the process
    # (``new_process_system_name``).  When ``from`` is ``"user"``, the
    # CLI does:
    #   new_printer_system_name = config["inherits"]
    # and then checks:
    #   new_printer_system_name ∈ process["compatible_printers"]
    # Without ``inherits`` the system name is empty and the check always
    # fails with ``CLI_PROCESS_NOT_COMPATIBLE``.
    merged["inherits"] = profile.inherits_name

    # Profiles with a parent are user profiles – ensure ``from`` is set
    # so the CLI validation (``from`` must be "system"/"User"/"user")
    # does not reject the file.
    merged.setdefault("from", "user")

    # Ensure 'type' is always present.  Child profile JSONs often omit it
    # because OrcaSlicer infers the type from the parent.  We walk the
    # chain to find it, or fall back to the model class name.
    if "type" not in merged:
        merged["type"] = _model_type_map.get(model_class, "")

    return merged


# ── Import result ────────────────────────────────────────────────────────


@dataclass
class ImportResult:
    """Result returned after importing a profile JSON file."""

    profile: Any  # OrcaMachineProfile | OrcaFilamentProfile
    is_resolved: bool
    missing_parent: str | None = None
    auto_resolved_children: list[str] | None = None

    @property
    def message(self) -> str:
        """Human-readable status message."""
        if self.is_resolved:
            children = ""
            if self.auto_resolved_children:
                names = ", ".join(self.auto_resolved_children)
                children = f" Additionally auto-resolved: {names}."
            return f"Profile '{self.profile.name}' imported and resolved.{children}"
        return (
            f"Profile '{self.profile.name}' saved as pending. "
            f"Please upload the parent profile '{self.missing_parent}' next."
        )


# ── Generic resolution helpers ───────────────────────────────────────────
#
# These internal functions are parameterised by ``model_class`` and
# ``field_map`` so they work for both machine and filament profiles.


def _find_parent_by_name(
    model_class: type[models.Model],
    parent_name: str,
    user: User,
) -> models.Model | None:
    """Look up a parent profile by ``orca_name`` or ``renamed_from``.

    First tries an exact match on ``orca_name``.  If that fails, looks for a
    profile whose ``renamed_from`` equals *parent_name* (handles OrcaSlicer
    profile renames).

    Args:
        model_class: The Django model class (e.g. OrcaMachineProfile).
        parent_name: The name to search for.
        user: Owner of the profile.

    Returns:
        The matching profile instance, or *None* if no profile matches.
    """
    try:
        return model_class.objects.get(orca_name=parent_name, created_by=user)
    except model_class.DoesNotExist:
        pass

    # Fallback: check renamed_from (profile was renamed in OrcaSlicer)
    return (
        model_class.objects.filter(
            renamed_from=parent_name,
            created_by=user,
        )
        .exclude(renamed_from="")
        .first()
    )


def _try_resolve_generic(
    profile: models.Model,
    model_class: type[models.Model],
    field_map: dict[str, tuple[str, str]],
    orca_key_to_field: dict[str, str],
    profile_type_label: str,
) -> ImportResult:
    """Attempt to resolve a profile's inheritance chain (generic).

    Args:
        profile: The profile instance to resolve.
        model_class: The Django model class (e.g. OrcaMachineProfile).
        field_map: The field map for this profile type.
        orca_key_to_field: Reverse lookup from OrcaSlicer key to model field.
        profile_type_label: Human-readable label (e.g. 'machine', 'filament').

    Returns:
        ImportResult with resolution status.
    """
    if not profile.inherits_name:
        _resolve_profile_generic(
            profile,
            profile.uploaded_json,
            field_map,
            orca_key_to_field,
            profile_type_label,
        )
        children = _auto_resolve_children_generic(
            profile, model_class, field_map, orca_key_to_field, profile_type_label
        )
        return ImportResult(
            profile=profile,
            is_resolved=True,
            auto_resolved_children=children,
        )

    # Has parent — check if it exists and is resolved
    parent = _find_parent_by_name(model_class, profile.inherits_name, profile.created_by)
    if parent is None:
        profile.state = model_class.STATE_PENDING
        profile.save()
        logger.info(f"Profile '{profile.orca_name}' is pending — parent '{profile.inherits_name}' not found.")
        return ImportResult(
            profile=profile,
            is_resolved=False,
            missing_parent=profile.inherits_name,
        )

    if parent.state != model_class.STATE_RESOLVED:
        profile.state = model_class.STATE_PENDING
        profile.save()
        logger.info(f"Profile '{profile.orca_name}' is pending — parent '{profile.inherits_name}' is not yet resolved.")
        return ImportResult(
            profile=profile,
            is_resolved=False,
            missing_parent=profile.inherits_name,
        )

    # Parent is resolved — merge and resolve
    merged = _merge_with_parent_generic(parent, profile.uploaded_json, field_map)
    _resolve_profile_generic(profile, merged, field_map, orca_key_to_field, profile_type_label)
    children = _auto_resolve_children_generic(profile, model_class, field_map, orca_key_to_field, profile_type_label)
    return ImportResult(
        profile=profile,
        is_resolved=True,
        auto_resolved_children=children,
    )


def _merge_with_parent_generic(
    parent: models.Model,
    child_json: dict[str, Any],
    field_map: dict[str, tuple[str, str]],
) -> dict[str, Any]:
    """Merge a child profile JSON on top of a resolved parent's settings.

    Args:
        parent: The resolved parent profile.
        child_json: Raw JSON from the child profile file.
        field_map: The field map for this profile type.

    Returns:
        Flat merged dictionary of all settings.
    """
    merged = _get_resolved_settings_generic(parent, field_map)

    for key, value in child_json.items():
        if key not in METADATA_FIELDS:
            merged[key] = value

    return merged


def _resolve_profile_generic(
    profile: models.Model,
    resolved_data: dict[str, Any],
    field_map: dict[str, tuple[str, str]],
    orca_key_to_field: dict[str, str],
    profile_type_label: str,
) -> None:
    """Extract settings from resolved data into DB columns (generic).

    Args:
        profile: The profile instance to populate.
        resolved_data: Flat dictionary of all resolved settings.
        field_map: The field map for this profile type.
        orca_key_to_field: Reverse lookup from OrcaSlicer key to model field.
        profile_type_label: Human-readable label for logging.
    """
    extra: dict[str, Any] = {}

    for key, value in resolved_data.items():
        if key in METADATA_FIELDS:
            continue

        if key in orca_key_to_field:
            field_name = orca_key_to_field[key]
            _, type_tag = field_map[field_name]
            parsed = _parse_value(value, type_tag)
            if parsed is not None:
                setattr(profile, field_name, parsed)
        else:
            extra[key] = value

    profile.extra_settings = extra
    profile.state = profile.__class__.STATE_RESOLVED
    profile.save()

    logger.info(
        f"Resolved {profile_type_label} profile '{profile.orca_name}' — "
        f"{len(field_map) - len(extra)} columns + "
        f"{len(extra)} extra settings."
    )


def _auto_resolve_children_generic(
    profile: models.Model,
    model_class: type[models.Model],
    field_map: dict[str, tuple[str, str]],
    orca_key_to_field: dict[str, str],
    profile_type_label: str,
) -> list[str]:
    """Auto-resolve pending profiles that inherit from the given profile.

    Args:
        profile: The newly resolved parent profile.
        model_class: The Django model class.
        field_map: The field map for this profile type.
        orca_key_to_field: Reverse lookup from OrcaSlicer key to model field.
        profile_type_label: Human-readable label for logging.

    Returns:
        List of orca_names that were auto-resolved.
    """
    resolved_names: list[str] = []
    name_q = Q(inherits_name=profile.orca_name)
    if profile.renamed_from:
        name_q |= Q(inherits_name=profile.renamed_from)
    pending = model_class.objects.filter(
        name_q,
        created_by=profile.created_by,
        state=model_class.STATE_PENDING,
    )

    for child in pending:
        try:
            merged = _merge_with_parent_generic(profile, child.uploaded_json, field_map)
            _resolve_profile_generic(child, merged, field_map, orca_key_to_field, profile_type_label)
            resolved_names.append(child.orca_name)
            logger.info(f"Auto-resolved child {profile_type_label} profile '{child.orca_name}'")

            grandchildren = _auto_resolve_children_generic(
                child, model_class, field_map, orca_key_to_field, profile_type_label
            )
            resolved_names.extend(grandchildren)
        except Exception as exc:
            logger.error(f"Failed to auto-resolve child '{child.orca_name}': {exc}")

    return resolved_names


def _get_resolved_settings_generic(
    profile: models.Model,
    field_map: dict[str, tuple[str, str]],
) -> dict[str, Any]:
    """Reconstruct the full resolved settings dict (generic).

    Args:
        profile: A resolved profile instance.
        field_map: The field map for this profile type.

    Returns:
        Flat dictionary with all OrcaSlicer settings.
    """
    settings: dict[str, Any] = {}

    for field_name, (orca_key, _type_tag) in field_map.items():
        value = getattr(profile, field_name)
        if value is not None and value != "" and value != []:
            settings[orca_key] = value

    settings.update(profile.extra_settings)
    return settings


def _get_missing_parent_chain_generic(
    profile: models.Model,
    model_class: type[models.Model],
) -> list[str]:
    """Walk the inheritance chain and return names of missing parents.

    Args:
        profile: A pending profile instance.
        model_class: The Django model class.

    Returns:
        List of missing parent profile names.
    """
    missing: list[str] = []
    current_name = profile.inherits_name

    while current_name:
        parent = _find_parent_by_name(model_class, current_name, profile.created_by)
        if parent is None:
            missing.append(current_name)
            break
        if parent.is_resolved:
            break
        current_name = parent.inherits_name

    return missing


# ══════════════════════════════════════════════════════════════════════════
# Machine profile public API
# ══════════════════════════════════════════════════════════════════════════


def import_machine_profile_json(
    json_data: dict[str, Any],
    user: User,
    display_name: str | None = None,
) -> ImportResult:
    """Import a single OrcaSlicer machine profile JSON into the database.

    Args:
        json_data: Parsed JSON dictionary from the uploaded file.
        user: The Django user performing the import.
        display_name: Optional display name override.

    Returns:
        ImportResult with the created/updated profile and status.

    Raises:
        ValueError: If the JSON is missing required fields.
    """
    orca_name = json_data.get("name", "").strip()
    if not orca_name:
        raise ValueError("Profile JSON is missing the required 'name' field.")

    profile_type = json_data.get("type", "").strip()
    if profile_type and profile_type != "machine":
        raise ValueError(
            f"Expected profile type 'machine', got '{profile_type}'. "
            f"This does not appear to be a machine/printer profile."
        )

    inherits_name = json_data.get("inherits", "").strip()
    setting_id = json_data.get("setting_id", "").strip()
    description = json_data.get("description", "").strip()
    renamed_from = json_data.get("renamed_from", "").strip()
    instantiation_raw = json_data.get("instantiation", "true")
    instantiation = str(instantiation_raw).lower() in ("true", "1")

    profile, created = OrcaMachineProfile.objects.update_or_create(
        orca_name=orca_name,
        created_by=user,
        defaults={
            "name": display_name or orca_name,
            "description": description,
            "inherits_name": inherits_name,
            "setting_id": setting_id,
            "instantiation": instantiation,
            "renamed_from": renamed_from,
            "uploaded_json": json_data,
        },
    )

    action = "Created" if created else "Updated"
    logger.info(f"{action} machine profile '{orca_name}' (inherits='{inherits_name}')")

    return _try_resolve_generic(
        profile,
        OrcaMachineProfile,
        MACHINE_FIELD_MAP,
        _MACHINE_ORCA_KEY_TO_FIELD,
        "machine",
    )


def get_resolved_settings(profile: OrcaMachineProfile) -> dict[str, Any]:
    """Reconstruct the full resolved settings dict for a machine profile.

    Args:
        profile: A resolved OrcaMachineProfile instance.

    Returns:
        Flat dictionary with all OrcaSlicer settings.
    """
    return _get_resolved_settings_generic(profile, MACHINE_FIELD_MAP)


def to_orca_json(profile: OrcaMachineProfile) -> dict[str, Any]:
    """Reconstruct the OrcaSlicer-compatible JSON for a machine profile.

    Rebuilds the full merged JSON from the ``uploaded_json`` inheritance
    chain, preserving original value formats (arrays, percent strings,
    etc.) exactly as OrcaSlicer expects.

    Args:
        profile: A resolved OrcaMachineProfile instance.

    Returns:
        Dictionary ready to be serialized as JSON for the OrcaSlicer API.

    Raises:
        ValueError: If the profile is not yet resolved.
    """
    if not profile.is_resolved:
        raise ValueError(f"Profile '{profile.orca_name}' is not yet resolved — cannot generate OrcaSlicer JSON.")

    return _rebuild_full_json_generic(profile, OrcaMachineProfile)


def get_missing_parent_chain(profile: OrcaMachineProfile) -> list[str]:
    """Walk the inheritance chain and return names of missing machine parents.

    Args:
        profile: A pending OrcaMachineProfile.

    Returns:
        List of missing parent profile names.
    """
    return _get_missing_parent_chain_generic(profile, OrcaMachineProfile)


# ══════════════════════════════════════════════════════════════════════════
# Filament profile public API
# ══════════════════════════════════════════════════════════════════════════


def import_filament_profile_json(
    json_data: dict[str, Any],
    user: User,
    display_name: str | None = None,
) -> ImportResult:
    """Import a single OrcaSlicer filament profile JSON into the database.

    Args:
        json_data: Parsed JSON dictionary from the uploaded file.
        user: The Django user performing the import.
        display_name: Optional display name override.

    Returns:
        ImportResult with the created/updated profile and status.

    Raises:
        ValueError: If the JSON is missing required fields.
    """
    orca_name = json_data.get("name", "").strip()
    if not orca_name:
        raise ValueError("Profile JSON is missing the required 'name' field.")

    profile_type = json_data.get("type", "").strip()
    if profile_type and profile_type != "filament":
        raise ValueError(
            f"Expected profile type 'filament', got '{profile_type}'. This does not appear to be a filament profile."
        )

    inherits_name = json_data.get("inherits", "").strip()
    setting_id = json_data.get("setting_id", "").strip()
    description = json_data.get("description", "").strip()
    renamed_from = json_data.get("renamed_from", "").strip()
    instantiation_raw = json_data.get("instantiation", "true")
    instantiation = str(instantiation_raw).lower() in ("true", "1")

    profile, created = OrcaFilamentProfile.objects.update_or_create(
        orca_name=orca_name,
        created_by=user,
        defaults={
            "name": display_name or orca_name,
            "description": description,
            "inherits_name": inherits_name,
            "setting_id": setting_id,
            "instantiation": instantiation,
            "renamed_from": renamed_from,
            "uploaded_json": json_data,
        },
    )

    action = "Created" if created else "Updated"
    logger.info(f"{action} filament profile '{orca_name}' (inherits='{inherits_name}')")

    return _try_resolve_generic(
        profile,
        OrcaFilamentProfile,
        FILAMENT_FIELD_MAP,
        _FILAMENT_ORCA_KEY_TO_FIELD,
        "filament",
    )


def get_filament_resolved_settings(profile: OrcaFilamentProfile) -> dict[str, Any]:
    """Reconstruct the full resolved settings dict for a filament profile.

    Args:
        profile: A resolved OrcaFilamentProfile instance.

    Returns:
        Flat dictionary with all OrcaSlicer filament settings.
    """
    return _get_resolved_settings_generic(profile, FILAMENT_FIELD_MAP)


def filament_to_orca_json(profile: OrcaFilamentProfile) -> dict[str, Any]:
    """Reconstruct the OrcaSlicer-compatible JSON for a filament profile.

    Rebuilds the full merged JSON from the ``uploaded_json`` inheritance
    chain, preserving original value formats (arrays of strings, etc.)
    exactly as OrcaSlicer expects.

    Args:
        profile: A resolved OrcaFilamentProfile instance.

    Returns:
        Dictionary ready to be serialized as JSON for the OrcaSlicer API.

    Raises:
        ValueError: If the profile is not yet resolved.
    """
    if not profile.is_resolved:
        raise ValueError(f"Profile '{profile.orca_name}' is not yet resolved — cannot generate OrcaSlicer JSON.")

    return _rebuild_full_json_generic(profile, OrcaFilamentProfile)


def get_filament_missing_parent_chain(profile: OrcaFilamentProfile) -> list[str]:
    """Walk the inheritance chain and return names of missing filament parents.

    Args:
        profile: A pending OrcaFilamentProfile.

    Returns:
        List of missing parent profile names.
    """
    return _get_missing_parent_chain_generic(profile, OrcaFilamentProfile)


# ══════════════════════════════════════════════════════════════════════════
# Process (print preset) profile public API
# ══════════════════════════════════════════════════════════════════════════


def import_process_profile_json(
    json_data: dict[str, Any],
    user: User,
    display_name: str | None = None,
) -> ImportResult:
    """Import a single OrcaSlicer process profile JSON into the database.

    Args:
        json_data: Parsed JSON dictionary from the uploaded file.
        user: The Django user performing the import.
        display_name: Optional display name override.

    Returns:
        ImportResult with the created/updated profile and status.

    Raises:
        ValueError: If the JSON is missing required fields.
    """
    orca_name = json_data.get("name", "").strip()
    if not orca_name:
        raise ValueError("Profile JSON is missing the required 'name' field.")

    profile_type = json_data.get("type", "").strip()
    if profile_type and profile_type != "process":
        raise ValueError(
            f"Expected profile type 'process', got '{profile_type}'. "
            f"This does not appear to be a process/print preset profile."
        )

    inherits_name = json_data.get("inherits", "").strip()
    setting_id = json_data.get("setting_id", "").strip()
    description = json_data.get("description", "").strip()
    renamed_from = json_data.get("renamed_from", "").strip()
    instantiation_raw = json_data.get("instantiation", "true")
    instantiation = str(instantiation_raw).lower() in ("true", "1")

    profile, created = OrcaPrintPreset.objects.update_or_create(
        orca_name=orca_name,
        created_by=user,
        defaults={
            "name": display_name or orca_name,
            "description": description,
            "inherits_name": inherits_name,
            "setting_id": setting_id,
            "instantiation": instantiation,
            "renamed_from": renamed_from,
            "uploaded_json": json_data,
        },
    )

    action = "Created" if created else "Updated"
    logger.info(f"{action} process profile '{orca_name}' (inherits='{inherits_name}')")

    return _try_resolve_generic(
        profile,
        OrcaPrintPreset,
        PROCESS_FIELD_MAP,
        _PROCESS_ORCA_KEY_TO_FIELD,
        "process",
    )


def get_process_resolved_settings(profile: OrcaPrintPreset) -> dict[str, Any]:
    """Reconstruct the full resolved settings dict for a process profile.

    Args:
        profile: A resolved OrcaPrintPreset instance.

    Returns:
        Flat dictionary with all OrcaSlicer process settings.
    """
    return _get_resolved_settings_generic(profile, PROCESS_FIELD_MAP)


def process_to_orca_json(profile: OrcaPrintPreset) -> dict[str, Any]:
    """Reconstruct the OrcaSlicer-compatible JSON for a process profile.

    Rebuilds the full merged JSON from the ``uploaded_json`` inheritance
    chain, preserving original value formats exactly as OrcaSlicer expects.

    Args:
        profile: A resolved OrcaPrintPreset instance.

    Returns:
        Dictionary ready to be serialized as JSON for the OrcaSlicer API.

    Raises:
        ValueError: If the profile is not yet resolved.
    """
    if not profile.is_resolved:
        raise ValueError(f"Profile '{profile.orca_name}' is not yet resolved — cannot generate OrcaSlicer JSON.")

    return _rebuild_full_json_generic(profile, OrcaPrintPreset)


def get_process_missing_parent_chain(profile: OrcaPrintPreset) -> list[str]:
    """Walk the inheritance chain and return names of missing process parents.

    Args:
        profile: A pending OrcaPrintPreset.

    Returns:
        List of missing parent profile names.
    """
    return _get_missing_parent_chain_generic(profile, OrcaPrintPreset)
