"""Models for the LayerNexus 3D printing project management application."""

from __future__ import annotations

from typing import Optional

from django.conf import settings
from django.core.validators import MinValueValidator
from django.db import models
from django.db.models import Sum


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


class Part(models.Model):
    """A single part within a project that needs to be printed."""

    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="parts")
    name = models.CharField(
        max_length=255,
        blank=True,
        help_text="Optional — derived from the uploaded filename if left empty.",
    )
    stl_file = models.FileField(upload_to="stl_files/", blank=True, null=True)
    quantity = models.PositiveIntegerField(default=1, validators=[MinValueValidator(1)])
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


class PrintJob(models.Model):
    """Tracks a print job that may contain one or more parts.

    Parts are linked via the ``PrintJobPart`` through model.  The slicing
    lifecycle is tracked here.  After slicing, each plate of G-code is
    stored on a ``PrintJobPlate`` child object.
    """

    STATUS_DRAFT = "draft"
    STATUS_PENDING = "pending"
    STATUS_SLICING = "slicing"
    STATUS_SLICED = "sliced"
    STATUS_UPLOADING = "uploading"
    STATUS_UPLOADED = "uploaded"
    STATUS_PRINTING = "printing"
    STATUS_COMPLETED = "completed"
    STATUS_FAILED = "failed"
    STATUS_CANCELLED = "cancelled"
    STATUS_CHOICES = [
        (STATUS_DRAFT, "Draft"),
        (STATUS_PENDING, "Pending"),
        (STATUS_SLICING, "Slicing"),
        (STATUS_SLICED, "Sliced"),
        (STATUS_UPLOADING, "Uploading to Printer"),
        (STATUS_UPLOADED, "Uploaded"),
        (STATUS_PRINTING, "Printing"),
        (STATUS_COMPLETED, "Completed"),
        (STATUS_FAILED, "Failed"),
        (STATUS_CANCELLED, "Cancelled"),
    ]

    name = models.CharField(
        max_length=255,
        blank=True,
        help_text="Optional descriptive name for this job",
    )
    parts = models.ManyToManyField(
        Part,
        through="PrintJobPart",
        related_name="print_jobs",
    )
    machine_profile = models.ForeignKey(
        "OrcaMachineProfile",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="print_jobs",
        help_text="Machine profile used for slicing (determines compatible printers)",
    )
    printer = models.ForeignKey(
        PrinterProfile,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="print_jobs",
        help_text="Assigned when the job is added to a print queue",
    )
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_DRAFT)

    # Aggregate print stats (sum of all plates)
    filament_used_grams = models.FloatField(null=True, blank=True)
    print_time_estimate = models.DurationField(null=True, blank=True)

    # Slicing status tracking
    slicing_error = models.TextField(
        blank=True,
        help_text="Error message if slicing failed",
    )
    slicing_started_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="Timestamp when slicing was started",
    )

    notes = models.TextField(blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="print_jobs",
        help_text="User who created this job",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        label = self.name or f"Job #{self.pk}"
        return f"{label} ({self.get_status_display()})"

    # ── Convenience properties ──────────────────────────────────────────

    @property
    def total_part_count(self) -> int:
        """Total number of individual parts in this job (sum of quantities)."""
        return self.job_parts.aggregate(total=Sum("quantity"))["total"] or 0

    @property
    def parts_list(self) -> list[str]:
        """Human-readable list of part names with quantities."""
        return [f"{jp.part.name} ×{jp.quantity}" for jp in self.job_parts.select_related("part")]

    @property
    def is_multi_plate(self) -> bool:
        """Whether this job has more than one plate."""
        return self.plates.count() > 1

    @property
    def all_plates_completed(self) -> bool:
        """Whether all plates of this job have been completed."""
        plates = self.plates.all()
        if not plates.exists():
            return False
        return not plates.exclude(status=PrintJobPlate.STATUS_COMPLETED).exists()

    @property
    def plate_count(self) -> int:
        """Number of plates in this job."""
        return self.plates.count()


class OrcaFilamentProfile(models.Model):
    """Fully resolved OrcaSlicer filament profile.

    Stores the flattened, inheritance-resolved OrcaSlicer filament profile.
    Key settings are exposed as dedicated DB columns for direct querying,
    while all remaining settings are stored in ``extra_settings`` (JSONField).

    Import workflow:
    1. User uploads a JSON profile file.
    2. If the ``inherits`` field is set and the parent profile does not yet
       exist, the profile is stored as *pending*.
    3. When the parent is uploaded and resolved, pending children are
       automatically resolved (inheritance chain merged).
    """

    STATE_PENDING = "pending"
    STATE_RESOLVED = "resolved"
    STATE_CHOICES = [
        (STATE_PENDING, "Pending (needs parent profile)"),
        (STATE_RESOLVED, "Resolved"),
    ]

    # ── Metadata ────────────────────────────────────────────────────────
    name = models.CharField(
        max_length=255,
        help_text="Display name for this profile",
    )
    description = models.TextField(
        blank=True,
        help_text="Supports Markdown formatting.",
    )
    orca_name = models.CharField(
        max_length=255,
        help_text="Original profile name from OrcaSlicer JSON ('name' field)",
        default="",
    )
    state = models.CharField(
        max_length=20,
        choices=STATE_CHOICES,
        default=STATE_PENDING,
        help_text="Whether inheritance has been fully resolved",
    )
    inherits_name = models.CharField(
        max_length=255,
        blank=True,
        help_text="Parent profile name (from 'inherits' field in JSON)",
    )
    setting_id = models.CharField(max_length=50, blank=True)
    instantiation = models.BooleanField(
        default=True,
        help_text="True = selectable profile; False = base/template only",
    )
    renamed_from = models.CharField(
        max_length=255,
        blank=True,
        help_text="Old profile name before OrcaSlicer rename (used for parent lookup)",
    )

    # Raw JSON from the uploaded file (before inheritance resolution)
    uploaded_json = models.JSONField(
        default=dict,
        help_text="Raw JSON content from the uploaded profile file",
    )

    # ── Material identification ─────────────────────────────────────────
    filament_type = models.JSONField(
        default=list,
        blank=True,
        help_text="Material type(s) e.g. PLA, PETG [string[]]",
    )
    filament_vendor = models.JSONField(
        default=list,
        blank=True,
        help_text="Vendor name(s) [string[]]",
    )
    filament_density = models.JSONField(
        default=list,
        blank=True,
        help_text="Density in g/cm³ [float[]]",
    )
    filament_diameter = models.JSONField(
        default=list,
        blank=True,
        help_text="Filament diameter in mm [float[]]",
    )
    filament_cost = models.JSONField(
        default=list,
        blank=True,
        help_text="Price per unit [float[]]",
    )
    filament_flow_ratio = models.JSONField(
        default=list,
        blank=True,
        help_text="Flow ratio [float[]]",
    )
    filament_max_volumetric_speed = models.JSONField(
        default=list,
        blank=True,
        help_text="Max volumetric speed in mm³/s [float[]]",
    )

    # ── Nozzle temperatures ─────────────────────────────────────────────
    nozzle_temperature = models.JSONField(
        default=list,
        blank=True,
        help_text="Nozzle temperature per extruder in °C [int[]]",
    )
    nozzle_temperature_initial_layer = models.JSONField(
        default=list,
        blank=True,
        help_text="First-layer nozzle temperature [int[]]",
    )
    nozzle_temperature_range_low = models.JSONField(
        default=list,
        blank=True,
        help_text="Minimum recommended nozzle temperature [int[]]",
    )
    nozzle_temperature_range_high = models.JSONField(
        default=list,
        blank=True,
        help_text="Maximum recommended nozzle temperature [int[]]",
    )

    # ── Bed temperatures ────────────────────────────────────────────────
    bed_temperature = models.JSONField(
        default=list,
        blank=True,
        help_text="Bed temperature per extruder in °C [int[]]",
    )
    bed_temperature_initial_layer = models.JSONField(
        default=list,
        blank=True,
        help_text="First-layer bed temperature [int[]]",
    )
    hot_plate_temp = models.JSONField(
        default=list,
        blank=True,
        help_text="Hot plate temperature [int[]]",
    )
    hot_plate_temp_initial_layer = models.JSONField(
        default=list,
        blank=True,
        help_text="First-layer hot plate temperature [int[]]",
    )
    cool_plate_temp = models.JSONField(
        default=list,
        blank=True,
        help_text="Cool plate temperature [int[]]",
    )
    cool_plate_temp_initial_layer = models.JSONField(
        default=list,
        blank=True,
        help_text="First-layer cool plate temperature [int[]]",
    )
    temperature_vitrification = models.JSONField(
        default=list,
        blank=True,
        help_text="Softening / glass-transition temperature [int[]]",
    )

    # ── Cooling / fan ───────────────────────────────────────────────────
    fan_min_speed = models.JSONField(
        default=list,
        blank=True,
        help_text="Minimum part-cooling fan speed [float[]]",
    )
    fan_max_speed = models.JSONField(
        default=list,
        blank=True,
        help_text="Maximum part-cooling fan speed [float[]]",
    )
    overhang_fan_speed = models.JSONField(
        default=list,
        blank=True,
        help_text="Overhang / bridge fan speed [int[]]",
    )
    close_fan_the_first_x_layers = models.JSONField(
        default=list,
        blank=True,
        help_text="Number of initial layers with fan off [int[]]",
    )

    # ── Pressure advance ────────────────────────────────────────────────
    pressure_advance = models.JSONField(
        default=list,
        blank=True,
        help_text="Pressure advance / linear advance value [float[]]",
    )
    enable_pressure_advance = models.JSONField(
        default=list,
        blank=True,
        help_text="Enable pressure advance [bool[]]",
    )

    # ── G-code ──────────────────────────────────────────────────────────
    filament_start_gcode = models.JSONField(
        default=list,
        blank=True,
        help_text="Filament start G-code [string[]]",
    )
    filament_end_gcode = models.JSONField(
        default=list,
        blank=True,
        help_text="Filament end G-code [string[]]",
    )

    # ── Material properties ─────────────────────────────────────────────
    filament_soluble = models.JSONField(
        default=list,
        blank=True,
        help_text="Soluble material flag [bool[]]",
    )
    filament_is_support = models.JSONField(
        default=list,
        blank=True,
        help_text="Support material flag [bool[]]",
    )

    # ── Catch-all for remaining settings ────────────────────────────────
    extra_settings = models.JSONField(
        default=dict,
        blank=True,
        help_text="All resolved settings not stored in dedicated columns",
    )

    # ── Ownership & timestamps ──────────────────────────────────────────
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="orca_filament_profiles",
        help_text="User who imported this profile (informational only)",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]
        permissions = [
            (
                "can_manage_orca_profiles",
                "Can import, edit, and delete OrcaSlicer profiles",
            ),
        ]

    def __str__(self) -> str:
        suffix = "" if self.state == self.STATE_RESOLVED else " (pending)"
        return f"{self.name}{suffix}"

    # ── Convenience properties ──────────────────────────────────────────

    @property
    def is_resolved(self) -> bool:
        """Whether inheritance has been fully resolved."""
        return self.state == self.STATE_RESOLVED

    @property
    def first_filament_type(self) -> str | None:
        """First element of the filament_type array (e.g. 'PLA')."""
        if self.filament_type and len(self.filament_type) > 0:
            return str(self.filament_type[0])
        return None

    @property
    def first_nozzle_temperature(self) -> int | None:
        """First nozzle temperature value."""
        if self.nozzle_temperature and len(self.nozzle_temperature) > 0:
            try:
                return int(self.nozzle_temperature[0])
            except (ValueError, TypeError):
                return None
        return None

    @property
    def first_bed_temperature(self) -> int | None:
        """First bed temperature value."""
        if self.bed_temperature and len(self.bed_temperature) > 0:
            try:
                return int(self.bed_temperature[0])
            except (ValueError, TypeError):
                return None
        return None

    @property
    def first_max_volumetric_speed(self) -> float | None:
        """First max volumetric speed value."""
        if self.filament_max_volumetric_speed and len(self.filament_max_volumetric_speed) > 0:
            try:
                return float(self.filament_max_volumetric_speed[0])
            except (ValueError, TypeError):
                return None
        return None

    # ── Import / Resolution ─────────────────────────────────────────────

    @classmethod
    def get_pending_for_parent(cls, parent_name: str) -> models.QuerySet:
        """Return pending profiles waiting for the given parent.

        Args:
            parent_name: The OrcaSlicer profile name of the parent.

        Returns:
            QuerySet of pending OrcaFilamentProfile instances.
        """
        return cls.objects.filter(
            inherits_name=parent_name,
            state=cls.STATE_PENDING,
        )


class SpoolmanFilamentMapping(models.Model):
    """Maps a Spoolman filament type to an OrcaSlicer filament profile.

    Allows automatic profile selection during slicing based on the filament
    assigned to a part via Spoolman.
    """

    spoolman_filament_id = models.IntegerField(
        help_text="Spoolman filament type ID",
    )
    spoolman_filament_name = models.CharField(
        max_length=255,
        blank=True,
        help_text="Cached display name from Spoolman (automatically updated)",
    )
    spoolman_color_hex = models.CharField(
        max_length=7,
        blank=True,
        help_text="Cached hex color from Spoolman (e.g. FF0000), automatically updated",
    )
    orca_filament_profile = models.ForeignKey(
        "OrcaFilamentProfile",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="spoolman_mappings",
        help_text="OrcaSlicer filament profile to use when slicing with this filament type",
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="spoolman_filament_mappings",
        help_text="User who created this mapping (informational only)",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["spoolman_filament_name", "spoolman_filament_id"]
        constraints = [
            models.UniqueConstraint(
                fields=["spoolman_filament_id"],
                name="unique_spoolman_filament_mapping",
            ),
        ]
        permissions = [
            (
                "can_manage_filament_mappings",
                "Can create, edit, and delete filament mappings",
            ),
        ]

    def __str__(self) -> str:
        name = self.spoolman_filament_name or f"Filament #{self.spoolman_filament_id}"
        profile = self.orca_filament_profile.name if self.orca_filament_profile else "—"
        return f"{name} → {profile}"


class OrcaPrintPreset(models.Model):
    """Fully resolved OrcaSlicer process (print) profile.

    Stores the flattened, inheritance-resolved OrcaSlicer process profile.
    Key settings are exposed as dedicated DB columns for direct querying,
    while all remaining settings are stored in ``extra_settings`` (JSONField).

    Import workflow:
    1. User uploads a JSON profile file.
    2. If the ``inherits`` field is set and the parent profile does not yet
       exist, the profile is stored as *pending*.
    3. When the parent is uploaded and resolved, pending children are
       automatically resolved (inheritance chain merged).
    """

    STATE_PENDING = "pending"
    STATE_RESOLVED = "resolved"
    STATE_CHOICES = [
        (STATE_PENDING, "Pending (needs parent profile)"),
        (STATE_RESOLVED, "Resolved"),
    ]

    # ── Metadata ────────────────────────────────────────────────────────
    name = models.CharField(
        max_length=255,
        help_text="Display name for this profile",
    )
    description = models.TextField(
        blank=True,
        help_text="Supports Markdown formatting.",
    )
    orca_name = models.CharField(
        max_length=255,
        help_text="Original profile name from OrcaSlicer JSON ('name' field)",
        default="",
    )
    state = models.CharField(
        max_length=20,
        choices=STATE_CHOICES,
        default=STATE_PENDING,
        help_text="Whether inheritance has been fully resolved",
    )
    inherits_name = models.CharField(
        max_length=255,
        blank=True,
        help_text="Parent profile name (from 'inherits' field in JSON)",
    )
    setting_id = models.CharField(max_length=50, blank=True)
    instantiation = models.BooleanField(
        default=True,
        help_text="True = selectable profile; False = base/template only",
    )
    renamed_from = models.CharField(
        max_length=255,
        blank=True,
        help_text="Old profile name before OrcaSlicer rename (used for parent lookup)",
    )

    # Raw JSON from the uploaded file (before inheritance resolution)
    uploaded_json = models.JSONField(
        default=dict,
        help_text="Raw JSON content from the uploaded profile file",
    )

    # ── Layer height ────────────────────────────────────────────────────
    layer_height = models.FloatField(
        null=True,
        blank=True,
        help_text="Layer height in mm",
    )
    initial_layer_print_height = models.FloatField(
        null=True,
        blank=True,
        help_text="First layer height in mm",
    )

    # ── Line widths ─────────────────────────────────────────────────────
    line_width = models.CharField(
        max_length=20,
        blank=True,
        help_text="Default line width (mm or percentage e.g. '0.45' or '100%')",
    )
    outer_wall_line_width = models.CharField(
        max_length=20,
        blank=True,
        help_text="Outer wall line width",
    )
    inner_wall_line_width = models.CharField(
        max_length=20,
        blank=True,
        help_text="Inner wall line width",
    )
    initial_layer_line_width = models.CharField(
        max_length=20,
        blank=True,
        help_text="First layer line width",
    )
    top_surface_line_width = models.CharField(
        max_length=20,
        blank=True,
        help_text="Top surface line width",
    )
    sparse_infill_line_width = models.CharField(
        max_length=20,
        blank=True,
        help_text="Sparse infill line width",
    )

    # ── Walls ───────────────────────────────────────────────────────────
    wall_loops = models.IntegerField(
        null=True,
        blank=True,
        help_text="Number of wall loops (perimeters)",
    )

    # ── Shell layers ────────────────────────────────────────────────────
    top_shell_layers = models.IntegerField(
        null=True,
        blank=True,
        help_text="Number of top solid layers",
    )
    bottom_shell_layers = models.IntegerField(
        null=True,
        blank=True,
        help_text="Number of bottom solid layers",
    )

    # ── Infill ──────────────────────────────────────────────────────────
    sparse_infill_density = models.CharField(
        max_length=20,
        blank=True,
        help_text="Infill density (percentage string e.g. '15%')",
    )
    sparse_infill_pattern = models.CharField(
        max_length=50,
        blank=True,
        help_text="Infill pattern (grid, gyroid, honeycomb, …)",
    )
    top_surface_pattern = models.CharField(
        max_length=50,
        blank=True,
        help_text="Top surface pattern (monotonic, monotonicline, …)",
    )

    # ── Speeds ──────────────────────────────────────────────────────────
    outer_wall_speed = models.FloatField(
        null=True,
        blank=True,
        help_text="Outer wall speed in mm/s",
    )
    inner_wall_speed = models.FloatField(
        null=True,
        blank=True,
        help_text="Inner wall speed in mm/s",
    )
    sparse_infill_speed = models.FloatField(
        null=True,
        blank=True,
        help_text="Sparse infill speed in mm/s",
    )
    internal_solid_infill_speed = models.FloatField(
        null=True,
        blank=True,
        help_text="Internal solid infill speed in mm/s",
    )
    top_surface_speed = models.FloatField(
        null=True,
        blank=True,
        help_text="Top surface speed in mm/s",
    )
    travel_speed = models.FloatField(
        null=True,
        blank=True,
        help_text="Travel speed in mm/s",
    )
    bridge_speed = models.FloatField(
        null=True,
        blank=True,
        help_text="Bridge speed in mm/s",
    )
    gap_infill_speed = models.FloatField(
        null=True,
        blank=True,
        help_text="Gap infill speed in mm/s",
    )
    initial_layer_speed = models.FloatField(
        null=True,
        blank=True,
        help_text="First layer speed in mm/s",
    )

    # ── Acceleration ────────────────────────────────────────────────────
    default_acceleration = models.FloatField(
        null=True,
        blank=True,
        help_text="Default acceleration in mm/s²",
    )
    outer_wall_acceleration = models.FloatField(
        null=True,
        blank=True,
        help_text="Outer wall acceleration in mm/s²",
    )
    inner_wall_acceleration = models.FloatField(
        null=True,
        blank=True,
        help_text="Inner wall acceleration in mm/s²",
    )
    travel_acceleration = models.FloatField(
        null=True,
        blank=True,
        help_text="Travel acceleration in mm/s²",
    )
    initial_layer_acceleration = models.FloatField(
        null=True,
        blank=True,
        help_text="First layer acceleration in mm/s²",
    )

    # ── Support ─────────────────────────────────────────────────────────
    enable_support = models.BooleanField(
        null=True,
        blank=True,
        help_text="Whether supports are enabled",
    )
    support_type = models.CharField(
        max_length=50,
        blank=True,
        help_text="Support type (normal, tree, …)",
    )
    support_threshold_angle = models.IntegerField(
        null=True,
        blank=True,
        help_text="Support threshold angle in degrees",
    )
    support_style = models.CharField(
        max_length=50,
        blank=True,
        help_text="Support style (default, grid, snug, organic)",
    )

    # ── Brim / adhesion ─────────────────────────────────────────────────
    brim_type = models.CharField(
        max_length=50,
        blank=True,
        help_text="Brim type (no_brim, outer_only, inner_only, outer_and_inner, auto_brim)",
    )
    brim_width = models.FloatField(
        null=True,
        blank=True,
        help_text="Brim width in mm",
    )

    # ── Seam ────────────────────────────────────────────────────────────
    seam_position = models.CharField(
        max_length=50,
        blank=True,
        help_text="Seam position (aligned, nearest, random, back)",
    )

    # ── Quality / detail ────────────────────────────────────────────────
    ironing_type = models.CharField(
        max_length=50,
        blank=True,
        help_text="Ironing type (no_ironing, top_surfaces, topmost_surface, all_solid_layer)",
    )
    detect_overhang_wall = models.BooleanField(
        null=True,
        blank=True,
        help_text="Enable overhang detection for walls",
    )
    elefant_foot_compensation = models.FloatField(
        null=True,
        blank=True,
        help_text="Elephant foot compensation in mm",
    )

    # ── Sequence / multi-object ─────────────────────────────────────────
    print_sequence = models.CharField(
        max_length=50,
        blank=True,
        help_text="Print sequence (by_layer, by_object)",
    )
    enable_prime_tower = models.BooleanField(
        null=True,
        blank=True,
        help_text="Enable prime tower for multi-material",
    )

    # ── Filename format ─────────────────────────────────────────────────
    filename_format = models.CharField(
        max_length=255,
        blank=True,
        help_text="G-code filename format template",
    )

    # ── Catch-all for remaining settings ────────────────────────────────
    extra_settings = models.JSONField(
        default=dict,
        blank=True,
        help_text="All resolved settings not stored in dedicated columns",
    )

    # ── Ownership & timestamps ──────────────────────────────────────────
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="orca_print_presets",
        help_text="User who imported this profile (informational only)",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]

    def __str__(self) -> str:
        suffix = "" if self.state == self.STATE_RESOLVED else " (pending)"
        return f"{self.name}{suffix}"

    # ── Convenience properties ──────────────────────────────────────────

    @property
    def is_resolved(self) -> bool:
        """Whether inheritance has been fully resolved."""
        return self.state == self.STATE_RESOLVED

    @property
    def infill_density_display(self) -> str | None:
        """Human-readable infill density (e.g. '15%')."""
        if self.sparse_infill_density:
            return self.sparse_infill_density
        return None

    @property
    def supports_enabled(self) -> bool:
        """Whether supports are enabled (convenience alias)."""
        return bool(self.enable_support)

    # ── Import / Resolution ─────────────────────────────────────────────

    @classmethod
    def get_pending_for_parent(cls, parent_name: str) -> models.QuerySet:
        """Return pending profiles waiting for the given parent.

        Args:
            parent_name: The OrcaSlicer profile name of the parent.

        Returns:
            QuerySet of pending OrcaPrintPreset instances.
        """
        return cls.objects.filter(
            inherits_name=parent_name,
            state=cls.STATE_PENDING,
        )


class PrintJobPart(models.Model):
    """Through model linking a part to a print job with quantity."""

    print_job = models.ForeignKey(PrintJob, on_delete=models.CASCADE, related_name="job_parts")
    part = models.ForeignKey(Part, on_delete=models.CASCADE, related_name="job_entries")
    quantity = models.PositiveIntegerField(
        default=1,
        validators=[MinValueValidator(1)],
        help_text="How many copies of this part are in the job",
    )

    class Meta:
        unique_together = ["print_job", "part"]

    def __str__(self) -> str:
        return f"{self.part.name} ×{self.quantity} in {self.print_job}"


class PrintJobPlate(models.Model):
    """A single plate (build plate) within a multi-part print job.

    When OrcaSlicer returns multiple plates (ZIP with multiple G-code
    files), each one becomes a PrintJobPlate.  Single-plate jobs still
    get exactly one plate.
    """

    STATUS_WAITING = "waiting"
    STATUS_PRINTING = "printing"
    STATUS_COMPLETED = "completed"
    STATUS_FAILED = "failed"
    STATUS_CHOICES = [
        (STATUS_WAITING, "Waiting"),
        (STATUS_PRINTING, "Printing"),
        (STATUS_COMPLETED, "Completed"),
        (STATUS_FAILED, "Failed"),
    ]

    print_job = models.ForeignKey(PrintJob, on_delete=models.CASCADE, related_name="plates")
    plate_number = models.PositiveIntegerField(
        default=1,
        help_text="Plate number within the job (1-based)",
    )
    gcode_file = models.FileField(upload_to="gcode_jobs/", blank=True, null=True)
    thumbnail = models.ImageField(
        upload_to="plate_thumbnails/",
        blank=True,
        null=True,
        help_text="Preview image extracted from sliced G-code",
    )
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_WAITING)

    # Per-plate stats
    filament_used_grams = models.FloatField(null=True, blank=True)
    print_time_estimate = models.DurationField(null=True, blank=True)

    # Klipper job tracking (per plate)
    klipper_job_id = models.CharField(max_length=255, blank=True)

    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["plate_number"]
        unique_together = ["print_job", "plate_number"]

    def __str__(self) -> str:
        return f"Plate {self.plate_number} of {self.print_job}"


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


class PrintTimeEstimate(models.Model):
    """Historical print time data for calibrating future estimates."""

    part = models.ForeignKey(Part, on_delete=models.CASCADE, related_name="time_estimates")
    printer = models.ForeignKey(PrinterProfile, on_delete=models.CASCADE, related_name="time_estimates")
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


class PrintQueue(models.Model):
    """Multi-printer queue with priority scheduling and job lifecycle tracking.

    Each queue entry represents a single print copy.  The ``status`` field
    tracks where the entry is in the farm workflow:

    * **waiting** – ready to be picked up by a printer.
    * **printing** – G-code has been uploaded & printing has started.
    * **awaiting_review** – print finished, operator must confirm pass/fail.
    """

    # Queue-entry status --------------------------------------------------
    STATUS_WAITING = "waiting"
    STATUS_PRINTING = "printing"
    STATUS_AWAITING_REVIEW = "awaiting_review"
    STATUS_CHOICES = [
        (STATUS_WAITING, "Waiting"),
        (STATUS_PRINTING, "Printing"),
        (STATUS_AWAITING_REVIEW, "Awaiting Review"),
    ]

    PRIORITY_CHOICES = [
        (1, "Low"),
        (2, "Normal"),
        (3, "High"),
        (4, "Urgent"),
    ]

    plate = models.ForeignKey(
        "PrintJobPlate",
        on_delete=models.CASCADE,
        related_name="queue_entries",
        null=True,
        help_text="The specific plate (G-code) to print",
    )
    printer = models.ForeignKey(PrinterProfile, on_delete=models.CASCADE, related_name="queue")
    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default=STATUS_WAITING,
    )
    priority = models.IntegerField(choices=PRIORITY_CHOICES, default=2)
    position = models.PositiveIntegerField(default=0, help_text="Position in the queue (0 = next)")

    # Retry logic ---------------------------------------------------------
    retry_count = models.PositiveIntegerField(
        default=0,
        help_text="Number of times this entry has been retried after failure",
    )
    max_retries = models.PositiveIntegerField(
        default=3,
        help_text="Maximum allowed retries before entry is discarded",
    )

    # Timestamps ----------------------------------------------------------
    added_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True, blank=True, help_text="When printing started")
    completed_at = models.DateTimeField(null=True, blank=True, help_text="When the printer finished (before review)")

    class Meta:
        ordering = ["-priority", "position", "added_at"]
        permissions = [
            ("can_manage_print_queue", "Can add and remove jobs from the print queue"),
            ("can_dequeue_job", "Can remove waiting jobs from the queue"),
        ]

    def __str__(self) -> str:
        if self.plate:
            return (
                f"Queue #{self.position}: Plate {self.plate.plate_number}"
                f" of {self.plate.print_job} [{self.get_priority_display()}]"
            )
        return f"Queue #{self.position}: (no plate) [{self.get_priority_display()}]"

    @property
    def print_job(self) -> PrintJob | None:
        """Convenience accessor for the parent PrintJob."""
        if self.plate:
            return self.plate.print_job
        return None

    @property
    def is_printer_busy(self) -> bool:
        """Return True if this entry's printer has an active or unreviewed job."""
        return (
            PrintQueue.objects.filter(
                printer=self.printer,
                status__in=[self.STATUS_PRINTING, self.STATUS_AWAITING_REVIEW],
            )
            .exclude(pk=self.pk)
            .exists()
        )


class OrcaMachineProfile(models.Model):
    """Fully resolved OrcaSlicer machine (printer) profile.

    Stores the flattened, inheritance-resolved OrcaSlicer machine profile.
    Key settings are exposed as dedicated DB columns for direct querying,
    while all remaining settings are stored in ``extra_settings`` (JSONField).

    Import workflow:
    1. User uploads a JSON profile file.
    2. If the ``inherits`` field is set and the parent profile does not yet
       exist, the profile is stored as *pending*.
    3. When the parent is uploaded and resolved, pending children are
       automatically resolved (inheritance chain merged).
    """

    STATE_PENDING = "pending"
    STATE_RESOLVED = "resolved"
    STATE_CHOICES = [
        (STATE_PENDING, "Pending (needs parent profile)"),
        (STATE_RESOLVED, "Resolved"),
    ]

    # ── Metadata ────────────────────────────────────────────────────────
    name = models.CharField(
        max_length=255,
        help_text="Display name for this profile",
    )
    description = models.TextField(
        blank=True,
        help_text="Supports Markdown formatting.",
    )
    orca_name = models.CharField(
        max_length=255,
        help_text="Original profile name from OrcaSlicer JSON ('name' field)",
    )
    state = models.CharField(
        max_length=20,
        choices=STATE_CHOICES,
        default=STATE_PENDING,
        help_text="Whether inheritance has been fully resolved",
    )
    inherits_name = models.CharField(
        max_length=255,
        blank=True,
        help_text="Parent profile name (from 'inherits' field in JSON)",
    )
    setting_id = models.CharField(max_length=50, blank=True)
    instantiation = models.BooleanField(
        default=True,
        help_text="True = selectable profile; False = base/template only",
    )
    renamed_from = models.CharField(
        max_length=255,
        blank=True,
        help_text="Old profile name before OrcaSlicer rename (used for parent lookup)",
    )

    # Raw JSON from the uploaded file (before inheritance resolution)
    uploaded_json = models.JSONField(
        default=dict,
        help_text="Raw JSON content from the uploaded profile file",
    )

    # ── Key machine settings (populated on resolve) ─────────────────────
    # Printer geometry
    nozzle_diameter = models.JSONField(
        default=list,
        blank=True,
        help_text="Nozzle diameter(s) in mm, per extruder [float[]]",
    )
    printable_area = models.JSONField(
        default=list,
        blank=True,
        help_text="Points defining printable area [point[]]",
    )
    printable_height = models.FloatField(
        null=True,
        blank=True,
        help_text="Maximum printable height in mm",
    )
    bed_shape = models.CharField(
        max_length=50,
        blank=True,
        help_text="Bed shape (e.g. 'rectangular')",
    )
    extruders_count = models.IntegerField(
        null=True,
        blank=True,
        help_text="Number of extruders",
    )

    # Printer identification
    gcode_flavor = models.CharField(
        max_length=50,
        blank=True,
        help_text="G-code flavor (marlin, klipper, reprapfirmware, …)",
    )
    printer_structure = models.CharField(
        max_length=50,
        blank=True,
        help_text="Printer structure (corexy, i3, delta, …)",
    )
    printer_technology = models.CharField(
        max_length=10,
        blank=True,
        help_text="Printer technology (FFF, SLA)",
    )
    printer_model = models.CharField(
        max_length=255,
        blank=True,
        help_text="Printer model/type name",
    )
    printer_variant = models.CharField(
        max_length=50,
        blank=True,
        help_text="Printer variant (often nozzle size, e.g. '0.4')",
    )

    # Motion limits
    machine_max_speed_x = models.IntegerField(null=True, blank=True, help_text="Max X speed (mm/s)")
    machine_max_speed_y = models.IntegerField(null=True, blank=True, help_text="Max Y speed (mm/s)")
    machine_max_speed_z = models.IntegerField(null=True, blank=True, help_text="Max Z speed (mm/s)")
    machine_max_acceleration_x = models.IntegerField(null=True, blank=True, help_text="Max X acceleration (mm/s²)")
    machine_max_acceleration_y = models.IntegerField(null=True, blank=True, help_text="Max Y acceleration (mm/s²)")
    machine_max_acceleration_z = models.IntegerField(null=True, blank=True, help_text="Max Z acceleration (mm/s²)")

    # Retraction (per extruder arrays)
    retraction_length = models.JSONField(
        default=list,
        blank=True,
        help_text="Retraction length per extruder in mm [float[]]",
    )
    retraction_speed = models.JSONField(
        default=list,
        blank=True,
        help_text="Retraction speed per extruder in mm/s [float[]]",
    )
    z_hop = models.JSONField(
        default=list,
        blank=True,
        help_text="Z-hop height per extruder in mm [float[]]",
    )

    # G-code templates
    machine_start_gcode = models.TextField(
        blank=True,
        help_text="Start G-code template",
    )
    machine_end_gcode = models.TextField(
        blank=True,
        help_text="End G-code template",
    )

    # Defaults
    default_bed_type = models.CharField(
        max_length=100,
        blank=True,
        help_text="Default bed type (e.g. 'Textured PEI Plate')",
    )
    default_filament_profile = models.JSONField(
        default=list,
        blank=True,
        help_text="Default filament profile name(s) [string[]]",
    )
    default_print_profile = models.CharField(
        max_length=255,
        blank=True,
        help_text="Default process profile name",
    )

    # Features
    single_extruder_multi_material = models.BooleanField(
        null=True,
        blank=True,
        help_text="Single nozzle multi-material setup",
    )
    use_relative_e_distances = models.BooleanField(
        null=True,
        blank=True,
        help_text="Use relative E distances",
    )
    use_firmware_retraction = models.BooleanField(
        null=True,
        blank=True,
        help_text="Use firmware retraction (G10/G11)",
    )

    # ── Catch-all for remaining settings ────────────────────────────────
    extra_settings = models.JSONField(
        default=dict,
        blank=True,
        help_text="All resolved settings not stored in dedicated columns",
    )

    # ── Ownership & timestamps ──────────────────────────────────────────
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="orca_machine_profiles",
        help_text="User who imported this profile (informational only)",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]

    def __str__(self) -> str:
        suffix = "" if self.state == self.STATE_RESOLVED else " (pending)"
        return f"{self.name}{suffix}"

    # ── Convenience properties ──────────────────────────────────────────

    @property
    def is_resolved(self) -> bool:
        """Whether inheritance has been fully resolved."""
        return self.state == self.STATE_RESOLVED

    @property
    def bed_size_x(self) -> float | None:
        """Approximate bed X size derived from printable_area."""
        return self._bed_dimension(0)

    @property
    def bed_size_y(self) -> float | None:
        """Approximate bed Y size derived from printable_area."""
        return self._bed_dimension(1)

    def _bed_dimension(self, axis: int) -> float | None:
        """Extract bed dimension from printable_area points.

        Args:
            axis: 0 for X, 1 for Y.

        Returns:
            The size along the given axis, or None if not available.
        """
        if not self.printable_area:
            return None
        try:
            coords = []
            for point in self.printable_area:
                parts = str(point).replace(",", "x").split("x")
                coords.append(float(parts[axis]))
            return max(coords) - min(coords)
        except (ValueError, IndexError):
            return None

    @property
    def first_nozzle_diameter(self) -> float | None:
        """Diameter of the first nozzle (convenience shortcut)."""
        if self.nozzle_diameter and len(self.nozzle_diameter) > 0:
            try:
                return float(self.nozzle_diameter[0])
            except (ValueError, TypeError):
                return None
        return None

    # ── Import / Resolution ─────────────────────────────────────────────

    @classmethod
    def get_pending_for_parent(cls, parent_name: str) -> models.QuerySet:
        """Return pending profiles waiting for the given parent.

        Args:
            parent_name: The OrcaSlicer profile name of the parent.

        Returns:
            QuerySet of pending OrcaMachineProfile instances.
        """
        return cls.objects.filter(
            inherits_name=parent_name,
            state=cls.STATE_PENDING,
        )


class FileVersion(models.Model):
    """Version tracking for STL and G-code files attached to parts."""

    part = models.ForeignKey(Part, on_delete=models.CASCADE, related_name="file_versions")
    version = models.PositiveIntegerField(default=1)
    file = models.FileField(upload_to="file_versions/")
    file_type = models.CharField(max_length=10, choices=[("stl", "STL"), ("gcode", "G-code")])
    file_hash = models.CharField(max_length=64, blank=True, help_text="SHA-256 hash of the file")
    file_size = models.PositiveIntegerField(default=0, help_text="File size in bytes")
    uploaded_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True)
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-version"]
        unique_together = ["part", "version", "file_type"]

    def __str__(self) -> str:
        return f"{self.part.name} v{self.version} ({self.file_type})"


class ProjectDocument(models.Model):
    """A file attachment (PDF, TXT, image, CAD, etc.) linked to a project."""

    project = models.ForeignKey(
        Project,
        on_delete=models.CASCADE,
        related_name="documents",
    )
    name = models.CharField(max_length=255, blank=True)
    file = models.FileField(upload_to="project_documents/")
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return self.name

    @property
    def file_extension(self) -> str:
        """Return the lowercase file extension without the leading dot."""
        import os

        _, ext = os.path.splitext(self.file.name)
        return ext.lstrip(".").lower()


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
        Project,
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
