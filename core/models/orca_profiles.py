"""OrcaSlicer profile models (filament, print preset, machine) for the LayerNexus application."""

from __future__ import annotations

from django.conf import settings as django_settings
from django.db import models
from django.db.models import Index

__all__ = [
    "OrcaProfileBase",
    "OrcaFilamentProfile",
    "OrcaMachineProfile",
    "OrcaPrintPreset",
]


class OrcaProfileBase(models.Model):
    """Abstract base class for OrcaSlicer profile models.

    Holds the common fields, properties, and methods shared by all three
    concrete profile types (filament, print preset, machine).

    Import workflow (common to all profile types):
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

    # ── All resolved settings ───────────────────────────────────────────
    settings = models.JSONField(
        default=dict,
        blank=True,
        help_text="All resolved settings",
    )

    # ── Timestamps ──────────────────────────────────────────────────────
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True
        ordering = ["name"]
        indexes = [
            Index(fields=["state", "instantiation"], name="%(class)s_state_inst_idx"),
        ]

    def __str__(self) -> str:
        suffix = "" if self.state == self.STATE_RESOLVED else " (pending)"
        return f"{self.name}{suffix}"

    # ── Convenience properties ──────────────────────────────────────────

    @property
    def is_resolved(self) -> bool:
        """Whether inheritance has been fully resolved."""
        return self.state == self.STATE_RESOLVED

    # ── Import / Resolution ─────────────────────────────────────────────

    @classmethod
    def get_pending_for_parent(cls, parent_name: str) -> models.QuerySet:
        """Return pending profiles waiting for the given parent.

        Args:
            parent_name: The OrcaSlicer profile name of the parent.

        Returns:
            QuerySet of pending profile instances.
        """
        return cls.objects.filter(
            inherits_name=parent_name,
            state=cls.STATE_PENDING,
        )


class OrcaFilamentProfile(OrcaProfileBase):
    """Fully resolved OrcaSlicer filament profile.

    Stores the flattened, inheritance-resolved OrcaSlicer filament profile.
    All resolved settings are stored in a single ``settings`` JSONField.
    """

    # ── Ownership ───────────────────────────────────────────────────────
    created_by = models.ForeignKey(
        django_settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="orca_filament_profiles",
        help_text="User who imported this profile (informational only)",
    )

    class Meta(OrcaProfileBase.Meta):
        permissions = [
            (
                "can_manage_orca_profiles",
                "Can import, edit, and delete OrcaSlicer profiles",
            ),
        ]

    # ── Filament-specific properties ────────────────────────────────────

    @property
    def first_filament_type(self) -> str | None:
        """First element of the filament_type array (e.g. 'PLA')."""
        ft = self.settings.get("filament_type")
        if ft and len(ft) > 0:
            return str(ft[0])
        return None

    @property
    def first_nozzle_temperature(self) -> int | None:
        """First nozzle temperature value."""
        nt = self.settings.get("nozzle_temperature")
        if nt and len(nt) > 0:
            try:
                return int(nt[0])
            except (ValueError, TypeError):
                return None
        return None

    @property
    def first_bed_temperature(self) -> int | None:
        """First bed temperature value."""
        bt = self.settings.get("bed_temperature")
        if bt and len(bt) > 0:
            try:
                return int(bt[0])
            except (ValueError, TypeError):
                return None
        return None

    @property
    def first_max_volumetric_speed(self) -> float | None:
        """First max volumetric speed value."""
        mvs = self.settings.get("filament_max_volumetric_speed")
        if mvs and len(mvs) > 0:
            try:
                return float(mvs[0])
            except (ValueError, TypeError):
                return None
        return None


class OrcaPrintPreset(OrcaProfileBase):
    """Fully resolved OrcaSlicer process (print) profile.

    Stores the flattened, inheritance-resolved OrcaSlicer process profile.
    All resolved settings are stored in a single ``settings`` JSONField.
    """

    # ── Ownership ───────────────────────────────────────────────────────
    created_by = models.ForeignKey(
        django_settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="orca_print_presets",
        help_text="User who imported this profile (informational only)",
    )

    class Meta(OrcaProfileBase.Meta):
        pass

    # ── Print-preset-specific properties ────────────────────────────────

    @property
    def infill_density_display(self) -> str | None:
        """Human-readable infill density (e.g. '15%')."""
        v = self.settings.get("sparse_infill_density")
        return v if v else None

    @property
    def supports_enabled(self) -> bool:
        """Whether supports are enabled (convenience alias)."""
        return bool(self.settings.get("enable_support"))


class OrcaMachineProfile(OrcaProfileBase):
    """Fully resolved OrcaSlicer machine (printer) profile.

    Stores the flattened, inheritance-resolved OrcaSlicer machine profile.
    All resolved settings are stored in a single ``settings`` JSONField.
    """

    # ── Ownership ───────────────────────────────────────────────────────
    created_by = models.ForeignKey(
        django_settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="orca_machine_profiles",
        help_text="User who imported this profile (informational only)",
    )

    class Meta(OrcaProfileBase.Meta):
        pass

    # ── Machine-specific properties ─────────────────────────────────────

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
        printable_area = self.settings.get("printable_area")
        if not printable_area:
            return None
        try:
            coords = []
            for point in printable_area:
                parts = str(point).replace(",", "x").split("x")
                coords.append(float(parts[axis]))
            return max(coords) - min(coords)
        except (ValueError, IndexError):
            return None

    @property
    def first_nozzle_diameter(self) -> float | None:
        """Diameter of the first nozzle (convenience shortcut)."""
        nd = self.settings.get("nozzle_diameter")
        if nd and len(nd) > 0:
            try:
                return float(nd[0])
            except (ValueError, TypeError):
                return None
        return None

    @property
    def printable_height(self) -> float | None:
        """Maximum printable height in mm."""
        v = self.settings.get("printable_height")
        if v is not None:
            try:
                return float(v)
            except (ValueError, TypeError):
                return None
        return None
