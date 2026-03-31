"""Project model for the LayerNexus application."""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional

from django.conf import settings
from django.core.validators import MinValueValidator
from django.db import models
from django.db.models import CheckConstraint, Q

from core.models.parts import Part
from core.models.spoolman import SpoolmanFilamentMapping

if TYPE_CHECKING:
    from core.models.documents import ProjectDocument
    from core.models.hardware import ProjectHardware
    from core.models.orca_profiles import OrcaPrintPreset


class Project(models.Model):
    """A 3D printing project containing multiple parts.

    Projects can be nested: a sub-project has a non-null ``parent`` and a
    ``quantity`` indicating how many times it is used within the parent.
    Sub-projects are excluded from the top-level project list.
    """

    name = models.CharField(max_length=255)
    description = models.TextField(
        blank=True,
        help_text="Supports Markdown formatting.",
    )
    image = models.ImageField(
        upload_to="project_images/",
        blank=True,
        null=True,
        help_text="Cover image shown in the project list and detail views.",
    )
    parent = models.ForeignKey(
        "self",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="subprojects",
        help_text="Parent project — set to make this a sub-project.",
    )
    quantity = models.PositiveIntegerField(
        default=1,
        validators=[MinValueValidator(1)],
        help_text="How many times this sub-project is needed within its parent project.",
    )
    default_print_preset = models.ForeignKey(
        "OrcaPrintPreset",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="default_for_projects",
        help_text="Default print preset used when creating new parts in this project",
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="projects",
        help_text="User who created this project (informational only)",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-updated_at"]
        constraints = [
            CheckConstraint(
                check=Q(quantity__gte=1),
                name="project_quantity_gte_1",
            ),
        ]
        permissions = [
            ("can_manage_projects", "Can create, edit, and delete projects"),
        ]

    def __str__(self) -> str:
        return self.name

    @property
    def is_subproject(self) -> bool:
        """Return True if this project is a sub-project of another project."""
        return self.parent_id is not None

    def get_ancestors(self) -> list[Project]:
        """Return list of ancestor projects from root to direct parent.

        Returns:
            Ordered list starting from the root project, ending with the
            direct parent (empty list for top-level projects).
        """
        ancestors: list[Project] = []
        current = self.parent
        while current is not None:
            ancestors.insert(0, current)
            current = current.parent
        return ancestors

    @property
    def effective_default_print_preset(self) -> Optional[OrcaPrintPreset]:
        """Return the effective default print preset, traversing parent projects.

        If this project has no ``default_print_preset`` set, the parent
        hierarchy is walked upward until a preset is found or the root
        project is reached.

        Returns:
            The first ``default_print_preset`` found in the hierarchy,
            or ``None`` if no ancestor has one set.
        """
        if self.default_print_preset_id is not None:
            return self.default_print_preset
        current = self.parent
        while current is not None:
            if current.default_print_preset_id is not None:
                return current.default_print_preset
            current = current.parent
        return None

    @property
    def effective_default_print_preset_id(self) -> Optional[int]:
        """Return the effective default print preset ID, traversing parent projects.

        Returns:
            The first ``default_print_preset_id`` found in the hierarchy,
            or ``None`` if no ancestor has one set.
        """
        if self.default_print_preset_id is not None:
            return self.default_print_preset_id
        current = self.parent
        while current is not None:
            if current.default_print_preset_id is not None:
                return current.default_print_preset_id
            current = current.parent
        return None

    def get_descendant_ids(self) -> set[int]:
        """Return set of IDs for all descendant projects (recursive).

        Used to prevent circular parent references when editing a project.

        Returns:
            Set of project PKs that are descendants of this project.
        """
        ids: set[int] = set()
        for sub in self.subprojects.all():
            ids.add(sub.pk)
            ids |= sub.get_descendant_ids()
        return ids

    def _collect_parts_with_multiplier(self, multiplier: int = 1) -> list[tuple[Part, int]]:
        """Collect all parts recursively with their effective quantity multiplier.

        Traverses the sub-project tree and accumulates the product of all
        ancestor ``quantity`` values so that filament/part counts at any
        level reflect how many times that sub-project is actually used.

        Args:
            multiplier: Accumulated parent quantity factor (default 1 for
                the project itself).

        Returns:
            List of ``(part, effective_multiplier)`` tuples.
        """
        result = [(part, multiplier) for part in self.parts.all()]
        for subproject in self.subprojects.all():
            result.extend(subproject._collect_parts_with_multiplier(multiplier * subproject.quantity))
        return result

    @property
    def total_parts_count(self) -> int:
        """Total number of individual parts needed (sum of all part quantities, including sub-projects)."""
        return sum(p.quantity * mult for p, mult in self._collect_parts_with_multiplier())

    @property
    def printed_parts_count(self) -> int:
        """Total number of parts already printed to completion (including sub-projects)."""
        return sum(p.printed_quantity * mult for p, mult in self._collect_parts_with_multiplier())

    @property
    def progress_percent(self) -> int:
        """Project completion percentage (0-100)."""
        total = self.total_parts_count
        if total == 0:
            return 0
        return int(self.printed_parts_count / total * 100)

    # Project-level aggregated status constants
    STATUS_EMPTY = "empty"
    STATUS_ERROR = "error"
    STATUS_ESTIMATING = "estimating"
    STATUS_COMPLETE = "complete"
    STATUS_IN_PROGRESS = "in_progress"
    STATUS_READY = "ready"
    STATUS_PENDING = "pending"

    @property
    def aggregated_status(self) -> str:
        """Compute an aggregated status from all parts and sub-projects.

        Status priority (highest to lowest):
        - ``error``: at least one part has an estimation error
        - ``estimating``: at least one part is currently being estimated
        - ``complete``: all parts have been printed
        - ``in_progress``: at least one part has been printed
        - ``ready``: all parts have filament estimates, none printed yet
        - ``pending``: parts exist but estimates are missing / not started
        - ``empty``: no parts in the project (and no sub-projects with parts)

        Returns:
            One of the STATUS_* constants.
        """
        parts_with_mult = self._collect_parts_with_multiplier()
        if not parts_with_mult:
            return self.STATUS_EMPTY

        has_error = False
        has_estimating = False
        all_complete = True
        any_printed = False
        all_estimated = True

        for part, _mult in parts_with_mult:
            if part.estimation_status == Part.ESTIMATION_ERROR:
                has_error = True
            if part.estimation_status in (
                Part.ESTIMATION_PENDING,
                Part.ESTIMATION_ESTIMATING,
            ):
                has_estimating = True
            if not part.is_complete:
                all_complete = False
            if part.printed_quantity > 0:
                any_printed = True
            if not part.filament_used_grams:
                all_estimated = False

        if has_error:
            return self.STATUS_ERROR
        if has_estimating:
            return self.STATUS_ESTIMATING
        if all_complete:
            return self.STATUS_COMPLETE
        if any_printed:
            return self.STATUS_IN_PROGRESS
        if all_estimated:
            return self.STATUS_READY
        return self.STATUS_PENDING

    @property
    def aggregated_status_display(self) -> str:
        """Human-readable label for the aggregated status.

        Returns:
            Display string for the current aggregated_status value.
        """
        return {
            self.STATUS_EMPTY: "Empty",
            self.STATUS_ERROR: "Error",
            self.STATUS_ESTIMATING: "Estimating",
            self.STATUS_COMPLETE: "Complete",
            self.STATUS_IN_PROGRESS: "In Progress",
            self.STATUS_READY: "Ready",
            self.STATUS_PENDING: "Pending",
        }.get(self.aggregated_status, "Unknown")

    @property
    def total_filament_grams(self) -> float:
        """Total filament required for all parts in the project (grams), including sub-projects."""
        return sum(
            p.filament_used_grams * p.quantity * mult
            for p, mult in self._collect_parts_with_multiplier()
            if p.filament_used_grams
        )

    @property
    def total_filament_meters(self) -> float:
        """Total filament required for all parts in the project (meters), including sub-projects."""
        return sum(
            p.filament_used_meters * p.quantity * mult
            for p, mult in self._collect_parts_with_multiplier()
            if p.filament_used_meters
        )

    def filament_requirements(self) -> list[dict]:
        """Per-filament-type breakdown of total and remaining filament needs.

        Groups parts (including those from sub-projects, scaled by the
        sub-project ``quantity``) by ``spoolman_filament_id`` and calculates
        how much filament is needed in total and how much is still remaining.

        Returns:
            List of dicts with keys: filament_id, filament_name, color,
            material, total_grams, total_meters, remaining_grams,
            remaining_meters, parts (list of contributing parts).
        """
        from collections import defaultdict

        # (part, effective_multiplier) — multiplier accounts for sub-project quantity
        part_mults = self._collect_parts_with_multiplier()

        buckets: dict[Optional[int], list[tuple]] = defaultdict(list)
        for part, mult in part_mults:
            buckets[part.spoolman_filament_id].append((part, mult))

        # Resolve filament names from SpoolmanFilamentMapping
        filament_ids = [fid for fid in buckets if fid is not None]
        mapping_lookup: dict[int, SpoolmanFilamentMapping] = {}
        if filament_ids:
            for m in SpoolmanFilamentMapping.objects.filter(
                spoolman_filament_id__in=filament_ids,
            ):
                mapping_lookup[m.spoolman_filament_id] = m

        results = []
        for filament_id, pm_list in buckets.items():
            mapping = mapping_lookup.get(filament_id) if filament_id else None
            filament_name = ""
            if mapping and mapping.spoolman_filament_name:
                filament_name = mapping.spoolman_filament_name

            total_g = sum((p.filament_used_grams or 0) * p.quantity * mult for p, mult in pm_list)
            total_m = sum((p.filament_used_meters or 0) * p.quantity * mult for p, mult in pm_list)
            remaining_g = sum((p.filament_used_grams or 0) * p.remaining_quantity * mult for p, mult in pm_list)
            remaining_m = sum((p.filament_used_meters or 0) * p.remaining_quantity * mult for p, mult in pm_list)

            # Collect material from parts and color from mapping (single source of truth)
            parts = [p for p, _ in pm_list]
            materials = sorted({p.material for p in parts if p.material})

            # Prefer the cached Spoolman color from the mapping over stale Part.color snapshots
            if mapping and mapping.spoolman_color_hex:
                colors = [mapping.spoolman_color_hex]
            else:
                # Fallback: deduplicate from parts (e.g. manually assigned colors)
                colors = sorted({p.color for p in parts if p.color})

            results.append(
                {
                    "filament_id": filament_id,
                    "filament_name": filament_name,
                    "colors": colors,
                    "material": ", ".join(materials) if materials else "—",
                    "total_grams": round(total_g, 1),
                    "total_meters": round(total_m, 2),
                    "remaining_grams": round(remaining_g, 1),
                    "remaining_meters": round(remaining_m, 2),
                    "parts": parts,
                    "has_estimates": any(p.filament_used_grams for p in parts),
                }
            )

        # Sort: filaments with names first, then unnamed, then unassigned
        results.sort(
            key=lambda r: (
                r["filament_id"] is None,
                not r["filament_name"],
                r["filament_name"],
            )
        )
        return results

    # ------------------------------------------------------------------
    # Document & hardware aggregation
    # ------------------------------------------------------------------

    def _collect_documents(self) -> list[tuple[ProjectDocument, Project]]:
        """Recursively collect all documents from this project and sub-projects.

        Returns:
            List of ``(ProjectDocument, project)`` tuples so the template can
            group documents by their owning project using a stable identifier.
        """
        result = [(doc, self) for doc in self.documents.all()]
        for subproject in self.subprojects.all():
            result.extend(subproject._collect_documents())
        return result

    def _collect_hardware_with_multiplier(
        self,
        multiplier: int = 1,
    ) -> list[tuple[ProjectHardware, int]]:
        """Recursively collect hardware assignments with quantity multiplier.

        Works identically to :meth:`_collect_parts_with_multiplier` but
        for :class:`ProjectHardware` records.

        Args:
            multiplier: Accumulated parent quantity factor.

        Returns:
            List of ``(ProjectHardware, effective_multiplier)`` tuples.
        """
        result = [(hw, multiplier) for hw in self.hardware_assignments.select_related("hardware_part").all()]
        for subproject in self.subprojects.all():
            result.extend(subproject._collect_hardware_with_multiplier(multiplier * subproject.quantity))
        return result

    @property
    def total_hardware_cost(self) -> float:
        """Total hardware cost across all assignments including sub-projects.

        Items without a ``unit_price`` are silently skipped.
        """
        total = 0.0
        for hw, mult in self._collect_hardware_with_multiplier():
            if hw.hardware_part.unit_price is not None:
                total += float(hw.hardware_part.unit_price) * hw.quantity * mult
        return round(total, 2)

    def hardware_requirements(self) -> list[dict]:
        """Aggregate hardware needs across sub-projects, grouped by part.

        Returns:
            List of dicts with keys: hardware_part, total_quantity,
            total_price, projects (list of contributing project names).
        """
        from collections import defaultdict

        hw_mults = self._collect_hardware_with_multiplier()

        buckets: dict[int, dict] = defaultdict(lambda: {"hardware_part": None, "total_quantity": 0, "projects": []})

        for hw, mult in hw_mults:
            key = hw.hardware_part_id
            bucket = buckets[key]
            bucket["hardware_part"] = hw.hardware_part
            bucket["total_quantity"] += hw.quantity * mult
            bucket["projects"].append(hw.project.name)

        results = []
        for bucket in buckets.values():
            hp = bucket["hardware_part"]
            total_qty = bucket["total_quantity"]
            total_price = round(float(hp.unit_price) * total_qty, 2) if hp.unit_price is not None else None
            results.append(
                {
                    "hardware_part": hp,
                    "total_quantity": total_qty,
                    "total_price": total_price,
                    "projects": sorted(set(bucket["projects"])),
                }
            )

        results.sort(key=lambda r: (r["hardware_part"].category, r["hardware_part"].name))
        return results
