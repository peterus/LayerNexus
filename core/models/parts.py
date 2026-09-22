"""Part and PrintTimeEstimate models for the LayerNexus application."""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional

from django.db import models
from django.db.models import Sum

if TYPE_CHECKING:
    from core.models.orca_profiles import OrcaPrintPreset
    from core.models.projects import Project


def resolve_part_preset(part: Part, nearest_project: Project | None) -> Optional[OrcaPrintPreset]:
    """Resolve a part's effective print preset given its nearest containing project.

    Applies the Variant-B precedence: an explicit ``part.print_preset`` override wins;
    otherwise the ``default_print_preset`` of the nearest directly-containing project on
    the current build path is used; otherwise ``None``.

    Args:
        part: The part whose print preset to resolve.
        nearest_project: The project directly containing the part on this build path,
            or ``None`` when there is no project context.

    Returns:
        The resolved :class:`~core.models.orca_profiles.OrcaPrintPreset`, or ``None``.
    """
    if part.print_preset_id is not None:
        return part.print_preset
    if nearest_project is not None and nearest_project.default_print_preset_id is not None:
        return nearest_project.default_print_preset
    return None


class Part(models.Model):
    """A standalone reusable part that can be attached to projects via ProjectPart edges."""

    name = models.CharField(
        max_length=255,
        blank=True,
        help_text="Optional — derived from the uploaded filename if left empty.",
    )
    stl_file = models.FileField(upload_to="stl_files/", blank=True, null=True)  # also stores 3MF files
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
    estimated_with_preset = models.ForeignKey(
        "OrcaPrintPreset",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="estimated_parts",
        help_text="The print preset that produced the currently stored estimate.",
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
        db_index=True,
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
        return self.name

    def containing_projects(self) -> list[Project]:
        """Return distinct projects that include this part via a composition edge.

        Traverses the ``ProjectPart`` edges pointing at this part (``project_links``)
        rather than the legacy single ``project`` FK, so a part shared by several
        modules lists all of them (its "used in").

        Returns:
            Distinct :class:`~core.models.projects.Project` instances, first-seen
            order preserved.
        """
        seen: dict[int, Project] = {}
        for link in self.project_links.select_related("project").all():
            seen.setdefault(link.project_id, link.project)
        return list(seen.values())

    @property
    def is_3mf(self) -> bool:
        """Return True if the uploaded model file is a 3MF file."""
        return bool(self.stl_file) and self.stl_file.name.lower().endswith(".3mf")

    @property
    def effective_print_preset_id(self) -> Optional[int]:
        """Return the part's own print preset ID, or ``None`` if not set.

        Returns:
            Print preset primary key, or ``None``.
        """
        return self.print_preset_id or None

    @property
    def effective_print_preset(self) -> Optional[OrcaPrintPreset]:
        """Return the part's own print preset, or ``None`` if not set.

        Returns:
            OrcaPrintPreset instance, or ``None``.
        """
        return self.print_preset if self.print_preset_id else None

    def resolve_estimation_preset(self) -> tuple[Optional[OrcaPrintPreset], bool]:
        """Resolve the preset to estimate this part with, flagging ambiguity.

        Variant B without a build context: an explicit override wins; otherwise the
        ``default_print_preset`` of the containing projects is used when they all agree
        (or there is exactly one). When two or more containing projects disagree and there
        is no override, the preset is **ambiguous** — estimation must not guess.

        Returns:
            ``(preset, ambiguous)``. When ``ambiguous`` is ``True`` the preset is ``None``
            and the caller should mark the estimate as needing an override rather than
            picking one. When ``ambiguous`` is ``False`` the preset may still be ``None``
            (no override and no containing-project default).
        """
        if self.print_preset_id is not None:
            return self.print_preset, False
        distinct: dict[int, OrcaPrintPreset] = {}
        for project in self.containing_projects():
            if project.default_print_preset_id is not None:
                distinct[project.default_print_preset_id] = project.default_print_preset
        if len(distinct) > 1:
            return None, True
        if len(distinct) == 1:
            return next(iter(distinct.values())), False
        return None, False

    @property
    def color_display(self) -> str:
        """Display-friendly color string ('—' if not set)."""
        return self.color or "—"

    @property
    def printed_quantity(self) -> int:
        """Number of this part already printed, summed over completed print jobs.

        Semantics: a print job counts as *printed* for this part as soon as it
        has **at least one** plate in the ``completed`` state. Each qualifying
        job contributes this part's job ``quantity`` exactly **once**, no matter
        how many completed plates it has.

        Two evaluation paths keep this correct *and* efficient for every caller:

        * **Prefetched** — when the whole ``job_entries__print_job__plates``
          chain is in the instance cache (see
          :meth:`Project.aggregate_prefetch_lookups`), the count is summed in
          Python with **no** extra query, which keeps project-list/detail/
          dashboard rendering off the N+1 path. The branch is only taken when the
          nested ``print_job`` and ``plates`` caches are present too, so a
          partial ``job_entries``-only prefetch can never silently fan out into a
          per-entry query.
        * **Not prefetched** — fall back to a single DB aggregate: the distinct
          completed-``job_entries`` PKs become an ``IN`` subquery inside the
          ``Sum`` query, so a lone caller such as ``PartDetailView`` pays one
          query no matter how many job entries exist (no per-entry N+1).

        Both paths count each job's ``quantity`` once — there is no plate join
        that fans the rows out.
        """
        completed = "completed"
        prefetched = getattr(self, "_prefetched_objects_cache", None)
        if prefetched is not None and "job_entries" in prefetched:
            entries = prefetched["job_entries"]
            if all(
                "print_job" in entry._state.fields_cache
                and "plates" in getattr(entry.print_job, "_prefetched_objects_cache", {})
                for entry in entries
            ):
                return sum(
                    entry.quantity
                    for entry in entries
                    if any(plate.status == completed for plate in entry.print_job.plates.all())
                )

        completed_entry_pks = (
            self.job_entries.filter(print_job__plates__status=completed).values_list("pk", flat=True).distinct()
        )
        return self.job_entries.filter(pk__in=completed_entry_pks).aggregate(total=Sum("quantity"))["total"] or 0

    def printed_quantity_for(self, assembly: Project) -> int:
        """Completed-plate print quantity of this part attributed to ``assembly``.

        Same completion rule as :attr:`printed_quantity` (a job counts once it has at
        least one completed plate), but restricted to job entries whose
        ``target_assembly`` is ``assembly``. Unattributed entries (``target_assembly``
        is ``NULL``) are never counted here; they remain in the global
        :attr:`printed_quantity` only.

        Two evaluation paths mirror :attr:`printed_quantity` so this stays off the N+1
        path for prefetched aggregate rendering: when the whole
        ``job_entries__print_job__plates`` chain is cached (see
        :meth:`Project.aggregate_prefetch_lookups`) the attributed sum is computed in Python
        with **no** extra query, filtering on the already-loaded ``target_assembly_id`` FK
        column; otherwise a single DB aggregate is issued.

        Args:
            assembly: The build context (top-level assembly project) to filter by.

        Returns:
            Sum of attributed job-entry quantities that have at least one completed plate.
        """
        completed = "completed"
        prefetched = getattr(self, "_prefetched_objects_cache", None)
        if prefetched is not None and "job_entries" in prefetched:
            entries = prefetched["job_entries"]
            if all(
                "print_job" in entry._state.fields_cache
                and "plates" in getattr(entry.print_job, "_prefetched_objects_cache", {})
                for entry in entries
            ):
                return sum(
                    entry.quantity
                    for entry in entries
                    if entry.target_assembly_id == assembly.pk
                    and any(plate.status == completed for plate in entry.print_job.plates.all())
                )

        completed_pks = (
            self.job_entries.filter(target_assembly=assembly, print_job__plates__status=completed)
            .values_list("pk", flat=True)
            .distinct()
        )
        return self.job_entries.filter(pk__in=completed_pks).aggregate(total=Sum("quantity"))["total"] or 0


class PrintTimeEstimate(models.Model):
    """Historical print time data for calibrating future estimates."""

    part = models.ForeignKey(Part, on_delete=models.CASCADE, related_name="time_estimates")
    printer = models.ForeignKey("core.PrinterProfile", on_delete=models.CASCADE, related_name="time_estimates")
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
