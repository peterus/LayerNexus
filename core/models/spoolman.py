"""SpoolmanFilamentMapping model for the LayerNexus application."""

from __future__ import annotations

from django.conf import settings
from django.db import models


class SpoolmanFilamentMapping(models.Model):
    """Maps a Spoolman filament type to an OrcaSlicer filament profile.

    Allows automatic profile selection during slicing based on the filament
    assigned to a part via Spoolman.
    """

    spoolman_filament_id = models.IntegerField(
        help_text="Spoolman filament type ID",
    )
    spoolman_filament_name = models.CharField(
        max_length=255,
        blank=True,
        help_text="Cached display name from Spoolman (automatically updated)",
    )
    spoolman_color_hex = models.CharField(
        max_length=7,
        blank=True,
        help_text="Cached hex color from Spoolman (e.g. FF0000), automatically updated",
    )
    orca_filament_profile = models.ForeignKey(
        "OrcaFilamentProfile",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="spoolman_mappings",
        help_text="OrcaSlicer filament profile to use when slicing with this filament type",
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="spoolman_filament_mappings",
        help_text="User who created this mapping (informational only)",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["spoolman_filament_name", "spoolman_filament_id"]
        constraints = [
            models.UniqueConstraint(
                fields=["spoolman_filament_id"],
                name="unique_spoolman_filament_mapping",
            ),
        ]
        permissions = [
            (
                "can_manage_filament_mappings",
                "Can create, edit, and delete filament mappings",
            ),
        ]

    def __str__(self) -> str:
        name = self.spoolman_filament_name or f"Filament #{self.spoolman_filament_id}"
        profile = self.orca_filament_profile.name if self.orca_filament_profile else "—"
        return f"{name} → {profile}"
