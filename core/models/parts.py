"""Part and PrintTimeEstimate models for the LayerNexus application."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Optional

from django.core.validators import MinValueValidator
from django.db import models, transaction
from django.db.models import CheckConstraint, Exists, OuterRef, Q, QuerySet, Sum, prefetch_related_objects

from core.models.file_copy import CopiedFiles, duplicate_field_file

if TYPE_CHECKING:
    from core.models.orca_profiles import OrcaPrintPreset
    from core.models.printing import PrintJob, PrintJobPart
    from core.models.projects import Project


def _job_fully_printed(job: PrintJob) -> bool:
    """Return whether every plate of a job (from the prefetch cache) is completed.

    Args:
        job: Print job whose ``plates`` are already prefetched (with their
            derived state, see :meth:`Project.aggregate_prefetch_lookups`).

    Returns:
        ``True`` if the job has at least one plate and all of them are completed.
    """
    from core.models.printing import PrintJobPlate

    plates = job.plates.all()
    return bool(plates) and all(plate.state == PrintJobPlate.STATE_COMPLETED for plate in plates)


class Part(models.Model):
    """A printable part that belongs to exactly one project (or sub-project)."""

    name = models.CharField(
        max_length=255,
        blank=True,
        help_text="Optional — derived from the uploaded filename if left empty.",
    )
    project = models.ForeignKey(
        "core.Project",
        on_delete=models.CASCADE,
        related_name="parts",
        help_text="The project or sub-project this part belongs to.",
    )
    quantity = models.PositiveIntegerField(
        default=1,
        validators=[MinValueValidator(1)],
        help_text="How many of this part the project needs.",
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
        constraints = [
            CheckConstraint(condition=Q(quantity__gte=1), name="part_quantity_gte_1"),
        ]

    def __str__(self) -> str:
        return self.name

    @property
    def is_3mf(self) -> bool:
        """Return True if the uploaded model file is a 3MF file."""
        return bool(self.stl_file) and self.stl_file.name.lower().endswith(".3mf")

    @property
    def effective_print_preset(self) -> Optional[OrcaPrintPreset]:
        """Return the preset this part is sliced with, resolved bottom-up.

        The part's own ``print_preset`` wins; otherwise the nearest
        ``default_print_preset`` up the project chain is used.

        Returns:
            The resolved :class:`~core.models.orca_profiles.OrcaPrintPreset`, or ``None``.
        """
        if self.print_preset_id is not None:
            return self.print_preset
        if self.project_id is None:
            return None
        return self.project.effective_default_print_preset

    @property
    def effective_print_preset_id(self) -> Optional[int]:
        """Return the primary key of :attr:`effective_print_preset`, or ``None``."""
        if self.print_preset_id is not None:
            return self.print_preset_id
        if self.project_id is None:
            return None
        return self.project.effective_default_print_preset_id

    def blocking_jobs(self) -> QuerySet[PrintJob]:
        """Return unfinished print jobs that contain this part.

        The part cannot be deleted while any of these exist (same rule as
        :meth:`Project.blocking_jobs`).

        Returns:
            Distinct queryset of :class:`~core.models.printing.PrintJob` rows that
            are not finished (display status not in
            :attr:`PrintJob.FINISHED_DISPLAY_STATUSES`).
        """
        from core.models.printing import PrintJob

        # ``pk__in`` instead of a join: no row fan-out, so the display-status subqueries run once per job.
        return PrintJob.objects.filter(pk__in=self.job_entries.values("print_job_id")).unfinished()

    def history_jobs(self) -> QuerySet[PrintJob]:
        """Return the print jobs with print attempts that contain this part (its print history).

        A job counts if the part is one of its parts while any plate has attempts,
        or if the part lies on a plate (plate parts) that has attempts.
        """
        from core.models.printing import PrintJob

        return PrintJob.objects.filter(
            Q(job_parts__part=self) | Q(plates__plate_parts__part=self),
            plates__queue_entries__isnull=False,
        ).distinct()

    def has_print_history(self) -> bool:
        """Return True if the part is in a print job (or on a plate) that has print attempts.

        Such a part is not deletable — its print history must be kept.
        """
        return self.history_jobs().exists()

    def is_estimable(self) -> bool:
        """Return whether this part has an STL file and a resolvable print preset."""
        return bool(self.stl_file) and self.effective_print_preset_id is not None

    def clear_estimate(self, *, queued: bool = False) -> None:
        """Discard the stored estimate and its provenance.

        Writes straight to the database (not through ``save()``), so the in-memory
        instance is not updated.

        Args:
            queued: Mark the part ``pending`` for the background worker instead of ``none``.
        """
        Part.objects.filter(pk=self.pk).update(
            filament_used_grams=None,
            filament_used_meters=None,
            estimated_print_time=None,
            estimation_status=self.ESTIMATION_PENDING if queued else self.ESTIMATION_NONE,
            estimation_error="",
            estimated_with_preset=None,
        )

    def request_estimate(self) -> bool:
        """Clear the stored estimate and queue the part for background estimation.

        The caller starts the OrcaSlicer worker afterwards (a model must not spawn threads).

        Returns:
            ``True`` if the part was queued, ``False`` if it is not estimable
            (see :meth:`is_estimable`) and was left untouched.
        """
        if not self.is_estimable():
            return False
        self.clear_estimate(queued=True)
        return True

    @classmethod
    def claim_estimate(cls, pk: int) -> bool:
        """Atomically move a part from ``pending`` to ``estimating``.

        Returns:
            ``True`` if this caller claimed the part, ``False`` if it was no longer pending.
        """
        return bool(
            cls.objects.filter(pk=pk, estimation_status=cls.ESTIMATION_PENDING).update(
                estimation_status=cls.ESTIMATION_ESTIMATING
            )
        )

    @classmethod
    def requeue_stuck_estimates(cls) -> int:
        """Reset every part stuck at ``estimating`` (e.g. after a crash) back to ``pending``.

        Returns:
            Number of parts reset.
        """
        return cls.objects.filter(estimation_status=cls.ESTIMATION_ESTIMATING).update(
            estimation_status=cls.ESTIMATION_PENDING
        )

    def finish_estimate(self, **values: Any) -> bool:
        """Store a successful estimate, but only if the part is still ``estimating``.

        If the part was edited or re-queued while the slice was in flight, the stale
        result is discarded so the newer request wins.

        Args:
            **values: Estimate fields to write (``filament_used_grams``, ``estimated_with_preset``, ...).

        Returns:
            ``True`` if the result was stored, ``False`` if it was superseded.
        """
        return bool(
            Part.objects.filter(pk=self.pk, estimation_status=self.ESTIMATION_ESTIMATING).update(
                estimation_status=self.ESTIMATION_SUCCESS,
                estimation_error="",
                **values,
            )
        )

    def fail_estimate(self, error: str) -> None:
        """Record a failed estimation, but only if the part is still ``estimating``."""
        Part.objects.filter(pk=self.pk, estimation_status=self.ESTIMATION_ESTIMATING).update(
            estimation_status=self.ESTIMATION_ERROR,
            estimation_error=error,
        )

    def abort_estimate(self) -> None:
        """Give up on estimating (no model file or no preset) and reset the part to ``none``."""
        Part.objects.filter(pk=self.pk).update(
            estimation_status=self.ESTIMATION_NONE,
            estimated_with_preset=None,
        )

    def deep_copy(self, *, project: Project) -> Part:
        """Copy this part (content, never history) into ``project``.

        Copies the scalar fields, the estimate fields and the ``PrintTimeEstimate``
        rows (without the measured ``actual_time``, which is history), and writes
        the STL/3MF as a new file. Print history (``PrintJobPart``)
        and ``FileVersion`` rows are not copied. An in-flight estimation
        (``pending``/``estimating``) resets the copy's estimation status to ``none``
        so it is never stuck; a model file missing on disk also drops the copied
        estimate.

        The file is copied *before* the (short) row transaction, so the SQLite write
        lock is not held during file I/O; if anything fails, the new file is removed.

        Args:
            project: Target project of the copy.

        Returns:
            The new :class:`Part`.
        """
        tracker = CopiedFiles()
        try:
            stl_name = duplicate_field_file(self.stl_file, tracker)
            # Read the time estimates before taking the write lock.
            prefetch_related_objects([self], "time_estimates")
            with transaction.atomic():
                return self._create_copy(project, stl_name)
        except Exception:
            tracker.delete_all()
            raise

    def _create_copy(self, project: Project, stl_name: str | None) -> Part:
        """Insert the copy's rows; the caller copied the model file beforehand.

        Args:
            project: Target project of the copy.
            stl_name: Storage name of the copied model file, or ``None`` if the part
                has none or it was missing on disk.

        Returns:
            The new :class:`Part`.
        """
        missing_file = bool(self.stl_file) and stl_name is None
        reset = missing_file or self.estimation_status in (self.ESTIMATION_PENDING, self.ESTIMATION_ESTIMATING)
        copy = Part.objects.create(
            project=project,
            name=self.name,
            quantity=self.quantity,
            stl_file=stl_name,
            color=self.color,
            material=self.material,
            spoolman_filament_id=self.spoolman_filament_id,
            print_preset_id=self.print_preset_id,
            notes=self.notes,
            filament_used_grams=None if missing_file else self.filament_used_grams,
            filament_used_meters=None if missing_file else self.filament_used_meters,
            estimated_print_time=None if missing_file else self.estimated_print_time,
            estimation_status=self.ESTIMATION_NONE if reset else self.estimation_status,
            estimation_error="" if reset else self.estimation_error,
            estimated_with_preset_id=None if reset else self.estimated_with_preset_id,
        )
        if missing_file:
            return copy
        estimates = [
            PrintTimeEstimate(
                part=copy,
                printer_id=estimate.printer_id,
                estimated_time=estimate.estimated_time,
                # actual_time is measured print history and is never copied.
            )
            # Oldest first, so the copies keep the newest-first default order.
            for estimate in reversed(self.time_estimates.all())
        ]
        if estimates:
            PrintTimeEstimate.objects.bulk_create(estimates)
        return copy

    @property
    def color_display(self) -> str:
        """Display-friendly color string ('—' if not set)."""
        return self.color or "—"

    @property
    def printed_quantity(self) -> int:
        """Number of this part already printed, summed over completed print jobs.

        Semantics: a print job counts as *printed* for this part only once
        **all** of its plates are completed — have a passed print attempt (which part sits on which plate
        is unknown, so a partially printed multi-plate job counts as nothing).
        Each qualifying job contributes this part's job ``quantity`` exactly
        **once**, no matter how many plates it has.

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
        entries = self._prefetched_job_entries()
        if entries is not None:
            return sum(entry.quantity for entry in entries if _job_fully_printed(entry.print_job))

        completed_entry_pks = self._fully_printed_entry_pks()
        return self.job_entries.filter(pk__in=completed_entry_pks).aggregate(total=Sum("quantity"))["total"] or 0

    def _prefetched_job_entries(self) -> list[PrintJobPart] | None:
        """Return the prefetched job entries if the whole plate chain is cached.

        Only returns entries when ``job_entries__print_job__plates`` is fully in the
        instance cache, so callers can evaluate completion without any query. A
        partial ``job_entries``-only prefetch returns ``None`` so it can never
        silently fan out into a per-entry query.

        Returns:
            The cached ``PrintJobPart`` list, or ``None`` if not (fully) prefetched.
        """
        prefetched = getattr(self, "_prefetched_objects_cache", None)
        if prefetched is None or "job_entries" not in prefetched:
            return None
        entries = prefetched["job_entries"]
        if all(
            "print_job" in entry._state.fields_cache
            and "plates" in getattr(entry.print_job, "_prefetched_objects_cache", {})
            for entry in entries
        ):
            return entries
        return None

    def _fully_printed_entry_pks(self) -> QuerySet:
        """Return PKs of this part's job entries whose job has all plates completed.

        A plate is completed once it has a passed print attempt.  Evaluated lazily
        as an ``IN`` subquery by the callers, so the full aggregate still costs a
        single query.

        Returns:
            Distinct ``values_list`` queryset of qualifying ``PrintJobPart`` PKs.
        """
        from core.models.printing import PrintJobPlate
        from core.models.queue import PrintQueue

        passed = PrintQueue.objects.filter(plate=OuterRef("pk"), result=PrintQueue.RESULT_PASSED)
        unfinished_job_ids = PrintJobPlate.objects.filter(~Exists(passed)).values("print_job_id")
        return (
            self.job_entries.filter(print_job__plates__isnull=False)
            .exclude(print_job_id__in=unfinished_job_ids)
            .values_list("pk", flat=True)
            .distinct()
        )


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
