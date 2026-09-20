"""Project model for the LayerNexus application."""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator
from django.db import models, transaction
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
        or import raises :class:`ValidationError` instead of persisting a graph
        that would later blow up the recursive aggregate properties.

        During the expand phase the legacy ``parent`` FK stays authoritative; this
        also mirrors exactly one ``ProjectComponent`` edge consistent with it so later
        phases can read edges without staleness. Removed in the contract phase.

        The FK write and the edge reconciliation run inside one
        :func:`~django.db.transaction.atomic` block, so a failure in either (e.g. the
        cycle guard) leaves neither applied and the edge never lags the authoritative FK.
        This does not serialize two *concurrent* reparentings of the same child against
        each other — the same bounded, application-level limitation documented on
        :meth:`_assert_parent_acyclic`; the edges self-heal on the next save and via the
        backfill helper, and the whole shim is removed in the contract phase.
        """
        from core.models.composition import ProjectComponent

        with transaction.atomic():
            self._assert_parent_acyclic()
            super().save(*args, **kwargs)
            if self.parent_id is None:
                ProjectComponent.objects.filter(child_project=self).delete()
                return
            ProjectComponent.objects.filter(child_project=self).exclude(parent_project_id=self.parent_id).delete()
            ProjectComponent.objects.update_or_create(
                parent_project_id=self.parent_id,
                child_project=self,
                defaults={"quantity": self.quantity},
            )

    def clean(self) -> None:
        """Validate that the parent assignment does not create a cycle.

        Runs on every ``full_clean()`` path — ``ProjectEditForm``, the admin,
        etc. The same check is also enforced in :meth:`save` so a bare
        shell/import ``save()`` cannot persist a cycle either. The visited-set
        guards on the recursive traversals (:meth:`get_descendant_ids` and the
        ``_collect_*`` aggregators) remain as a runtime safety net for any cycle
        that somehow reaches the database (e.g. a raw SQL / bulk ``update``).
        """
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
        row locks / a recursive-CTE constraint). The consequence is bounded, not
        catastrophic: **every** parent-chain traversal in this model
        (:meth:`get_ancestors`, :meth:`get_descendant_ids`,
        :meth:`effective_default_print_preset`, the ``_collect_*`` aggregators)
        carries a visited-set guard, so a raced cycle degrades to a logically
        odd graph rather than an infinite loop / ``RecursionError`` at render.
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
        """Return True if this project is a sub-project of another project."""
        return self.parent_id is not None

    def get_ancestors(self) -> list[Project]:
        """Return list of ancestor projects from root to direct parent.

        Returns:
            Ordered list starting from the root project, ending with the
            direct parent (empty list for top-level projects).
        """
        ancestors: list[Project] = []
        visited: set[int] = set()
        current = self.parent
        while current is not None and current.pk not in visited:
            visited.add(current.pk)
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

    #: Relation that carries everything ``aggregated_status`` / ``progress_percent``
    #: need for one project node (parts, their completed-plate job info).
    _AGGREGATE_PART_LEAF = "parts__job_entries__print_job__plates"

    @classmethod
    def aggregate_prefetch_lookups(cls, depth: int = 3) -> list[str]:
        """Prefetch lookups that make the recursive aggregate properties query-flat.

        Returns the ``prefetch_related`` arguments a list/detail view should use
        so that ``total_parts_count``, ``progress_percent``, ``aggregated_status``
        and ``total_filament_grams`` traverse the sub-project tree entirely from
        cache instead of issuing a query per node/part (the N+1 that made project
        lists with status badges slow).

        The self-referential ``subprojects`` relation cannot be prefetched to
        unbounded depth, so the tree is covered up to ``depth`` levels — deep
        enough for realistic project nesting; levels below that degrade
        gracefully to lazy queries (never worse than before).

        Args:
            depth: Number of sub-project levels to cover (root counts as 0).

        Returns:
            List of ``prefetch_related`` lookup strings.
        """
        lookups: list[str] = []
        prefix = ""
        for _ in range(depth + 1):
            lookups.append(f"{prefix}{cls._AGGREGATE_PART_LEAF}")
            lookups.append(f"{prefix}subprojects".rstrip("_"))
            prefix += "subprojects__"
        return lookups

    def get_descendant_ids(self, _path: set[int] | None = None) -> set[int]:
        """Return the set of PKs of all descendant projects over the composition DAG.

        Traverses ``child_links`` (composition edges) instead of the legacy
        ``subprojects`` FK; a path-local guard makes a corrupt persisted cycle
        terminate. Shared modules reached via multiple paths appear once in the set.

        Args:
            _path: PKs on the current recursion stack (path-local cycle guard) so a
                corrupt cycle already persisted in the database (e.g. inserted outside
                model validation) terminates instead of raising ``RecursionError``.

        Returns:
            Set of project PKs that are descendants of this project.
        """
        if _path is None:
            _path = set()
        if self.pk in _path:
            return set()
        next_path = _path | {self.pk}
        ids: set[int] = set()
        for edge in self.child_links.all():
            child = edge.child_project
            ids.add(child.pk)
            ids |= child.get_descendant_ids(next_path)
        return ids

    def _expand_parts_relative(
        self,
        _path: set[int],
        _memo: dict[int, list[tuple[Part, int]]],
    ) -> list[tuple[Part, int]]:
        """Return ``[(part, multiplier_relative_to_self)]`` over the composition DAG.

        Traverses ``part_links`` (direct parts) and ``child_links`` (child modules),
        multiplying each edge ``quantity`` along the path. The node itself counts as ×1.
        A per-node ``_memo`` caches the (path-independent, in an acyclic graph) expansion so
        a module shared via several paths is expanded once and scaled per incoming edge.
        ``_path`` guards against a corrupt persisted cycle: a node recurring on its own
        recursion stack contributes nothing further (traversal terminates, degrading
        gracefully — consistent with the Phase-1 corrupt-cycle stance).

        Args:
            _path: PKs on the current recursion stack (path-local cycle guard).
            _memo: Per-node cache of relative expansions, keyed by project PK.

        Returns:
            List of ``(part, multiplier_relative_to_self)`` tuples.
        """
        if self.pk in _memo:
            return _memo[self.pk]
        if self.pk in _path:
            return []
        next_path = _path | {self.pk}
        rel: list[tuple[Part, int]] = [(link.part, link.quantity) for link in self.part_links.all()]
        for edge in self.child_links.all():
            for part, mult in edge.child_project._expand_parts_relative(next_path, _memo):
                rel.append((part, edge.quantity * mult))
        _memo[self.pk] = rel
        return rel

    def _collect_parts_with_multiplier(self, multiplier: int = 1) -> list[tuple[Part, int]]:
        """Collect all parts over the composition DAG with their effective multiplier.

        Reads the Phase-1 composition edges (``part_links``/``child_links``). Each part is
        returned once per distinct path to it, scaled by the product of edge quantities on
        that path times ``multiplier`` — so a building block shared by two assemblies (or
        reached via a diamond) contributes correctly to each.

        Args:
            multiplier: Outer quantity factor applied to every collected part.

        Returns:
            List of ``(part, effective_multiplier)`` tuples.
        """
        rel = self._expand_parts_relative(set(), {})
        return [(part, multiplier * mult) for part, mult in rel]

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

    def _collect_documents(self, _path: set[int] | None = None) -> list[tuple[ProjectDocument, Project]]:
        """Recursively collect documents from this project and its child modules (DAG).

        Traverses ``child_links`` (composition edges) instead of the legacy
        ``subprojects`` FK. A path-local guard makes a corrupt persisted cycle
        terminate; a module shared via several paths contributes its documents once
        per path, matching the parts/hardware collectors.

        Args:
            _path: PKs on the current recursion stack (path-local cycle guard).

        Returns:
            List of ``(ProjectDocument, project)`` tuples so the template can
            group documents by their owning project using a stable identifier.
        """
        if _path is None:
            _path = set()
        if self.pk in _path:
            return []
        next_path = _path | {self.pk}
        result = [(doc, self) for doc in self.documents.all()]
        for edge in self.child_links.all():
            result.extend(edge.child_project._collect_documents(next_path))
        return result

    def _collect_hardware_with_multiplier(
        self,
        multiplier: int = 1,
        _path: set[int] | None = None,
    ) -> list[tuple[ProjectHardware, int]]:
        """Recursively collect hardware assignments over the DAG with quantity multiplier.

        Traverses ``child_links`` (composition edges) instead of the legacy
        ``subprojects`` FK, multiplying each edge ``quantity`` along the path. A
        path-local guard makes a corrupt persisted cycle terminate.

        Args:
            multiplier: Accumulated parent quantity factor.
            _path: PKs on the current recursion stack (path-local cycle guard).

        Returns:
            List of ``(ProjectHardware, effective_multiplier)`` tuples.
        """
        if _path is None:
            _path = set()
        if self.pk in _path:
            return []
        next_path = _path | {self.pk}
        result = [(hw, multiplier) for hw in self.hardware_assignments.select_related("hardware_part").all()]
        for edge in self.child_links.all():
            result.extend(
                edge.child_project._collect_hardware_with_multiplier(multiplier * edge.quantity, next_path)
            )
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
