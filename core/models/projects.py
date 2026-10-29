"""Project model for the LayerNexus application."""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator
from django.db import models, transaction
from django.db.models import CheckConstraint, Q, QuerySet, prefetch_related_objects

from core.models.file_copy import CopiedFiles, duplicate_field_file
from core.models.parts import Part
from core.models.spoolman import SpoolmanFilamentMapping

if TYPE_CHECKING:
    from django.contrib.auth.models import User

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
        (:meth:`ancestors`, :meth:`effective_default_print_preset`),
        :meth:`get_descendant_ids` and the ``_collect_*`` aggregators (via
        :meth:`_walk_subtree`) carry a visited-set guard, so a raced cycle
        degrades to an odd result rather than a hang.
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

    @classmethod
    def path_label_map(cls) -> dict[int, str]:
        """Return :attr:`path_label` for every project, computed with a single query.

        Used by pickers that list many projects or parts, where calling
        :attr:`path_label` per row would walk the parent chain once per row. A
        visited-set guard makes a corrupt persisted cycle terminate.

        Returns:
            Mapping of project PK to its ``"Root › … › Name"`` path label.
        """
        rows = {pk: (name, parent_id) for pk, name, parent_id in cls.objects.values_list("pk", "name", "parent_id")}
        labels: dict[int, str] = {}
        for pk in rows:
            chain: list[str] = []
            visited: set[int] = set()
            current: int | None = pk
            while current is not None and current in rows and current not in visited:
                visited.add(current)
                name, parent_id = rows[current]
                chain.append(name)
                current = parent_id
            labels[pk] = " › ".join(reversed(chain))
        return labels

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

        Walks the tree level by level with one ``values_list`` query per depth
        (not per node); a visited-set guard makes a corrupt persisted cycle
        terminate.

        Returns:
            Set of project PKs below this project in the tree.
        """
        ids: set[int] = set()
        frontier: set[int] = {self.pk}
        while frontier:
            children = set(Project.objects.filter(parent_id__in=frontier).values_list("pk", flat=True))
            frontier = children - ids - {self.pk}
            ids |= frontier
        return ids

    def subtree_ids(self) -> set[int]:
        """Return the PKs of this project and all of its descendants."""
        return {self.pk} | self.get_descendant_ids()

    def blocking_jobs(self, ids: set[int] | None = None) -> QuerySet[PrintJob]:
        """Return unfinished print jobs that contain a part of this project's subtree.

        A project cannot be deleted while any of these exist.

        Args:
            ids: Precomputed :meth:`subtree_ids`, to avoid walking the tree again.

        Returns:
            Distinct queryset of :class:`~core.models.printing.PrintJob` rows whose
            status is not in :attr:`PrintJob.TERMINAL_STATUSES`.
        """
        from core.models.printing import PrintJob

        return (
            PrintJob.objects.filter(job_parts__part__project_id__in=self.subtree_ids() if ids is None else ids)
            .exclude(status__in=PrintJob.TERMINAL_STATUSES)
            .distinct()
        )

    def deletion_summary(self, ids: set[int] | None = None) -> dict[str, int]:
        """Count what deleting this project would remove along with it.

        Args:
            ids: Precomputed :meth:`subtree_ids`, to avoid walking the tree again.

        Returns:
            Dict with keys ``subprojects``, ``parts``, ``documents``, ``hardware`` and
            ``print_history`` (the ``PrintJobPart`` rows of the subtree's parts).
        """
        from core.models.documents import ProjectDocument
        from core.models.hardware import ProjectHardware
        from core.models.printing import PrintJobPart

        if ids is None:
            ids = self.subtree_ids()
        return {
            "subprojects": len(ids) - 1,
            "parts": Part.objects.filter(project_id__in=ids).count(),
            "documents": ProjectDocument.objects.filter(project_id__in=ids).count(),
            "hardware": ProjectHardware.objects.filter(project_id__in=ids).count(),
            "print_history": PrintJobPart.objects.filter(part__project_id__in=ids).count(),
        }

    #: Suffix of the default name of a copied project.
    COPY_SUFFIX = " (copy)"

    def default_copy_name(self) -> str:
        """Return ``"<name> (copy)"``, shortening the name so it fits ``max_length``."""
        max_length = self._meta.get_field("name").max_length
        return f"{self.name[: max_length - len(self.COPY_SUFFIX)]}{self.COPY_SUFFIX}"

    def deep_copy(
        self,
        *,
        parent: Project | None,
        name: str | None = None,
        created_by: User | None = None,
    ) -> Project:
        """Copy this project and its whole subtree (content, never history).

        Copies every node's description, image (new file), quantity and preset,
        its documents (new files), its hardware assignments (same catalog
        entries) and its parts (see :meth:`Part.deep_copy`). Print history,
        ``FileVersion`` rows and shopping status are not copied. A file missing on
        disk logs a warning: the image or model file of the copy is left empty, and a
        document without its file is skipped.

        When copied as a new top-level project (``parent=None``), the copy gets this
        project's *effective* preset, so the "roots must have a preset" rule holds.

        The files are copied first, *outside* the row transaction, so the SQLite write
        lock is only held for the short row inserts. If anything fails, the files
        written so far are deleted again. (Files are not removed if an *enclosing*
        transaction of the caller rolls back later, or if the process is killed.)

        Args:
            parent: Target parent, or ``None`` to create a new top-level project.
            name: Name of the top copy; defaults to :meth:`default_copy_name`.
                Descendants keep their names.
            created_by: User recorded as creator of every copied node.

        Returns:
            The new top :class:`Project`.

        Raises:
            ValidationError: If ``parent`` is this project or one of its descendants.
        """
        self._assert_copy_target(parent)
        tracker = CopiedFiles()
        try:
            nodes = self._snapshot_subtree()
            top_preset_id = self.effective_default_print_preset_id if parent is None else self.default_print_preset_id
            if parent is not None:
                # Warm the parent chain so the cycle check in save() needs no query under the lock.
                parent.ancestors()
            files = self._copy_subtree_files(nodes, tracker)
            with transaction.atomic():
                # Re-check under the write lock: the tree may have changed meanwhile.
                self._assert_copy_target(parent)
                return self._create_subtree_copy(
                    nodes, files, parent, name or self.default_copy_name(), created_by, top_preset_id
                )
        except Exception:
            tracker.delete_all()
            raise

    def _assert_copy_target(self, parent: Project | None) -> None:
        """Raise :class:`ValidationError` if ``parent`` lies in this project's subtree."""
        if parent is not None and parent.pk in self.subtree_ids():
            raise ValidationError({"parent": "A project cannot be copied into itself or one of its sub-projects."})

    def _snapshot_subtree(self) -> list[tuple[Project, list[ProjectDocument], list[Part]]]:
        """Return every subtree node (parents first) with its documents and parts.

        Walks via the cycle-guarded :meth:`_walk_subtree`, before anything is created.
        Each node's hardware assignments and each part's time estimates are
        prefetched here too, so the row transaction reads no source rows (only the
        target re-check in :meth:`deep_copy` queries under the lock).

        Returns:
            ``(node, documents, parts)`` tuples in depth-first order.
        """
        nodes = [node for node, _factor in self._walk_subtree()]
        prefetch_related_objects(nodes, "documents", "hardware_assignments", "parts__time_estimates")
        return [(node, list(node.documents.all()), list(node.parts.all())) for node in nodes]

    @staticmethod
    def _copy_subtree_files(
        nodes: list[tuple[Project, list[ProjectDocument], list[Part]]],
        tracker: CopiedFiles,
    ) -> dict[tuple[str, int], str | None]:
        """Copy every image, document and model file of the snapshot.

        Args:
            nodes: Output of :meth:`_snapshot_subtree`.
            tracker: Records every new file for cleanup on failure.

        Returns:
            New storage names keyed by ``("image"|"document"|"part", source pk)``;
            ``None`` where the source has no file or it is missing on disk.
        """
        files: dict[tuple[str, int], str | None] = {}
        for node, documents, parts in nodes:
            files["image", node.pk] = duplicate_field_file(node.image, tracker)
            for doc in documents:
                files["document", doc.pk] = duplicate_field_file(doc.file, tracker)
            for part in parts:
                files["part", part.pk] = duplicate_field_file(part.stl_file, tracker)
        return files

    def _create_subtree_copy(
        self,
        nodes: list[tuple[Project, list[ProjectDocument], list[Part]]],
        files: dict[tuple[str, int], str | None],
        parent: Project | None,
        name: str,
        created_by: User | None,
        top_preset_id: int | None,
    ) -> Project:
        """Insert the rows of the copied subtree; the caller provides the transaction.

        Args:
            nodes: Output of :meth:`_snapshot_subtree` (each node after its parent).
            files: Output of :meth:`_copy_subtree_files`.
            parent: Target parent of the top copy, or ``None`` for a new root.
            name: Name of the top copy.
            created_by: User recorded as creator of every copied node.
            top_preset_id: Preset of the top copy (resolved before the transaction).

        Returns:
            The new top :class:`Project`.
        """
        from core.models.documents import ProjectDocument
        from core.models.hardware import ProjectHardware

        copies: dict[int, Project] = {}
        for node, documents, parts in nodes:
            is_top = node.pk == self.pk
            preset_id = top_preset_id if is_top else node.default_print_preset_id
            copy = Project.objects.create(
                name=name if is_top else node.name,
                description=node.description,
                image=files["image", node.pk],
                parent=parent if is_top else copies[node.parent_id],
                # A new root has no parent to be needed in: its quantity is reset to 1.
                quantity=1 if is_top and parent is None else node.quantity,
                default_print_preset_id=preset_id,
                created_by=created_by,
            )
            copies[node.pk] = copy
            for doc in documents:
                doc_file = files["document", doc.pk]
                if doc_file is None:
                    # Nothing to copy: a document row without its file would only 500 on download.
                    continue
                ProjectDocument.objects.create(
                    project=copy, name=doc.name, file=doc_file, uploaded_by_id=doc.uploaded_by_id
                )
            hardware = [
                ProjectHardware(
                    project=copy, hardware_part_id=hw.hardware_part_id, quantity=hw.quantity, notes=hw.notes
                )
                for hw in node.hardware_assignments.all()
            ]
            if hardware:
                ProjectHardware.objects.bulk_create(hardware)
            for part in parts:
                part._create_copy(copy, files["part", part.pk])
        return copies[self.pk]

    def _walk_subtree(self, multiplier: int = 1) -> list[tuple[Project, int]]:
        """Return every node of the subtree (self first, depth-first) with its multiplier.

        The multiplier of a node is ``multiplier`` times the quantities of all
        sub-projects from this project down to (and including) that node. A
        visited-set guard skips a node already seen, so a corrupt persisted cycle
        terminates instead of recursing forever.

        Args:
            multiplier: Factor of this project itself.

        Returns:
            List of ``(project, multiplier)`` tuples in depth-first order.
        """
        nodes: list[tuple[Project, int]] = []
        visited: set[int] = set()
        stack: list[tuple[Project, int]] = [(self, multiplier)]
        while stack:
            node, factor = stack.pop()
            if node.pk in visited:
                continue
            visited.add(node.pk)
            nodes.append((node, factor))
            stack.extend((sub, factor * sub.quantity) for sub in reversed(list(node.subprojects.all())))
        return nodes

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
        return [
            (part, factor * part.quantity)
            for node, factor in self._walk_subtree(multiplier)
            for part in node.parts.all()
        ]

    @property
    def total_parts_count(self) -> int:
        """Total number of individual parts needed, including sub-projects.

        For a sub-project this is its need for the whole tree (see
        :meth:`chain_multiplier`), consistent with :meth:`build_progress`.
        """
        return sum(needed for _p, needed in self._collect_parts_with_multiplier(self.chain_multiplier()))

    def chain_multiplier(self) -> int:
        """Return how many instances of this project the whole tree needs.

        ``1`` for a top-level project; for a sub-project the product of its own
        ``quantity`` and the quantities of every sub-project above it (the root's
        quantity is ignored).
        """
        if self.parent_id is None:
            return 1
        factor = self.quantity
        for ancestor in self.ancestors()[1:]:
            factor *= ancestor.quantity
        return factor

    def build_progress(self) -> dict:
        """Per-part print progress of the subtree.

        ``needed`` per part is its count for the **whole tree** (part quantity ×
        every sub-project quantity up to the root, see :meth:`chain_multiplier`),
        because ``printed`` is :attr:`Part.printed_quantity` — all completed jobs of
        the part, which serve the whole build. Each part is capped at its need, so
        over-printing one part never pushes the project past 100%.

        Returns:
            Dict with keys ``percent`` (int 0–100), ``needed`` (int), ``printed``
            (int) and ``parts`` (list of ``{part, needed, printed, remaining}`` dicts).
        """
        rows: list[dict] = []
        total_needed = 0
        total_printed = 0
        for part, needed in self._collect_parts_with_multiplier(self.chain_multiplier()):
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
        """Total filament (grams) for all parts incl. sub-projects, scaled like :attr:`total_parts_count`."""
        return sum(
            p.filament_used_grams * mult
            for p, mult in self._collect_parts_with_multiplier(self.chain_multiplier())
            if p.filament_used_grams
        )

    @property
    def total_filament_meters(self) -> float:
        """Total filament (meters) for all parts incl. sub-projects, scaled like :attr:`total_parts_count`."""
        return sum(
            p.filament_used_meters * mult
            for p, mult in self._collect_parts_with_multiplier(self.chain_multiplier())
            if p.filament_used_meters
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
        return [(doc, node) for node, _factor in self._walk_subtree() for doc in node.documents.all()]

    def _collect_hardware_with_multiplier(self, multiplier: int = 1) -> list[tuple[ProjectHardware, int]]:
        """Recursively collect hardware assignments with the sub-project quantity multiplier.

        Args:
            multiplier: Accumulated parent quantity factor.

        Returns:
            List of ``(ProjectHardware, effective_multiplier)`` tuples.
        """
        result: list[tuple[ProjectHardware, int]] = []
        for node, factor in self._walk_subtree(multiplier):
            for hw in node.hardware_assignments.select_related("hardware_part").all():
                # Reuse the walked node (its parent chain is already cached) so
                # ``hw.project.path_label`` costs no extra query per sub-project.
                hw.project = node
                result.append((hw, factor))
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
