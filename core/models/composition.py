"""Composition edge models: reusable parts and modules in a DAG.

``ProjectPart`` links a reusable :class:`~core.models.parts.Part` into a project/module
with a per-edge quantity; ``ProjectComponent`` links a child project/module into a parent
assembly. Together they express the many-to-many composition graph that lets a building
block be shared across multiple assemblies (e.g. two truck variants).
"""

from __future__ import annotations

from django.core.validators import MinValueValidator
from django.db import models
from django.db.models import CheckConstraint, F, Q, UniqueConstraint


class ProjectPart(models.Model):
    """A reusable part included in a project/module with a quantity."""

    project = models.ForeignKey(
        "core.Project",
        on_delete=models.CASCADE,
        related_name="part_links",
        help_text="The module/assembly this part is included in.",
    )
    part = models.ForeignKey(
        "core.Part",
        on_delete=models.CASCADE,
        related_name="project_links",
        help_text="The reusable part referenced by the module/assembly.",
    )
    quantity = models.PositiveIntegerField(
        default=1,
        validators=[MinValueValidator(1)],
        help_text="How many of this part the module needs.",
    )
    position = models.PositiveIntegerField(
        default=0,
        help_text="Ordering of this part within the module.",
    )

    class Meta:
        ordering = ["position", "pk"]
        constraints = [
            UniqueConstraint(fields=["project", "part"], name="uniq_projectpart_project_part"),
            CheckConstraint(condition=Q(quantity__gte=1), name="projectpart_quantity_gte_1"),
        ]

    def __str__(self) -> str:
        return f"{self.quantity}× {self.part_id} in {self.project_id}"


class ProjectComponent(models.Model):
    """A child project/module included in a parent assembly with a quantity."""

    parent_project = models.ForeignKey(
        "core.Project",
        on_delete=models.CASCADE,
        related_name="child_links",
        help_text="The assembly that contains the child module.",
    )
    child_project = models.ForeignKey(
        "core.Project",
        on_delete=models.CASCADE,
        related_name="parent_links",
        help_text="The module included in the parent assembly.",
    )
    quantity = models.PositiveIntegerField(
        default=1,
        validators=[MinValueValidator(1)],
        help_text="How many of this child module the assembly needs.",
    )
    position = models.PositiveIntegerField(
        default=0,
        help_text="Ordering of this child within the assembly.",
    )

    class Meta:
        ordering = ["position", "pk"]
        constraints = [
            UniqueConstraint(
                fields=["parent_project", "child_project"],
                name="uniq_projectcomponent_parent_child",
            ),
            CheckConstraint(condition=Q(quantity__gte=1), name="projectcomponent_quantity_gte_1"),
            CheckConstraint(
                condition=~Q(parent_project=F("child_project")),
                name="projectcomponent_no_self_parent",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.quantity}× {self.child_project_id} in {self.parent_project_id}"
