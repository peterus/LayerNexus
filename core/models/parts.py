"""Part and PrintTimeEstimate models for the LayerNexus application."""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional

from django.core.validators import MinValueValidator
from django.db import models
from django.db.models import CheckConstraint, Q, Sum

if TYPE_CHECKING:
    from core.models.orca_profiles import OrcaPrintPreset


class Part(models.Model):
    """A single part within a project that needs to be printed."""

    project = models.ForeignKey("core.Project", on_delete=models.CASCADE, related_name="parts")
    name = models.CharField(
        max_length=255,
        blank=True,
        help_text="Optional — derived from the uploaded filename if left empty.",
    )
    stl_file = models.FileField(upload_to="stl_files/", blank=True, null=True)  # also stores 3MF files
    quantity = models.PositiveIntegerField(default=1, validators=[MinValueValidator(1)])

    @property
    def is_3mf(self) -> bool:
        """Return True if the uploaded model file is a 3MF file."""
        return bool(self.stl_file) and self.stl_file.name.lower().endswith(".3mf")
    color = models.CharField(
        max_length=100,
        blank=True,
        help_text="Filament color (auto-filled from Spoolman or free text)",
    )
    material = models.CharField(
        max_length=100,
        blank=True,
        help_text="Material type (auto-filled from Spoolman or free text, e.g. PLA, PETG)",
    )
    spoolman_filament_id = models.IntegerField(
        null=True,
        blank=True,
        help_text="Spoolman filament type ID — links this part to a Spoolman filament",
    )
    print_preset = models.ForeignKey(
        "OrcaPrintPreset",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="parts",
        help_text="Print preset for slicing this part (inherited from project if not set)",
    )
    notes = models.TextField(blank=True)

    # Filament usage estimates (back-filled from first successful PrintJob slice)
    filament_used_grams = models.FloatField(null=True, blank=True)
    filament_used_meters = models.FloatField(null=True, blank=True)
    estimated_print_time = models.DurationField(null=True, blank=True)

    # Estimation status tracking
    ESTIMATION_NONE = "none"
    ESTIMATION_PENDING = "pending"
    ESTIMATION_ESTIMATING = "estimating"
    ESTIMATION_SUCCESS = "success"
    ESTIMATION_ERROR = "error"
    ESTIMATION_STATUS_CHOICES = [
        (ESTIMATION_NONE, "None"),
        (ESTIMATION_PENDING, "Pending"),
        (ESTIMATION_ESTIMATING, "Estimating"),
        (ESTIMATION_SUCCESS, "Success"),
        (ESTIMATION_ERROR, "Error"),
    ]
    estimation_status = models.CharField(
        max_length=10,
        choices=ESTIMATION_STATUS_CHOICES,
        default=ESTIMATION_NONE,
        db_index=True,
        help_text="Status of the background filament/time estimation.",
    )
    estimation_error = models.TextField(
        blank=True,
        default="",
        help_text="Error message from the last failed estimation attempt.",
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]
        constraints = [
            CheckConstraint(
                condition=Q(quantity__gte=1),
                name="part_quantity_gte_1",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.name} ({self.project.name})"

    @property
    def effective_print_preset_id(self) -> Optional[int]:
        """Return the effective print preset ID (own or inherited from project hierarchy).

        Resolution order:
        1. The part's own ``print_preset_id``
        2. The project's ``default_print_preset_id`` (traversing parent projects)

        Returns:
            Print preset primary key, or ``None`` if none is configured.
        """
        if self.print_preset_id:
            return self.print_preset_id
        if self.project_id:
            return self.project.effective_default_print_preset_id
        return None

    @property
    def effective_print_preset(self) -> Optional[OrcaPrintPreset]:
        """Return the effective print preset object (own or inherited from project hierarchy).

        Resolution order:
        1. The part's own ``print_preset``
        2. The project's effective default print preset (traversing parent projects)

        Returns:
            OrcaPrintPreset instance, or ``None`` if none is configured.
        """
        if self.print_preset_id:
            return self.print_preset
        if self.project_id:
            return self.project.effective_default_print_preset
        return None

    @property
    def color_display(self) -> str:
        """Display-friendly color string ('—' if not set)."""
        return self.color or "—"

    @property
    def printed_quantity(self) -> int:
        """Number of this part already printed (across all completed job plates)."""
        completed_qty = (
            self.job_entries.filter(
                print_job__plates__status="completed",
            )
            .values("print_job")
            .distinct()
            .aggregate(total=Sum("quantity"))["total"]
            or 0
        )
        return completed_qty

    @property
    def remaining_quantity(self) -> int:
        """Number of this part still needed to complete the project."""
        return max(0, self.quantity - self.printed_quantity)

    @property
    def is_complete(self) -> bool:
        """Whether all required instances of this part have been printed."""
        return self.printed_quantity >= self.quantity


class PrintTimeEstimate(models.Model):
    """Historical print time data for calibrating future estimates."""

    part = models.ForeignKey(Part, on_delete=models.CASCADE, related_name="time_estimates")
    printer = models.ForeignKey("core.PrinterProfile", on_delete=models.CASCADE, related_name="time_estimates")
    estimated_time = models.DurationField(help_text="Slicer estimate")
    actual_time = models.DurationField(null=True, blank=True, help_text="Measured actual time")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"Estimate for {self.part.name} on {self.printer.name}"

    @property
    def accuracy_factor(self) -> float | None:
        """Ratio of actual to estimated time; useful for calibrating future estimates."""
        if not self.actual_time or not self.estimated_time:
            return None
        est = self.estimated_time.total_seconds()
        if est == 0:
            return None
        return self.actual_time.total_seconds() / est
