"""OrcaSlicer profile models (filament, print preset, machine) for the LayerNexus application."""

from __future__ import annotations

from django.conf import settings
from django.db import models


class OrcaFilamentProfile(models.Model):
    """Fully resolved OrcaSlicer filament profile.

    Stores the flattened, inheritance-resolved OrcaSlicer filament profile.
    Key settings are exposed as dedicated DB columns for direct querying,
    while all remaining settings are stored in ``extra_settings`` (JSONField).

    Import workflow:
    1. User uploads a JSON profile file.
    2. If the ``inherits`` field is set and the parent profile does not yet
       exist, the profile is stored as *pending*.
    3. When the parent is uploaded and resolved, pending children are
       automatically resolved (inheritance chain merged).
    """

    STATE_PENDING = "pending"
    STATE_RESOLVED = "resolved"
    STATE_CHOICES = [
        (STATE_PENDING, "Pending (needs parent profile)"),
        (STATE_RESOLVED, "Resolved"),
    ]

    # ── Metadata ────────────────────────────────────────────────────────
    name = models.CharField(
        max_length=255,
        help_text="Display name for this profile",
    )
    description = models.TextField(
        blank=True,
        help_text="Supports Markdown formatting.",
    )
    orca_name = models.CharField(
        max_length=255,
        help_text="Original profile name from OrcaSlicer JSON ('name' field)",
        default="",
    )
    state = models.CharField(
        max_length=20,
        choices=STATE_CHOICES,
        default=STATE_PENDING,
        help_text="Whether inheritance has been fully resolved",
    )
    inherits_name = models.CharField(
        max_length=255,
        blank=True,
        help_text="Parent profile name (from 'inherits' field in JSON)",
    )
    setting_id = models.CharField(max_length=50, blank=True)
    instantiation = models.BooleanField(
        default=True,
        help_text="True = selectable profile; False = base/template only",
    )
    renamed_from = models.CharField(
        max_length=255,
        blank=True,
        help_text="Old profile name before OrcaSlicer rename (used for parent lookup)",
    )

    # Raw JSON from the uploaded file (before inheritance resolution)
    uploaded_json = models.JSONField(
        default=dict,
        help_text="Raw JSON content from the uploaded profile file",
    )

    # ── Material identification ─────────────────────────────────────────
    filament_type = models.JSONField(
        default=list,
        blank=True,
        help_text="Material type(s) e.g. PLA, PETG [string[]]",
    )
    filament_vendor = models.JSONField(
        default=list,
        blank=True,
        help_text="Vendor name(s) [string[]]",
    )
    filament_density = models.JSONField(
        default=list,
        blank=True,
        help_text="Density in g/cm³ [float[]]",
    )
    filament_diameter = models.JSONField(
        default=list,
        blank=True,
        help_text="Filament diameter in mm [float[]]",
    )
    filament_cost = models.JSONField(
        default=list,
        blank=True,
        help_text="Price per unit [float[]]",
    )
    filament_flow_ratio = models.JSONField(
        default=list,
        blank=True,
        help_text="Flow ratio [float[]]",
    )
    filament_max_volumetric_speed = models.JSONField(
        default=list,
        blank=True,
        help_text="Max volumetric speed in mm³/s [float[]]",
    )

    # ── Nozzle temperatures ─────────────────────────────────────────────
    nozzle_temperature = models.JSONField(
        default=list,
        blank=True,
        help_text="Nozzle temperature per extruder in °C [int[]]",
    )
    nozzle_temperature_initial_layer = models.JSONField(
        default=list,
        blank=True,
        help_text="First-layer nozzle temperature [int[]]",
    )
    nozzle_temperature_range_low = models.JSONField(
        default=list,
        blank=True,
        help_text="Minimum recommended nozzle temperature [int[]]",
    )
    nozzle_temperature_range_high = models.JSONField(
        default=list,
        blank=True,
        help_text="Maximum recommended nozzle temperature [int[]]",
    )

    # ── Bed temperatures ────────────────────────────────────────────────
    bed_temperature = models.JSONField(
        default=list,
        blank=True,
        help_text="Bed temperature per extruder in °C [int[]]",
    )
    bed_temperature_initial_layer = models.JSONField(
        default=list,
        blank=True,
        help_text="First-layer bed temperature [int[]]",
    )
    hot_plate_temp = models.JSONField(
        default=list,
        blank=True,
        help_text="Hot plate temperature [int[]]",
    )
    hot_plate_temp_initial_layer = models.JSONField(
        default=list,
        blank=True,
        help_text="First-layer hot plate temperature [int[]]",
    )
    cool_plate_temp = models.JSONField(
        default=list,
        blank=True,
        help_text="Cool plate temperature [int[]]",
    )
    cool_plate_temp_initial_layer = models.JSONField(
        default=list,
        blank=True,
        help_text="First-layer cool plate temperature [int[]]",
    )
    temperature_vitrification = models.JSONField(
        default=list,
        blank=True,
        help_text="Softening / glass-transition temperature [int[]]",
    )

    # ── Cooling / fan ───────────────────────────────────────────────────
    fan_min_speed = models.JSONField(
        default=list,
        blank=True,
        help_text="Minimum part-cooling fan speed [float[]]",
    )
    fan_max_speed = models.JSONField(
        default=list,
        blank=True,
        help_text="Maximum part-cooling fan speed [float[]]",
    )
    overhang_fan_speed = models.JSONField(
        default=list,
        blank=True,
        help_text="Overhang / bridge fan speed [int[]]",
    )
    close_fan_the_first_x_layers = models.JSONField(
        default=list,
        blank=True,
        help_text="Number of initial layers with fan off [int[]]",
    )

    # ── Pressure advance ────────────────────────────────────────────────
    pressure_advance = models.JSONField(
        default=list,
        blank=True,
        help_text="Pressure advance / linear advance value [float[]]",
    )
    enable_pressure_advance = models.JSONField(
        default=list,
        blank=True,
        help_text="Enable pressure advance [bool[]]",
    )

    # ── G-code ──────────────────────────────────────────────────────────
    filament_start_gcode = models.JSONField(
        default=list,
        blank=True,
        help_text="Filament start G-code [string[]]",
    )
    filament_end_gcode = models.JSONField(
        default=list,
        blank=True,
        help_text="Filament end G-code [string[]]",
    )

    # ── Material properties ─────────────────────────────────────────────
    filament_soluble = models.JSONField(
        default=list,
        blank=True,
        help_text="Soluble material flag [bool[]]",
    )
    filament_is_support = models.JSONField(
        default=list,
        blank=True,
        help_text="Support material flag [bool[]]",
    )

    # ── Catch-all for remaining settings ────────────────────────────────
    extra_settings = models.JSONField(
        default=dict,
        blank=True,
        help_text="All resolved settings not stored in dedicated columns",
    )

    # ── Ownership & timestamps ──────────────────────────────────────────
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="orca_filament_profiles",
        help_text="User who imported this profile (informational only)",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]
        permissions = [
            (
                "can_manage_orca_profiles",
                "Can import, edit, and delete OrcaSlicer profiles",
            ),
        ]

    def __str__(self) -> str:
        suffix = "" if self.state == self.STATE_RESOLVED else " (pending)"
        return f"{self.name}{suffix}"

    # ── Convenience properties ──────────────────────────────────────────

    @property
    def is_resolved(self) -> bool:
        """Whether inheritance has been fully resolved."""
        return self.state == self.STATE_RESOLVED

    @property
    def first_filament_type(self) -> str | None:
        """First element of the filament_type array (e.g. 'PLA')."""
        if self.filament_type and len(self.filament_type) > 0:
            return str(self.filament_type[0])
        return None

    @property
    def first_nozzle_temperature(self) -> int | None:
        """First nozzle temperature value."""
        if self.nozzle_temperature and len(self.nozzle_temperature) > 0:
            try:
                return int(self.nozzle_temperature[0])
            except (ValueError, TypeError):
                return None
        return None

    @property
    def first_bed_temperature(self) -> int | None:
        """First bed temperature value."""
        if self.bed_temperature and len(self.bed_temperature) > 0:
            try:
                return int(self.bed_temperature[0])
            except (ValueError, TypeError):
                return None
        return None

    @property
    def first_max_volumetric_speed(self) -> float | None:
        """First max volumetric speed value."""
        if self.filament_max_volumetric_speed and len(self.filament_max_volumetric_speed) > 0:
            try:
                return float(self.filament_max_volumetric_speed[0])
            except (ValueError, TypeError):
                return None
        return None

    # ── Import / Resolution ─────────────────────────────────────────────

    @classmethod
    def get_pending_for_parent(cls, parent_name: str) -> models.QuerySet:
        """Return pending profiles waiting for the given parent.

        Args:
            parent_name: The OrcaSlicer profile name of the parent.

        Returns:
            QuerySet of pending OrcaFilamentProfile instances.
        """
        return cls.objects.filter(
            inherits_name=parent_name,
            state=cls.STATE_PENDING,
        )


class OrcaPrintPreset(models.Model):
    """Fully resolved OrcaSlicer process (print) profile.

    Stores the flattened, inheritance-resolved OrcaSlicer process profile.
    Key settings are exposed as dedicated DB columns for direct querying,
    while all remaining settings are stored in ``extra_settings`` (JSONField).

    Import workflow:
    1. User uploads a JSON profile file.
    2. If the ``inherits`` field is set and the parent profile does not yet
       exist, the profile is stored as *pending*.
    3. When the parent is uploaded and resolved, pending children are
       automatically resolved (inheritance chain merged).
    """

    STATE_PENDING = "pending"
    STATE_RESOLVED = "resolved"
    STATE_CHOICES = [
        (STATE_PENDING, "Pending (needs parent profile)"),
        (STATE_RESOLVED, "Resolved"),
    ]

    # ── Metadata ────────────────────────────────────────────────────────
    name = models.CharField(
        max_length=255,
        help_text="Display name for this profile",
    )
    description = models.TextField(
        blank=True,
        help_text="Supports Markdown formatting.",
    )
    orca_name = models.CharField(
        max_length=255,
        help_text="Original profile name from OrcaSlicer JSON ('name' field)",
        default="",
    )
    state = models.CharField(
        max_length=20,
        choices=STATE_CHOICES,
        default=STATE_PENDING,
        help_text="Whether inheritance has been fully resolved",
    )
    inherits_name = models.CharField(
        max_length=255,
        blank=True,
        help_text="Parent profile name (from 'inherits' field in JSON)",
    )
    setting_id = models.CharField(max_length=50, blank=True)
    instantiation = models.BooleanField(
        default=True,
        help_text="True = selectable profile; False = base/template only",
    )
    renamed_from = models.CharField(
        max_length=255,
        blank=True,
        help_text="Old profile name before OrcaSlicer rename (used for parent lookup)",
    )

    # Raw JSON from the uploaded file (before inheritance resolution)
    uploaded_json = models.JSONField(
        default=dict,
        help_text="Raw JSON content from the uploaded profile file",
    )

    # ── Layer height ────────────────────────────────────────────────────
    layer_height = models.FloatField(
        null=True,
        blank=True,
        help_text="Layer height in mm",
    )
    initial_layer_print_height = models.FloatField(
        null=True,
        blank=True,
        help_text="First layer height in mm",
    )

    # ── Line widths ─────────────────────────────────────────────────────
    line_width = models.CharField(
        max_length=20,
        blank=True,
        help_text="Default line width (mm or percentage e.g. '0.45' or '100%')",
    )
    outer_wall_line_width = models.CharField(
        max_length=20,
        blank=True,
        help_text="Outer wall line width",
    )
    inner_wall_line_width = models.CharField(
        max_length=20,
        blank=True,
        help_text="Inner wall line width",
    )
    initial_layer_line_width = models.CharField(
        max_length=20,
        blank=True,
        help_text="First layer line width",
    )
    top_surface_line_width = models.CharField(
        max_length=20,
        blank=True,
        help_text="Top surface line width",
    )
    sparse_infill_line_width = models.CharField(
        max_length=20,
        blank=True,
        help_text="Sparse infill line width",
    )

    # ── Walls ───────────────────────────────────────────────────────────
    wall_loops = models.IntegerField(
        null=True,
        blank=True,
        help_text="Number of wall loops (perimeters)",
    )

    # ── Shell layers ────────────────────────────────────────────────────
    top_shell_layers = models.IntegerField(
        null=True,
        blank=True,
        help_text="Number of top solid layers",
    )
    bottom_shell_layers = models.IntegerField(
        null=True,
        blank=True,
        help_text="Number of bottom solid layers",
    )

    # ── Infill ──────────────────────────────────────────────────────────
    sparse_infill_density = models.CharField(
        max_length=20,
        blank=True,
        help_text="Infill density (percentage string e.g. '15%')",
    )
    sparse_infill_pattern = models.CharField(
        max_length=50,
        blank=True,
        help_text="Infill pattern (grid, gyroid, honeycomb, …)",
    )
    top_surface_pattern = models.CharField(
        max_length=50,
        blank=True,
        help_text="Top surface pattern (monotonic, monotonicline, …)",
    )

    # ── Speeds ──────────────────────────────────────────────────────────
    outer_wall_speed = models.FloatField(
        null=True,
        blank=True,
        help_text="Outer wall speed in mm/s",
    )
    inner_wall_speed = models.FloatField(
        null=True,
        blank=True,
        help_text="Inner wall speed in mm/s",
    )
    sparse_infill_speed = models.FloatField(
        null=True,
        blank=True,
        help_text="Sparse infill speed in mm/s",
    )
    internal_solid_infill_speed = models.FloatField(
        null=True,
        blank=True,
        help_text="Internal solid infill speed in mm/s",
    )
    top_surface_speed = models.FloatField(
        null=True,
        blank=True,
        help_text="Top surface speed in mm/s",
    )
    travel_speed = models.FloatField(
        null=True,
        blank=True,
        help_text="Travel speed in mm/s",
    )
    bridge_speed = models.FloatField(
        null=True,
        blank=True,
        help_text="Bridge speed in mm/s",
    )
    gap_infill_speed = models.FloatField(
        null=True,
        blank=True,
        help_text="Gap infill speed in mm/s",
    )
    initial_layer_speed = models.FloatField(
        null=True,
        blank=True,
        help_text="First layer speed in mm/s",
    )

    # ── Acceleration ────────────────────────────────────────────────────
    default_acceleration = models.FloatField(
        null=True,
        blank=True,
        help_text="Default acceleration in mm/s²",
    )
    outer_wall_acceleration = models.FloatField(
        null=True,
        blank=True,
        help_text="Outer wall acceleration in mm/s²",
    )
    inner_wall_acceleration = models.FloatField(
        null=True,
        blank=True,
        help_text="Inner wall acceleration in mm/s²",
    )
    travel_acceleration = models.FloatField(
        null=True,
        blank=True,
        help_text="Travel acceleration in mm/s²",
    )
    initial_layer_acceleration = models.FloatField(
        null=True,
        blank=True,
        help_text="First layer acceleration in mm/s²",
    )

    # ── Support ─────────────────────────────────────────────────────────
    enable_support = models.BooleanField(
        null=True,
        blank=True,
        help_text="Whether supports are enabled",
    )
    support_type = models.CharField(
        max_length=50,
        blank=True,
        help_text="Support type (normal, tree, …)",
    )
    support_threshold_angle = models.IntegerField(
        null=True,
        blank=True,
        help_text="Support threshold angle in degrees",
    )
    support_style = models.CharField(
        max_length=50,
        blank=True,
        help_text="Support style (default, grid, snug, organic)",
    )

    # ── Brim / adhesion ─────────────────────────────────────────────────
    brim_type = models.CharField(
        max_length=50,
        blank=True,
        help_text="Brim type (no_brim, outer_only, inner_only, outer_and_inner, auto_brim)",
    )
    brim_width = models.FloatField(
        null=True,
        blank=True,
        help_text="Brim width in mm",
    )

    # ── Seam ────────────────────────────────────────────────────────────
    seam_position = models.CharField(
        max_length=50,
        blank=True,
        help_text="Seam position (aligned, nearest, random, back)",
    )

    # ── Quality / detail ────────────────────────────────────────────────
    ironing_type = models.CharField(
        max_length=50,
        blank=True,
        help_text="Ironing type (no_ironing, top_surfaces, topmost_surface, all_solid_layer)",
    )
    detect_overhang_wall = models.BooleanField(
        null=True,
        blank=True,
        help_text="Enable overhang detection for walls",
    )
    elefant_foot_compensation = models.FloatField(
        null=True,
        blank=True,
        help_text="Elephant foot compensation in mm",
    )

    # ── Sequence / multi-object ─────────────────────────────────────────
    print_sequence = models.CharField(
        max_length=50,
        blank=True,
        help_text="Print sequence (by_layer, by_object)",
    )
    enable_prime_tower = models.BooleanField(
        null=True,
        blank=True,
        help_text="Enable prime tower for multi-material",
    )

    # ── Filename format ─────────────────────────────────────────────────
    filename_format = models.CharField(
        max_length=255,
        blank=True,
        help_text="G-code filename format template",
    )

    # ── Catch-all for remaining settings ────────────────────────────────
    extra_settings = models.JSONField(
        default=dict,
        blank=True,
        help_text="All resolved settings not stored in dedicated columns",
    )

    # ── Ownership & timestamps ──────────────────────────────────────────
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="orca_print_presets",
        help_text="User who imported this profile (informational only)",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]

    def __str__(self) -> str:
        suffix = "" if self.state == self.STATE_RESOLVED else " (pending)"
        return f"{self.name}{suffix}"

    # ── Convenience properties ──────────────────────────────────────────

    @property
    def is_resolved(self) -> bool:
        """Whether inheritance has been fully resolved."""
        return self.state == self.STATE_RESOLVED

    @property
    def infill_density_display(self) -> str | None:
        """Human-readable infill density (e.g. '15%')."""
        if self.sparse_infill_density:
            return self.sparse_infill_density
        return None

    @property
    def supports_enabled(self) -> bool:
        """Whether supports are enabled (convenience alias)."""
        return bool(self.enable_support)

    # ── Import / Resolution ─────────────────────────────────────────────

    @classmethod
    def get_pending_for_parent(cls, parent_name: str) -> models.QuerySet:
        """Return pending profiles waiting for the given parent.

        Args:
            parent_name: The OrcaSlicer profile name of the parent.

        Returns:
            QuerySet of pending OrcaPrintPreset instances.
        """
        return cls.objects.filter(
            inherits_name=parent_name,
            state=cls.STATE_PENDING,
        )


class OrcaMachineProfile(models.Model):
    """Fully resolved OrcaSlicer machine (printer) profile.

    Stores the flattened, inheritance-resolved OrcaSlicer machine profile.
    Key settings are exposed as dedicated DB columns for direct querying,
    while all remaining settings are stored in ``extra_settings`` (JSONField).

    Import workflow:
    1. User uploads a JSON profile file.
    2. If the ``inherits`` field is set and the parent profile does not yet
       exist, the profile is stored as *pending*.
    3. When the parent is uploaded and resolved, pending children are
       automatically resolved (inheritance chain merged).
    """

    STATE_PENDING = "pending"
    STATE_RESOLVED = "resolved"
    STATE_CHOICES = [
        (STATE_PENDING, "Pending (needs parent profile)"),
        (STATE_RESOLVED, "Resolved"),
    ]

    # ── Metadata ────────────────────────────────────────────────────────
    name = models.CharField(
        max_length=255,
        help_text="Display name for this profile",
    )
    description = models.TextField(
        blank=True,
        help_text="Supports Markdown formatting.",
    )
    orca_name = models.CharField(
        max_length=255,
        help_text="Original profile name from OrcaSlicer JSON ('name' field)",
    )
    state = models.CharField(
        max_length=20,
        choices=STATE_CHOICES,
        default=STATE_PENDING,
        help_text="Whether inheritance has been fully resolved",
    )
    inherits_name = models.CharField(
        max_length=255,
        blank=True,
        help_text="Parent profile name (from 'inherits' field in JSON)",
    )
    setting_id = models.CharField(max_length=50, blank=True)
    instantiation = models.BooleanField(
        default=True,
        help_text="True = selectable profile; False = base/template only",
    )
    renamed_from = models.CharField(
        max_length=255,
        blank=True,
        help_text="Old profile name before OrcaSlicer rename (used for parent lookup)",
    )

    # Raw JSON from the uploaded file (before inheritance resolution)
    uploaded_json = models.JSONField(
        default=dict,
        help_text="Raw JSON content from the uploaded profile file",
    )

    # ── Key machine settings (populated on resolve) ─────────────────────
    # Printer geometry
    nozzle_diameter = models.JSONField(
        default=list,
        blank=True,
        help_text="Nozzle diameter(s) in mm, per extruder [float[]]",
    )
    printable_area = models.JSONField(
        default=list,
        blank=True,
        help_text="Points defining printable area [point[]]",
    )
    printable_height = models.FloatField(
        null=True,
        blank=True,
        help_text="Maximum printable height in mm",
    )
    bed_shape = models.CharField(
        max_length=50,
        blank=True,
        help_text="Bed shape (e.g. 'rectangular')",
    )
    extruders_count = models.IntegerField(
        null=True,
        blank=True,
        help_text="Number of extruders",
    )

    # Printer identification
    gcode_flavor = models.CharField(
        max_length=50,
        blank=True,
        help_text="G-code flavor (marlin, klipper, reprapfirmware, …)",
    )
    printer_structure = models.CharField(
        max_length=50,
        blank=True,
        help_text="Printer structure (corexy, i3, delta, …)",
    )
    printer_technology = models.CharField(
        max_length=10,
        blank=True,
        help_text="Printer technology (FFF, SLA)",
    )
    printer_model = models.CharField(
        max_length=255,
        blank=True,
        help_text="Printer model/type name",
    )
    printer_variant = models.CharField(
        max_length=50,
        blank=True,
        help_text="Printer variant (often nozzle size, e.g. '0.4')",
    )

    # Motion limits
    machine_max_speed_x = models.IntegerField(null=True, blank=True, help_text="Max X speed (mm/s)")
    machine_max_speed_y = models.IntegerField(null=True, blank=True, help_text="Max Y speed (mm/s)")
    machine_max_speed_z = models.IntegerField(null=True, blank=True, help_text="Max Z speed (mm/s)")
    machine_max_acceleration_x = models.IntegerField(null=True, blank=True, help_text="Max X acceleration (mm/s²)")
    machine_max_acceleration_y = models.IntegerField(null=True, blank=True, help_text="Max Y acceleration (mm/s²)")
    machine_max_acceleration_z = models.IntegerField(null=True, blank=True, help_text="Max Z acceleration (mm/s²)")

    # Retraction (per extruder arrays)
    retraction_length = models.JSONField(
        default=list,
        blank=True,
        help_text="Retraction length per extruder in mm [float[]]",
    )
    retraction_speed = models.JSONField(
        default=list,
        blank=True,
        help_text="Retraction speed per extruder in mm/s [float[]]",
    )
    z_hop = models.JSONField(
        default=list,
        blank=True,
        help_text="Z-hop height per extruder in mm [float[]]",
    )

    # G-code templates
    machine_start_gcode = models.TextField(
        blank=True,
        help_text="Start G-code template",
    )
    machine_end_gcode = models.TextField(
        blank=True,
        help_text="End G-code template",
    )

    # Defaults
    default_bed_type = models.CharField(
        max_length=100,
        blank=True,
        help_text="Default bed type (e.g. 'Textured PEI Plate')",
    )
    default_filament_profile = models.JSONField(
        default=list,
        blank=True,
        help_text="Default filament profile name(s) [string[]]",
    )
    default_print_profile = models.CharField(
        max_length=255,
        blank=True,
        help_text="Default process profile name",
    )

    # Features
    single_extruder_multi_material = models.BooleanField(
        null=True,
        blank=True,
        help_text="Single nozzle multi-material setup",
    )
    use_relative_e_distances = models.BooleanField(
        null=True,
        blank=True,
        help_text="Use relative E distances",
    )
    use_firmware_retraction = models.BooleanField(
        null=True,
        blank=True,
        help_text="Use firmware retraction (G10/G11)",
    )

    # ── Catch-all for remaining settings ────────────────────────────────
    extra_settings = models.JSONField(
        default=dict,
        blank=True,
        help_text="All resolved settings not stored in dedicated columns",
    )

    # ── Ownership & timestamps ──────────────────────────────────────────
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="orca_machine_profiles",
        help_text="User who imported this profile (informational only)",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]

    def __str__(self) -> str:
        suffix = "" if self.state == self.STATE_RESOLVED else " (pending)"
        return f"{self.name}{suffix}"

    # ── Convenience properties ──────────────────────────────────────────

    @property
    def is_resolved(self) -> bool:
        """Whether inheritance has been fully resolved."""
        return self.state == self.STATE_RESOLVED

    @property
    def bed_size_x(self) -> float | None:
        """Approximate bed X size derived from printable_area."""
        return self._bed_dimension(0)

    @property
    def bed_size_y(self) -> float | None:
        """Approximate bed Y size derived from printable_area."""
        return self._bed_dimension(1)

    def _bed_dimension(self, axis: int) -> float | None:
        """Extract bed dimension from printable_area points.

        Args:
            axis: 0 for X, 1 for Y.

        Returns:
            The size along the given axis, or None if not available.
        """
        if not self.printable_area:
            return None
        try:
            coords = []
            for point in self.printable_area:
                parts = str(point).replace(",", "x").split("x")
                coords.append(float(parts[axis]))
            return max(coords) - min(coords)
        except (ValueError, IndexError):
            return None

    @property
    def first_nozzle_diameter(self) -> float | None:
        """Diameter of the first nozzle (convenience shortcut)."""
        if self.nozzle_diameter and len(self.nozzle_diameter) > 0:
            try:
                return float(self.nozzle_diameter[0])
            except (ValueError, TypeError):
                return None
        return None

    # ── Import / Resolution ─────────────────────────────────────────────

    @classmethod
    def get_pending_for_parent(cls, parent_name: str) -> models.QuerySet:
        """Return pending profiles waiting for the given parent.

        Args:
            parent_name: The OrcaSlicer profile name of the parent.

        Returns:
            QuerySet of pending OrcaMachineProfile instances.
        """
        return cls.objects.filter(
            inherits_name=parent_name,
            state=cls.STATE_PENDING,
        )
