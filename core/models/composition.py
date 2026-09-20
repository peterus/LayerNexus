"""Composition edge models: reusable parts and modules in a DAG.

``ProjectPart`` links a reusable :class:`~core.models.parts.Part` into a project/module
with a per-edge quantity; ``ProjectComponent`` links a child project/module into a parent
assembly. Together they express the many-to-many composition graph that lets a building
block be shared across multiple assemblies (e.g. two truck variants).
"""

from __future__ import annotations

from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator
from django.db import models
from django.db.models import CheckConstraint, F, Q, UniqueConstraint


def component_would_create_cycle(parent_id: int, child_id: int) -> bool:
    """Return True if adding a ``parent_id -> child_id`` edge would create a cycle.

    A cycle forms when the prospective parent is reachable *from* the child by
    following existing ``ProjectComponent`` edges downward (parent → child), or when
    parent and child are the same node. A visited-set guard makes the descent terminate
    even if a corrupt cycle already exists in the database.

    Args:
        parent_id: PK of the prospective parent project.
        child_id: PK of the prospective child project.

    Returns:
        True if the edge would introduce a cycle, else False.
    """
    if parent_id == child_id:
        return True
    visited: set[int] = set()
    stack: list[int] = [child_id]
    while stack:
        current = stack.pop()
        if current == parent_id:
            return True
        if current in visited:
            continue
        visited.add(current)
        stack.extend(
            ProjectComponent.objects.filter(parent_project_id=current).values_list("child_project_id", flat=True)
        )
    return False


def rebuild_composition_edges(project_model, part_model, component_model, part_link_model) -> None:
    """Mirror the legacy FK graph into composition edges, idempotently.

    Creates one ``ProjectPart`` per ``Part.project`` relation and one
    ``ProjectComponent`` per ``Project.parent`` relation, copying the legacy per-node
    ``quantity`` onto the edge. Uses ``update_or_create`` so repeated runs (backfill +
    later reconciliation) neither duplicate nor error. Written to accept model classes so
    the data migration can pass historical ``apps.get_model(...)`` classes while unit
    tests pass the real models.

    Args:
        project_model: The ``Project`` model class.
        part_model: The ``Part`` model class.
        component_model: The ``ProjectComponent`` model class.
        part_link_model: The ``ProjectPart`` model class.
    """
    for part in part_model.objects.filter(project__isnull=False).iterator():
        part_link_model.objects.update_or_create(
            project_id=part.project_id,
            part_id=part.pk,
            defaults={"quantity": part.quantity},
        )
    for child in project_model.objects.filter(parent__isnull=False).iterator():
        component_model.objects.update_or_create(
            parent_project_id=child.parent_id,
            child_project_id=child.pk,
            defaults={"quantity": child.quantity},
        )


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

    def save(self, *args, **kwargs) -> None:
        """Persist the edge, refusing to store one that closes a cycle."""
        if (
            self.parent_project_id
            and self.child_project_id
            and component_would_create_cycle(self.parent_project_id, self.child_project_id)
        ):
            raise ValidationError({"child_project": "This would make an assembly contain itself (cycle)."})
        super().save(*args, **kwargs)

    def clean(self) -> None:
        """Reject edges that would make an assembly (transitively) contain itself."""
        super().clean()
        if (
            self.parent_project_id
            and self.child_project_id
            and component_would_create_cycle(self.parent_project_id, self.child_project_id)
        ):
            raise ValidationError({"child_project": "This would make an assembly contain itself (cycle)."})
