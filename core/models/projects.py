"""Project model for the LayerNexus application."""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator
from django.db import models
from django.db.models import CheckConstraint, Q, QuerySet

from core.models.parts import Part
from core.models.spoolman import SpoolmanFilamentMapping

if TYPE_CHECKING:
    from core.models.documents import ProjectDocument
    from core.models.hardware import ProjectHardware
    from core.models.orca_profiles import OrcaPrintPreset
    from core.models.printing import PrintJob


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
        on_delete=models.CASCADE,
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
                condition=Q(quantity__gte=1),
                name="project_quantity_gte_1",
            ),
        ]
        permissions = [
            ("can_manage_projects", "Can create, edit, and delete projects"),
        ]

    def __str__(self) -> str:
        return self.name

    def save(self, *args, **kwargs) -> None:
        """Persist the project, refusing to store a cyclic ``parent``.

        Django never calls :meth:`clean` implicitly, so the cycle check is run
        here too — a ``project.parent = descendant; project.save()`` from a shell
        or import raises :class:`ValidationError` instead of persisting a cycle
        that would later blow up the recursive aggregate properties.
        """
        self._assert_parent_acyclic()
        super().save(*args, **kwargs)

    def clean(self) -> None:
        """Validate that the parent assignment does not create a cycle."""
        super().clean()
        self._assert_parent_acyclic()

    def _assert_parent_acyclic(self) -> None:
        """Raise :class:`ValidationError` if ``parent`` puts this project in a cycle.

        Walks the ``parent`` chain upward; if this project is encountered anywhere
        in its own ancestry the assignment would create a cycle. A visited-set
        guard makes the walk terminate even if a corrupt cycle already exists in
        the database.

        Limitation: this is an application-level check and therefore not fully
        concurrency-safe — two simultaneous re-parent transactions (A→B and B→A)
        can each pass the check on stale reads and commit a cycle. Fully
        preventing that would need DB-level enforcement (serializable isolation /
        row locks / a recursive-CTE constraint). The upward walks
        (:meth:`ancestors`, :meth:`effective_default_print_preset`) and
        :meth:`get_descendant_ids` carry a visited-set guard; the recursive
        ``_collect_*`` aggregators do not, so a raced cycle surfaces as a
        ``RecursionError`` (HTTP 500) rather than a hang.
        """
        if self.parent_id is None:
            return

        visited: set[int] = set()
        ancestor = self.parent
        while ancestor is not None:
            if self.pk is not None and ancestor.pk == self.pk:
                raise ValidationError(
                    {"parent": "A project cannot be a sub-project of itself or one of its descendants."}
                )
            if ancestor.pk in visited:
                break
            visited.add(ancestor.pk)
            ancestor = ancestor.parent

    @property
    def is_subproject(self) -> bool:
        """Return True if this project has a parent project."""
        return self.parent_id is not None

    def ancestors(self) -> list[Project]:
        """Return the ancestor chain of this project, root first, excluding self.

        A visited-set guard makes the walk terminate even on a corrupt cycle.

        Returns:
            Ancestor projects ordered from the root down to the direct parent.
        """
        chain: list[Project] = []
        visited: set[int] = {self.pk} if self.pk is not None else set()
        current = self.parent
        while current is not None and current.pk not in visited:
            visited.add(current.pk)
            chain.append(current)
            current = current.parent
        chain.reverse()
        return chain

    @property
    def root(self) -> Project:
        """Return the top-level project of this project's tree (self for roots)."""
        chain = self.ancestors()
        return chain[0] if chain else self

    @property
    def path_label(self) -> str:
        """Return the project path from the root down to self, joined by ``" › "``."""
        return " › ".join(p.name for p in [*self.ancestors(), self])

    def direct_part_count(self) -> int:
        """Return the number of parts directly in this project."""
        return self.parts.count()

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
        visited: set[int] = set()
        current = self.parent
        while current is not None and current.pk not in visited:
            visited.add(current.pk)
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
        visited: set[int] = set()
        current = self.parent
        while current is not None and current.pk not in visited:
            visited.add(current.pk)
            if current.default_print_preset_id is not None:
                return current.default_print_preset_id
            current = current.parent
        return None

    #: Relation chain that carries everything the aggregate properties need for one node
    #: (its parts and each part's completed-plate job info).
    _AGGREGATE_PART_LEAF = "parts__job_entries__print_job__plates"

    @classmethod
    def aggregate_prefetch_lookups(cls, depth: int = 3) -> list[str]:
        """Prefetch lookups that keep the tree aggregate properties query-flat.

        Returns the ``prefetch_related`` arguments a list/detail view should use
        so that ``total_parts_count``, ``progress_percent``, ``aggregated_status``
        and ``total_filament_grams`` traverse the project tree entirely from
        cache instead of issuing a query per node/part.

        Covers, up to ``depth`` levels of ``subprojects`` nesting, the part leaf
        (``parts__job_entries__print_job__plates``) and the ``subprojects`` relation.
        Levels below ``depth`` degrade gracefully to lazy queries.

        Args:
            depth: Number of sub-project levels to cover (root counts as 0).

        Returns:
            List of ``prefetch_related`` lookup strings.
        """
        lookups: list[str] = []
        prefix = ""
        for _ in range(depth + 1):
            lookups.append(f"{prefix}{cls._AGGREGATE_PART_LEAF}")
            lookups.append(f"{prefix}subprojects")
            prefix += "subprojects__"
        return lookups

    @classmethod
    def hardware_prefetch_lookups(cls, depth: int = 3) -> list[str]:
        """Prefetch lookups for walking only the project tree (no parts/print jobs).

        Lighter alternative to :meth:`aggregate_prefetch_lookups` for callers that
        only aggregate hardware, such as the shopping list.

        Args:
            depth: Number of sub-project levels to cover (root counts as 0).

        Returns:
            List of ``prefetch_related`` lookup strings.
        """
        return ["subprojects" + "__subprojects" * level for level in range(depth + 1)]

    def get_descendant_ids(self) -> set[int]:
        """Return the set of PKs of all descendant projects.

        Walks ``subprojects`` iteratively; a visited-set guard makes a corrupt
        persisted cycle terminate.

        Returns:
            Set of project PKs below this project in the tree.
        """
        ids: set[int] = set()
        stack: list[Project] = [self]
        while stack:
            for sub in stack.pop().subprojects.all():
                if sub.pk in ids or sub.pk == self.pk:
                    continue
                ids.add(sub.pk)
                stack.append(sub)
        return ids

    def subtree_ids(self) -> set[int]:
        """Return the PKs of this project and all of its descendants."""
        return {self.pk} | self.get_descendant_ids()

    def blocking_jobs(self) -> QuerySet[PrintJob]:
        """Return unfinished print jobs that contain a part of this project's subtree.

        A project cannot be deleted while any of these exist.

        Returns:
            Distinct queryset of :class:`~core.models.printing.PrintJob` rows whose
            status is not in :attr:`PrintJob.TERMINAL_STATUSES`.
        """
        from core.models.printing import PrintJob

        return (
            PrintJob.objects.filter(job_parts__part__project_id__in=self.subtree_ids())
            .exclude(status__in=PrintJob.TERMINAL_STATUSES)
            .distinct()
        )

    def deletion_summary(self) -> dict[str, int]:
        """Count what deleting this project would remove along with it.

        Returns:
            Dict with keys ``subprojects``, ``parts``, ``documents``, ``hardware`` and
            ``print_history`` (the ``PrintJobPart`` rows of the subtree's parts).
        """
        from core.models.documents import ProjectDocument
        from core.models.hardware import ProjectHardware
        from core.models.printing import PrintJobPart

        ids = self.subtree_ids()
        return {
            "subprojects": len(ids) - 1,
            "parts": Part.objects.filter(project_id__in=ids).count(),
            "documents": ProjectDocument.objects.filter(project_id__in=ids).count(),
            "hardware": ProjectHardware.objects.filter(project_id__in=ids).count(),
            "print_history": PrintJobPart.objects.filter(part__project_id__in=ids).count(),
        }

    def _collect_parts_with_multiplier(self, multiplier: int = 1) -> list[tuple[Part, int]]:
        """Collect all parts of the subtree with their needed count.

        Each part appears exactly once, with ``multiplier × part.quantity`` where the
        multiplier is the product of the sub-project quantities from this project down
        to the part's project.

        Args:
            multiplier: Outer quantity factor applied to every collected part.

        Returns:
            List of ``(part, needed_count)`` tuples.
        """
        result: list[tuple[Part, int]] = [(part, multiplier * part.quantity) for part in self.parts.all()]
        for sub in self.subprojects.all():
            result.extend(sub._collect_parts_with_multiplier(multiplier * sub.quantity))
        return result

    @property
    def total_parts_count(self) -> int:
        """Total number of individual parts needed, including sub-projects."""
        return sum(needed for _p, needed in self._collect_parts_with_multiplier())

    def build_progress(self) -> dict:
        """Per-part print progress of the subtree.

        ``needed`` per part is its tree multiplier count; ``printed`` is
        :attr:`Part.printed_quantity` (all completed jobs). Each part is capped at
        its need, so over-printing one part never pushes the project past 100%.

        Returns:
            Dict with keys ``percent`` (int 0–100), ``needed`` (int), ``printed``
            (int) and ``parts`` (list of ``{part, needed, printed, remaining}`` dicts).
        """
        rows: list[dict] = []
        total_needed = 0
        total_printed = 0
        for part, needed in self._collect_parts_with_multiplier():
            printed = part.printed_quantity
            total_needed += needed
            total_printed += min(printed, needed)
            rows.append(
                {
                    "part": part,
                    "needed": needed,
                    "printed": printed,
                    "remaining": max(0, needed - printed),
                }
            )
        percent = int(total_printed / total_needed * 100) if total_needed else 0
        return {"percent": percent, "needed": total_needed, "printed": total_printed, "parts": rows}

    @property
    def printed_parts_count(self) -> int:
        """Total number of parts printed to completion, capped per part at its need."""
        return self.build_progress()["printed"]

    @property
    def progress_percent(self) -> int:
        """Project completion percentage (0-100)."""
        return self.build_progress()["percent"]

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

        The printed/complete signals come from :meth:`build_progress` (all completed
        jobs per part); the estimation signals come from the parts themselves.

        Returns:
            One of the STATUS_* constants.
        """
        parts_with_mult = self._collect_parts_with_multiplier()
        if not parts_with_mult:
            return self.STATUS_EMPTY

        has_error = False
        has_estimating = False
        all_estimated = True

        for part, _mult in parts_with_mult:
            if part.estimation_status == Part.ESTIMATION_ERROR:
                has_error = True
            if part.estimation_status in (
                Part.ESTIMATION_PENDING,
                Part.ESTIMATION_ESTIMATING,
            ):
                has_estimating = True
            if not part.filament_used_grams:
                all_estimated = False

        rows = self.build_progress()["parts"]
        all_complete = bool(rows) and all(row["remaining"] == 0 for row in rows)
        any_printed = any(row["printed"] > 0 for row in rows)

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
            p.filament_used_grams * mult for p, mult in self._collect_parts_with_multiplier() if p.filament_used_grams
        )

    @property
    def total_filament_meters(self) -> float:
        """Total filament required for all parts in the project (meters), including sub-projects."""
        return sum(
            p.filament_used_meters * mult for p, mult in self._collect_parts_with_multiplier() if p.filament_used_meters
        )

    def filament_requirements(self) -> list[dict]:
        """Per-filament-type breakdown of total and remaining filament needs.

        Groups parts (including those from sub-projects, scaled by the sub-project
        quantities) by ``spoolman_filament_id`` and calculates how much filament
        is needed in total and how much is still remaining (``needed − printed``
        per part, from :meth:`build_progress`).

        Returns:
            List of dicts with keys: filament_id, filament_name, color,
            material, total_grams, total_meters, remaining_grams,
            remaining_meters, parts (list of contributing parts).
        """
        from collections import defaultdict

        buckets: dict[Optional[int], list[tuple[Part, int, int]]] = defaultdict(list)
        for row in self.build_progress()["parts"]:
            part = row["part"]
            buckets[part.spoolman_filament_id].append((part, row["needed"], row["remaining"]))

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

            total_g = sum((p.filament_used_grams or 0) * needed for p, needed, _r in pm_list)
            total_m = sum((p.filament_used_meters or 0) * needed for p, needed, _r in pm_list)
            remaining_g = sum((p.filament_used_grams or 0) * remaining for p, _n, remaining in pm_list)
            remaining_m = sum((p.filament_used_meters or 0) * remaining for p, _n, remaining in pm_list)

            # Collect material from parts and color from mapping (single source of truth)
            parts = [p for p, _n, _r in pm_list]
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
        """Recursively collect documents from this project and its sub-projects.

        Returns:
            List of ``(ProjectDocument, project)`` tuples so the template can
            group documents by their owning project using a stable identifier.
        """
        result = [(doc, self) for doc in self.documents.all()]
        for sub in self.subprojects.all():
            result.extend(sub._collect_documents())
        return result

    def _collect_hardware_with_multiplier(self, multiplier: int = 1) -> list[tuple[ProjectHardware, int]]:
        """Recursively collect hardware assignments with the sub-project quantity multiplier.

        Args:
            multiplier: Accumulated parent quantity factor.

        Returns:
            List of ``(ProjectHardware, effective_multiplier)`` tuples.
        """
        result = [(hw, multiplier) for hw in self.hardware_assignments.select_related("hardware_part", "project").all()]
        for sub in self.subprojects.all():
            result.extend(sub._collect_hardware_with_multiplier(multiplier * sub.quantity))
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
            total_price, projects (sorted tree path labels of the contributing
            projects, e.g. ``"Truck › Axle"``).
        """
        from collections import defaultdict

        hw_mults = self._collect_hardware_with_multiplier()

        buckets: dict[int, dict] = defaultdict(lambda: {"hardware_part": None, "total_quantity": 0, "projects": []})

        for hw, mult in hw_mults:
            key = hw.hardware_part_id
            bucket = buckets[key]
            bucket["hardware_part"] = hw.hardware_part
            bucket["total_quantity"] += hw.quantity * mult
            bucket["projects"].append(hw.project.path_label)

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
