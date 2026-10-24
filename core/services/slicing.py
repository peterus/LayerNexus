"""Slicing helper functions for building slicer arguments and finding machines."""

import json
import logging
from typing import Any

from core.models import (
    OrcaFilamentProfile,
    OrcaMachineProfile,
    OrcaPrintPreset,
)

logger = logging.getLogger(__name__)

__all__: list[str] = [
    "_build_slicer_kwargs",
    "_find_compatible_machine",
]

# OrcaSlicer ``BedType`` enum names, indexed by their numeric value (1-based).
ORCA_BED_TYPES: tuple[str, ...] = (
    "Cool Plate",
    "Engineering Plate",
    "High Temp Plate",
    "Textured PEI Plate",
    "Textured Cool Plate",
    "Supertack Plate",
)
DEFAULT_BED_TYPE = "High Temp Plate"


def _resolve_bed_type(default_bed_type: Any) -> str:
    """Resolve the plate type to slice with, mirroring the OrcaSlicer GUI.

    The OrcaSlicer CLI does not apply the machine's ``default_bed_type`` and
    falls back to "Cool Plate", so first-layer/bed temperatures would come
    from ``cool_plate_temp*``. The GUI instead uses ``default_bed_type`` (a
    name or legacy integer) and otherwise "High Temp Plate" — which is also
    what single-bed printers (no plate selector) slice with.

    Args:
        default_bed_type: The machine profile's ``default_bed_type`` value.

    Returns:
        An OrcaSlicer bed type name.
    """
    value = str(default_bed_type or "").strip()
    if value in ORCA_BED_TYPES:
        return value
    if value.isdigit() and 1 <= int(value) <= len(ORCA_BED_TYPES):
        return ORCA_BED_TYPES[int(value) - 1]
    if value:
        logger.warning(f"Unknown default_bed_type '{value}', using '{DEFAULT_BED_TYPE}'")
    return DEFAULT_BED_TYPE


def _build_slicer_kwargs(
    machine_profile: OrcaMachineProfile | None,
    print_preset: OrcaPrintPreset | None,
    filament_profile: OrcaFilamentProfile | None,
) -> dict[str, Any]:
    """Build keyword arguments for OrcaSlicerAPIClient.slice() from profiles.

    All three profile types use the structured approach -- the full
    flattened settings are reconstructed via ``to_orca_json()`` /
    ``filament_to_orca_json()`` / ``process_to_orca_json()``.

    Args:
        machine_profile: Resolved OrcaMachineProfile instance (or None).
        print_preset: Resolved OrcaPrintPreset instance (or None).
        filament_profile: Resolved OrcaFilamentProfile instance (or None).

    Returns:
        Dictionary with profile arguments for the slicer API.
    """
    from core.services.profile_import import (
        filament_to_orca_json,
        process_to_orca_json,
        to_orca_json,
    )

    kwargs: dict[str, Any] = {}
    logger.debug(
        f"Building slicer kwargs from profiles - Machine: {machine_profile.name if machine_profile else 'None'}, "
        f"Preset: {print_preset.name if print_preset else 'None'}, "
        f"Filament: {filament_profile.name if filament_profile else 'None'}"
    )

    # -- Machine profile (structured approach) ---------------------------------
    if machine_profile:
        try:
            orca_json = to_orca_json(machine_profile)

            # Inject thumbnail settings if not already present.
            # OrcaSlicer system profiles for Klipper set these internally,
            # but they are often missing from exported user-profile JSONs.
            # Without them the slicer won't embed preview images in G-code.
            if "thumbnails" not in orca_json:
                orca_json["thumbnails"] = ["32x32", "400x300"]
                logger.debug("Machine: Injected default thumbnails setting")
            if "thumbnails_format" not in orca_json:
                orca_json["thumbnails_format"] = "PNG"
                logger.debug("Machine: Injected default thumbnails_format=PNG")

            kwargs["printer_profile_json"] = json.dumps(orca_json).encode("utf-8")
            logger.debug(f"Machine: Using reconstructed JSON for '{machine_profile.orca_name}'")

            kwargs["bed_type"] = _resolve_bed_type(orca_json.get("default_bed_type"))
            logger.debug(f"Machine: Using bed type '{kwargs['bed_type']}'")
        except ValueError as exc:
            logger.warning(f"Machine profile not usable: {exc}")

    # -- Filament profile (structured approach) --------------------------------
    if filament_profile:
        try:
            fil_json = filament_to_orca_json(filament_profile)
            kwargs["filament_profile_json"] = json.dumps(fil_json).encode("utf-8")
            logger.debug(f"Filament: Using reconstructed JSON for '{filament_profile.orca_name}'")
        except ValueError as exc:
            logger.warning(f"Filament profile not usable: {exc}")

    # -- Process / print preset (structured approach) --------------------------
    if print_preset:
        try:
            proc_json = process_to_orca_json(print_preset)

            # Ensure the machine profile's *system* name is listed in the
            # process preset's ``compatible_printers``.  The OrcaSlicer
            # CLI reads the machine's ``inherits`` value as the "system
            # printer name" and checks it against this list.  If the
            # user's machine profile inherits from a system profile that
            # is already listed, no injection is needed.  As a safety
            # net we add both the machine's ``inherits_name`` (system
            # name) and the machine's ``orca_name`` (leaf name) so the
            # check passes in every case.
            if machine_profile:
                compat = proc_json.get("compatible_printers")
                if isinstance(compat, list):
                    names_to_add = []
                    if machine_profile.inherits_name and machine_profile.inherits_name not in compat:
                        names_to_add.append(machine_profile.inherits_name)
                    if machine_profile.orca_name not in compat:
                        names_to_add.append(machine_profile.orca_name)
                    if names_to_add:
                        proc_json["compatible_printers"] = compat + names_to_add
                        logger.debug(f"Process: Added {names_to_add} to compatible_printers")

            kwargs["preset_profile_json"] = json.dumps(proc_json).encode("utf-8")
            logger.debug(f"Process: Using reconstructed JSON for '{print_preset.orca_name}'")
        except ValueError as exc:
            logger.warning(f"Process profile not usable: {exc}")

    logger.debug(f"Final slicer kwargs: {list(kwargs.keys())}")

    return kwargs


def _find_compatible_machine(
    print_preset: OrcaPrintPreset,
) -> OrcaMachineProfile | None:
    """Find a resolved OrcaMachineProfile compatible with the given preset.

    Checks the preset's ``compatible_printers`` list against machine
    profiles (by ``orca_name`` or ``inherits_name``).  Falls back to
    the first available resolved machine profile.

    Args:
        print_preset: The OrcaPrintPreset to match against.

    Returns:
        A compatible OrcaMachineProfile, or None if none found.
    """
    # Try to match via compatible_printers in the preset's settings
    compat_printers = print_preset.settings.get("compatible_printers", [])

    if compat_printers:
        # Try exact orca_name match first
        machine = OrcaMachineProfile.objects.filter(
            state=OrcaMachineProfile.STATE_RESOLVED,
            instantiation=True,
            orca_name__in=compat_printers,
        ).first()
        if machine:
            return machine

        # Try inherits_name match (system profile name)
        machine = OrcaMachineProfile.objects.filter(
            state=OrcaMachineProfile.STATE_RESOLVED,
            instantiation=True,
            inherits_name__in=compat_printers,
        ).first()
        if machine:
            return machine

    # Fallback: any resolved machine profile
    return OrcaMachineProfile.objects.filter(
        state=OrcaMachineProfile.STATE_RESOLVED,
        instantiation=True,
    ).first()
