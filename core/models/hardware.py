"""HardwarePart and ProjectHardware models for the LayerNexus application."""

from __future__ import annotations

from typing import Optional

from django.conf import settings
from django.core.validators import MinValueValidator
from django.db import models


class HardwarePart(models.Model):
    """A reusable hardware component (screw, motor, bearing, etc.).

    Hardware parts live in a shared catalogue and can be referenced by
    many projects via :class:`ProjectHardware`.
    """

    CATEGORY_SCREWS = "screws"
    CATEGORY_NUTS = "nuts"
    CATEGORY_BOLTS = "bolts"
    CATEGORY_THREADED_INSERTS = "threaded_inserts"
    CATEGORY_SPRINGS = "springs"
    CATEGORY_BEARINGS = "bearings"
    CATEGORY_MOTORS = "motors"
    CATEGORY_ELECTRONICS = "electronics"
    CATEGORY_MAGNETS = "magnets"
    CATEGORY_OTHER = "other"

    CATEGORY_CHOICES = [
        (CATEGORY_SCREWS, "Screws"),
        (CATEGORY_NUTS, "Nuts"),
        (CATEGORY_BOLTS, "Bolts"),
        (CATEGORY_THREADED_INSERTS, "Threaded Inserts"),
        (CATEGORY_SPRINGS, "Springs"),
        (CATEGORY_BEARINGS, "Bearings"),
        (CATEGORY_MOTORS, "Motors"),
        (CATEGORY_ELECTRONICS, "Electronics"),
        (CATEGORY_MAGNETS, "Magnets"),
        (CATEGORY_OTHER, "Other"),
    ]

    name = models.CharField(max_length=255)
    category = models.CharField(
        max_length=20,
        choices=CATEGORY_CHOICES,
        default=CATEGORY_OTHER,
    )
    url = models.URLField(
        blank=True,
        help_text="Link to a webshop or datasheet.",
    )
    unit_price = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        null=True,
        blank=True,
        help_text="Price per unit.",
    )
    notes = models.TextField(blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["category", "name"]
        unique_together = ["name", "category"]

    def __str__(self) -> str:
        return f"{self.get_category_display()}: {self.name}"


class ProjectHardware(models.Model):
    """Links a :class:`HardwarePart` to a :class:`Project` with a quantity.

    The same hardware part may appear in many projects with different
    quantities.  Deleting this record removes the assignment but keeps
    the :class:`HardwarePart` catalogue entry.
    """

    project = models.ForeignKey(
        "core.Project",
        on_delete=models.CASCADE,
        related_name="hardware_assignments",
    )
    hardware_part = models.ForeignKey(
        HardwarePart,
        on_delete=models.CASCADE,
        related_name="project_assignments",
    )
    quantity = models.PositiveIntegerField(
        default=1,
        validators=[MinValueValidator(1)],
    )
    notes = models.TextField(blank=True, help_text="Project-specific notes.")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["hardware_part__category", "hardware_part__name"]
        unique_together = ["project", "hardware_part"]

    def __str__(self) -> str:
        return f"{self.hardware_part.name} ×{self.quantity} ({self.project.name})"

    @property
    def total_price(self) -> Optional[float]:
        """Return quantity × unit_price, or None if no price is set."""
        if self.hardware_part.unit_price is None:
            return None
        from decimal import Decimal

        return float(Decimal(str(self.hardware_part.unit_price)) * self.quantity)
