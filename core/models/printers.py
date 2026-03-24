"""PrinterProfile and CostProfile models for the LayerNexus application."""

from __future__ import annotations

from django.conf import settings
from django.db import models


class PrinterProfile(models.Model):
    """Configuration for a 3D printer including slicer and connection settings.

    Bed dimensions and nozzle diameter are derived from the linked
    ``orca_machine_profile`` and exposed as read-only convenience
    properties.
    """

    name = models.CharField(max_length=255)
    description = models.TextField(
        blank=True,
        help_text="Supports Markdown formatting.",
    )

    # OrcaSlicer machine profile (new structured import)
    orca_machine_profile = models.ForeignKey(
        "OrcaMachineProfile",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="printers",
        limit_choices_to={"state": "resolved", "instantiation": True},
        help_text="OrcaSlicer machine profile for this printer (only selectable/resolved profiles)",
    )

    # Klipper/Moonraker connection
    moonraker_url = models.URLField(blank=True, help_text="e.g. http://192.168.1.100:7125")
    moonraker_api_key = models.CharField(max_length=255, blank=True)

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="printer_profiles",
        help_text="User who created this printer profile (informational only)",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        permissions = [
            ("can_manage_printers", "Can create, edit, and delete printers"),
            ("can_control_printer", "Can start prints and cancel running prints"),
        ]

    def __str__(self) -> str:
        return self.name

    # ── Convenience properties (delegated to OrcaMachineProfile) ────────

    @property
    def bed_size_x(self) -> float | None:
        """Bed X dimension in mm from the linked machine profile."""
        if self.orca_machine_profile:
            return self.orca_machine_profile.bed_size_x
        return None

    @property
    def bed_size_y(self) -> float | None:
        """Bed Y dimension in mm from the linked machine profile."""
        if self.orca_machine_profile:
            return self.orca_machine_profile.bed_size_y
        return None

    @property
    def bed_size_z(self) -> float | None:
        """Printable height in mm from the linked machine profile."""
        if self.orca_machine_profile:
            return self.orca_machine_profile.printable_height
        return None

    @property
    def nozzle_diameter(self) -> float | None:
        """First nozzle diameter in mm from the linked machine profile."""
        if self.orca_machine_profile:
            return self.orca_machine_profile.first_nozzle_diameter
        return None


class CostProfile(models.Model):
    """Configuration for cost calculations per printer."""

    printer = models.OneToOneField(PrinterProfile, on_delete=models.CASCADE, related_name="cost_profile")
    electricity_cost_per_kwh = models.DecimalField(
        max_digits=8,
        decimal_places=4,
        default=0.30,
        help_text="Electricity cost per kWh in your currency",
    )
    printer_power_watts = models.IntegerField(default=200, help_text="Average power consumption in watts")
    printer_purchase_cost = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=0,
        help_text="Purchase price for depreciation calculation",
    )
    printer_lifespan_hours = models.IntegerField(
        default=5000,
        help_text="Expected lifespan in print-hours",
    )
    maintenance_cost_per_hour = models.DecimalField(
        max_digits=8,
        decimal_places=4,
        default=0.0,
        help_text="Maintenance cost per print-hour",
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self) -> str:
        return f"Cost Profile: {self.printer.name}"

    @property
    def depreciation_per_hour(self) -> float:
        """Printer depreciation cost per print-hour."""
        if self.printer_lifespan_hours <= 0:
            return 0
        return float(self.printer_purchase_cost) / self.printer_lifespan_hours

    @property
    def electricity_per_hour(self) -> float:
        """Electricity cost per print-hour."""
        return float(self.electricity_cost_per_kwh) * self.printer_power_watts / 1000

    def calculate_print_cost(
        self,
        print_time_hours: float,
        filament_grams: float = 0,
        cost_per_kg: float = 0,
    ) -> dict[str, float]:
        """Calculate total cost for a print job.

        Args:
            print_time_hours: Duration of the print in hours.
            filament_grams: Weight of filament used.
            cost_per_kg: Cost of the filament per kilogram.

        Returns:
            Dictionary with cost breakdown.
        """
        filament_cost = (filament_grams / 1000) * float(cost_per_kg) if cost_per_kg else 0
        electricity = self.electricity_per_hour * print_time_hours
        depreciation = self.depreciation_per_hour * print_time_hours
        maintenance = float(self.maintenance_cost_per_hour) * print_time_hours
        total = filament_cost + electricity + depreciation + maintenance
        return {
            "filament_cost": round(filament_cost, 2),
            "electricity_cost": round(electricity, 2),
            "depreciation_cost": round(depreciation, 2),
            "maintenance_cost": round(maintenance, 2),
            "total_cost": round(total, 2),
        }
