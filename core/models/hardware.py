"""HardwarePart and ProjectHardware models for the LayerNexus application."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional

from django.conf import settings
from django.core.validators import MinValueValidator
from django.db import models, transaction

if TYPE_CHECKING:
    from core.models.projects import Project


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
        on_delete=models.PROTECT,
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


class ShoppingItemNotRequired(LookupError):
    """Raised when a hardware part is not part of a project's aggregated demand."""


@dataclass
class ShoppingRow:
    """One line of a project's shopping list with its effective status.

    Attributes:
        hardware_part: The hardware part to buy.
        quantity: Current aggregated quantity needed.
        status: Effective status (``open``, ``in_cart`` or ``ordered``).
        previous_quantity: Quantity stored when the status was set, if the
            demand changed since then (row fell back to ``open``); else ``None``.
        projects: Names of the contributing projects.
    """

    hardware_part: HardwarePart
    quantity: int
    status: str
    previous_quantity: int | None
    projects: list[str]

    @property
    def next_status(self) -> str:
        """Return the next status in the cycle open -> in_cart -> ordered -> open."""
        order = HardwareShoppingItem.ALL_STATUSES
        return order[(order.index(self.status) + 1) % len(order)]


class HardwareShoppingItem(models.Model):
    """Persisted shopping status of a hardware part within one project's list.

    "Open" is never stored (no record means open). ``quantity`` is the
    aggregated quantity at the time the status was set; a deviating current
    demand makes the row effectively open again.
    """

    STATUS_OPEN = "open"
    STATUS_IN_CART = "in_cart"
    STATUS_ORDERED = "ordered"
    STATUS_CHOICES = [
        (STATUS_IN_CART, "In cart"),
        (STATUS_ORDERED, "Ordered"),
    ]
    ALL_STATUSES = (STATUS_OPEN, STATUS_IN_CART, STATUS_ORDERED)

    project = models.ForeignKey(
        "core.Project",
        on_delete=models.CASCADE,
        related_name="hardware_shopping_items",
    )
    hardware_part = models.ForeignKey(
        HardwarePart,
        on_delete=models.CASCADE,
        related_name="shopping_items",
    )
    status = models.CharField(max_length=10, choices=STATUS_CHOICES)
    quantity = models.PositiveIntegerField(
        validators=[MinValueValidator(1)],
        help_text="Aggregated quantity at the time the status was set.",
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["project", "hardware_part__category", "hardware_part__name"]
        constraints = [
            models.UniqueConstraint(fields=["project", "hardware_part"], name="uniq_shoppingitem_project_part"),
        ]

    def __str__(self) -> str:
        return f"{self.project.name}: {self.hardware_part.name} ({self.get_status_display()})"

    @classmethod
    def build_list(cls, project: Project) -> list[ShoppingRow]:
        """Build the project's shopping list with effective statuses.

        Args:
            project: The project whose aggregated hardware demand is listed.

        Returns:
            Rows sorted by category order (``CATEGORY_CHOICES``), then name.
        """
        stored = {item.hardware_part_id: item for item in project.hardware_shopping_items.all()}
        category_index = {key: i for i, (key, _label) in enumerate(HardwarePart.CATEGORY_CHOICES)}
        rows: list[ShoppingRow] = []
        for req in project.hardware_requirements():
            part = req["hardware_part"]
            quantity = req["total_quantity"]
            item = stored.get(part.pk)
            status = cls.STATUS_OPEN
            previous: int | None = None
            if item is not None:
                if item.quantity == quantity:
                    status = item.status
                else:
                    previous = item.quantity
            rows.append(ShoppingRow(part, quantity, status, previous, req["projects"]))
        rows.sort(
            key=lambda r: (
                category_index.get(r.hardware_part.category, len(category_index)),
                r.hardware_part.name.lower(),
            )
        )
        return rows

    @classmethod
    def counts(cls, rows: list[ShoppingRow]) -> dict[str, int]:
        """Count rows per effective status (always contains all three keys)."""
        result = dict.fromkeys(cls.ALL_STATUSES, 0)
        for row in rows:
            result[row.status] += 1
        return result

    @classmethod
    def _prune_orphans(cls, project: Project, rows: list[ShoppingRow]) -> None:
        """Delete records of parts no longer in the project's demand."""
        project.hardware_shopping_items.exclude(hardware_part_id__in=[r.hardware_part.pk for r in rows]).delete()

    @classmethod
    def set_status(cls, project: Project, hardware_part_id: int, status: str) -> ShoppingRow:
        """Set the target status of one row, snapshotting the current quantity.

        Args:
            project: The project owning the list.
            hardware_part_id: Primary key of the hardware part.
            status: Target status; ``open`` deletes the record.

        Returns:
            The updated row.

        Raises:
            ValueError: If ``status`` is not a known status.
            ShoppingItemNotRequired: If the part is not in the project's demand.
        """
        if status not in cls.ALL_STATUSES:
            raise ValueError(f"Invalid shopping status: {status!r}")
        with transaction.atomic():
            rows = cls.build_list(project)
            row = next((r for r in rows if r.hardware_part.pk == hardware_part_id), None)
            if row is None:
                raise ShoppingItemNotRequired(f"Hardware part {hardware_part_id} is not required by {project.name}")
            cls._prune_orphans(project, rows)
            if status == cls.STATUS_OPEN:
                project.hardware_shopping_items.filter(hardware_part_id=hardware_part_id).delete()
            else:
                cls.objects.update_or_create(
                    project=project,
                    hardware_part_id=hardware_part_id,
                    defaults={"status": status, "quantity": row.quantity},
                )
            row.status = status
            row.previous_quantity = None
            return row

    @classmethod
    def mark_cart_ordered(cls, project: Project) -> int:
        """Switch all effectively "in cart" rows to "ordered".

        Returns:
            Number of rows switched.
        """
        with transaction.atomic():
            rows = cls.build_list(project)
            cls._prune_orphans(project, rows)
            part_ids = [r.hardware_part.pk for r in rows if r.status == cls.STATUS_IN_CART]
            return project.hardware_shopping_items.filter(hardware_part_id__in=part_ids).update(
                status=cls.STATUS_ORDERED
            )

    @classmethod
    def reset(cls, project: Project) -> int:
        """Delete all shopping records of the project.

        Returns:
            Number of deleted records.
        """
        with transaction.atomic():
            deleted, _ = project.hardware_shopping_items.all().delete()
            return deleted
