"""Data migration: copy individual field values into the settings JSONField."""

from django.db import migrations


MACHINE_FIELDS = [
    "nozzle_diameter",
    "printable_area",
    "printable_height",
    "bed_shape",
    "extruders_count",
    "gcode_flavor",
    "printer_structure",
    "printer_technology",
    "printer_model",
    "printer_variant",
    "machine_max_speed_x",
    "machine_max_speed_y",
    "machine_max_speed_z",
    "machine_max_acceleration_x",
    "machine_max_acceleration_y",
    "machine_max_acceleration_z",
    "retraction_length",
    "retraction_speed",
    "z_hop",
    "machine_start_gcode",
    "machine_end_gcode",
    "default_bed_type",
    "default_filament_profile",
    "default_print_profile",
    "single_extruder_multi_material",
    "use_relative_e_distances",
    "use_firmware_retraction",
]

FILAMENT_FIELDS = [
    "filament_type",
    "filament_vendor",
    "filament_density",
    "filament_diameter",
    "filament_cost",
    "filament_flow_ratio",
    "filament_max_volumetric_speed",
    "nozzle_temperature",
    "nozzle_temperature_initial_layer",
    "nozzle_temperature_range_low",
    "nozzle_temperature_range_high",
    "bed_temperature",
    "bed_temperature_initial_layer",
    "hot_plate_temp",
    "hot_plate_temp_initial_layer",
    "cool_plate_temp",
    "cool_plate_temp_initial_layer",
    "temperature_vitrification",
    "fan_min_speed",
    "fan_max_speed",
    "overhang_fan_speed",
    "close_fan_the_first_x_layers",
    "pressure_advance",
    "enable_pressure_advance",
    "filament_start_gcode",
    "filament_end_gcode",
    "filament_soluble",
    "filament_is_support",
]

PROCESS_FIELDS = [
    "layer_height",
    "initial_layer_print_height",
    "line_width",
    "outer_wall_line_width",
    "inner_wall_line_width",
    "initial_layer_line_width",
    "top_surface_line_width",
    "sparse_infill_line_width",
    "wall_loops",
    "top_shell_layers",
    "bottom_shell_layers",
    "sparse_infill_density",
    "sparse_infill_pattern",
    "top_surface_pattern",
    "outer_wall_speed",
    "inner_wall_speed",
    "sparse_infill_speed",
    "internal_solid_infill_speed",
    "top_surface_speed",
    "travel_speed",
    "bridge_speed",
    "gap_infill_speed",
    "initial_layer_speed",
    "default_acceleration",
    "outer_wall_acceleration",
    "inner_wall_acceleration",
    "travel_acceleration",
    "initial_layer_acceleration",
    "enable_support",
    "support_type",
    "support_threshold_angle",
    "support_style",
    "brim_type",
    "brim_width",
    "seam_position",
    "ironing_type",
    "detect_overhang_wall",
    "elefant_foot_compensation",
    "print_sequence",
    "enable_prime_tower",
    "filename_format",
]


def _migrate_settings(apps, schema_editor, model_name, field_names):
    """Copy individual field values into the settings JSONField."""
    Model = apps.get_model("core", model_name)
    for profile in Model.objects.all():
        settings = {}
        for field_name in field_names:
            val = getattr(profile, field_name, None)
            if val is not None and val != "" and val != []:
                settings[field_name] = val
        if profile.extra_settings:
            settings.update(profile.extra_settings)
        profile.settings = settings
        profile.save(update_fields=["settings"])


def forwards(apps, schema_editor):
    """Migrate data from individual columns to settings JSONField."""
    _migrate_settings(apps, schema_editor, "OrcaMachineProfile", MACHINE_FIELDS)
    _migrate_settings(apps, schema_editor, "OrcaFilamentProfile", FILAMENT_FIELDS)
    _migrate_settings(apps, schema_editor, "OrcaPrintPreset", PROCESS_FIELDS)


def backwards(apps, schema_editor):
    """Data migration backwards not needed — old columns still exist at this point."""
    pass


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0011_add_settings_jsonfield"),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
