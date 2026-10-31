"""Part and PrintTimeEstimate models for the LayerNexus application."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Optional

from django.conf import settings
from django.core.validators import MinValueValidator
from django.db import models, transaction
from django.db.models import CheckConstraint, Exists, OuterRef, Prefetch, Q, QuerySet, prefetch_related_objects
from django.utils import timezone

from core.models.file_copy import CopiedFiles, duplicate_field_file

if TYPE_CHECKING:
    from core.models.orca_profiles import OrcaPrintPreset
    from core.models.printing import PrintJob, PrintJobPart, PrintJobPlate
    from core.models.projects import Project


def _printed_in_job(part_id: int, entry: PrintJobPart, plates: list[PrintJobPlate]) -> int:
    """Return how many copies of a part one print job has printed.

    * The job's plate parts are reliable (``plate_parts_known``) and recorded:
      a copy counts once the ``meshes_on_plate`` over all plates with a passed
      attempt add up to its ``mesh_count`` (a partly printed job counts its passed
      plates; a multi-mesh copy counts only once all its meshes are printed).
    * Otherwise (plate assignment unknown, or no plate parts recorded) the legacy
      rule: all plates completed → the part's job ``quantity``, else nothing.

    Args:
        part_id: Primary key of the part.
        entry: The part's ``PrintJobPart`` of the job (with ``print_job`` loaded).
        plates: The job's plates with their derived state and their ``plate_parts``
            (cached; see :meth:`Part.printed_quantity_prefetch_lookups`).

    Returns:
        Number of printed copies, never more than the job ``quantity``.
    """
    from core.models.printing import PrintJobPlate

    if not plates:
        return 0
    plate_rows = [(plate.state == PrintJobPlate.STATE_COMPLETED, list(plate.plate_parts.all())) for plate in plates]
    if not entry.print_job.plate_parts_known or not any(rows for _passed, rows in plate_rows):
        return entry.quantity if all(passed for passed, _rows in plate_rows) else 0
    printed_meshes: dict[int, int] = {}
    mesh_counts: dict[int, int] = {}
    for passed, rows in plate_rows:
        if not passed:
            continue
        for row in rows:
            if row.part_id == part_id:
                printed_meshes[row.copy_index] = printed_meshes.get(row.copy_index, 0) + row.meshes_on_plate
                mesh_counts[row.copy_index] = row.mesh_count
    complete = sum(1 for copy, meshes in printed_meshes.items() if meshes >= mesh_counts[copy])
    return min(complete, entry.quantity)


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
        or if the part lies on a plate (plate parts) that has attempts.  Built from
        ``EXISTS`` subqueries, so no row fan-out over plates and attempts.
        """
        from core.models.printing import PrintJob, PrintJobPart, PrintJobPlatePart
        from core.models.queue import PrintQueue

        in_job = Exists(PrintJobPart.objects.filter(print_job=OuterRef("pk"), part=self))
        on_attempted_plate = Exists(
            PrintJobPlatePart.objects.filter(
                plate__print_job=OuterRef("pk"),
                part=self,
                plate__queue_entries__isnull=False,
            )
        )
        has_attempts = Exists(PrintQueue.objects.filter(plate__print_job=OuterRef("pk")))
        # Restrict the outer scan to this part's jobs (indexed) before the correlated checks.
        candidates = Q(pk__in=self.job_entries.values("print_job_id")) | Q(
            pk__in=PrintJobPlatePart.objects.filter(part=self).values("plate__print_job_id")
        )
        return PrintJob.objects.filter(candidates).filter((in_job & has_attempts) | on_attempted_plate)

    def has_print_history(self) -> bool:
        """Return True if the part is in a print job (or on a plate) that has print attempts, or has manual
        printed-part records.

        Such a part is not deletable — its print history must be kept.
        """
        return self.printed_records.exists() or self.history_jobs().exists()

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

    @staticmethod
    def printed_quantity_prefetch_lookups(prefix: str = "") -> list[str | Prefetch]:
        """Prefetch lookups that let :attr:`printed_quantity` run without any query.

        Args:
            prefix: Lookup path to the parts (e.g. ``"parts__"`` from a project).

        Returns:
            ``prefetch_related`` arguments: the job entries with their jobs, the
            plates (annotated with their derived state) and plate parts, and the
            manual printed-part records.
        """
        from core.models.printing import PrintJobPlate

        return [
            Prefetch(f"{prefix}job_entries__print_job__plates", queryset=PrintJobPlate.objects.with_state()),
            f"{prefix}job_entries__print_job__plates__plate_parts",
            Prefetch(f"{prefix}printed_records", queryset=PrintedPartRecord.objects.select_related("created_by")),
        ]

    @property
    def printed_quantity(self) -> int:
        """Number of copies of this part printed so far (derived, never stored).

        The sum of

        * the copies printed by print jobs (see :func:`_printed_in_job`): copies
          whose meshes all lie on plates with a passed attempt, or — for jobs whose
          plate assignment is unknown — the job ``quantity`` once every plate is
          completed, and
        * the manual :class:`PrintedPartRecord` quantities.

        Evaluated from the prefetch cache when
        :meth:`printed_quantity_prefetch_lookups` (part of
        :meth:`Project.aggregate_prefetch_lookups`) was applied, so lists and the
        project detail stay off the N+1 path; otherwise from a constant number of
        queries.  Both paths run the same counting code.
        """
        sources = self._prefetched_print_sources()
        if sources is None:
            sources = self._load_print_sources()
        entries, plates_by_job, records = sources
        printed = sum(_printed_in_job(self.pk, entry, plates_by_job.get(entry.print_job_id, [])) for entry in entries)
        return printed + sum(record.quantity for record in records)

    def printed_plates(self) -> list[dict[str, Any]]:
        """Return where the automatically counted copies were printed (read-only overview).

        One row per passed plate that carries copies of this part (jobs with a known
        plate assignment), or one row per completed job counted by the legacy
        all-plates rule (``plate`` is ``None`` then).  Uses the same inputs as
        :attr:`printed_quantity`; prefetch ``job_entries__print_job__plates__queue_entries``
        as well to read the dates without a query per plate.

        Returns:
            Dicts with ``job``, ``plate`` (or ``None``), ``copies`` (copies completed
            by this plate — a multi-mesh copy split over plates is listed on the last
            one — or counted for the whole job), ``partial`` (meshes of copies that
            are not complete yet) and ``date`` (when the plate passed), newest first.
        """
        from core.models.printing import PrintJobPlate

        sources = self._prefetched_print_sources()
        if sources is None:
            sources = self._load_print_sources()
        entries, plates_by_job, _records = sources
        rows: list[dict[str, Any]] = []
        for entry in entries:
            plates = plates_by_job.get(entry.print_job_id, [])
            if not plates:
                continue
            job = entry.print_job
            has_plate_parts = any(plate.plate_parts.all() for plate in plates)
            if not job.plate_parts_known or not has_plate_parts:
                if _printed_in_job(self.pk, entry, plates):
                    dates = [plate.completed_at for plate in plates if plate.completed_at is not None]
                    date = max(dates, default=None)
                    rows.append({"job": job, "plate": None, "copies": entry.quantity, "partial": 0, "date": date})
                continue
            passed_plates = [plate for plate in plates if plate.state == PrintJobPlate.STATE_COMPLETED]
            # Same completion rule as ``_printed_in_job``; a complete copy is listed on the
            # (last) passed plate that finished it, so the listed copies add up to the count.
            meshes: dict[int, int] = {}
            mesh_counts: dict[int, int] = {}
            finished_on: dict[int, PrintJobPlate] = {}
            for plate in passed_plates:
                for row in plate.plate_parts.all():
                    if row.part_id == self.pk:
                        meshes[row.copy_index] = meshes.get(row.copy_index, 0) + row.meshes_on_plate
                        mesh_counts[row.copy_index] = row.mesh_count
                        finished_on[row.copy_index] = plate
            complete = {copy for copy, count in meshes.items() if count >= mesh_counts[copy]}
            for plate in passed_plates:
                mine = [row for row in plate.plate_parts.all() if row.part_id == self.pk]
                copies = sum(1 for copy in complete if finished_on[copy] is plate)
                partial = sum(1 for row in mine if row.copy_index not in complete)
                if copies or partial:
                    rows.append(
                        {"job": job, "plate": plate, "copies": copies, "partial": partial, "date": plate.completed_at}
                    )
        rows.sort(key=lambda row: row["date"].timestamp() if row["date"] is not None else 0.0, reverse=True)
        return rows

    def _prefetched_print_sources(
        self,
    ) -> tuple[list[PrintJobPart], dict[int, list[PrintJobPlate]], list[PrintedPartRecord]] | None:
        """Return the counting inputs from the prefetch cache, or ``None`` if not fully cached.

        Only returns them when the whole chain of
        :meth:`printed_quantity_prefetch_lookups` is cached, so a partial prefetch
        can never silently fan out into per-entry queries.

        Returns:
            ``(job entries, plates per job pk, manual records)`` or ``None``.
        """
        prefetched = getattr(self, "_prefetched_objects_cache", {})
        if "job_entries" not in prefetched or "printed_records" not in prefetched:
            return None
        entries = list(prefetched["job_entries"])
        plates_by_job: dict[int, list[PrintJobPlate]] = {}
        for entry in entries:
            if "print_job" not in entry._state.fields_cache:
                return None
            job_cache = getattr(entry.print_job, "_prefetched_objects_cache", {})
            if "plates" not in job_cache:
                return None
            plates = list(job_cache["plates"])
            if any("plate_parts" not in getattr(plate, "_prefetched_objects_cache", {}) for plate in plates):
                return None
            plates_by_job[entry.print_job_id] = plates
        return entries, plates_by_job, list(prefetched["printed_records"])

    def _load_print_sources(
        self,
    ) -> tuple[list[PrintJobPart], dict[int, list[PrintJobPlate]], list[PrintedPartRecord]]:
        """Load the counting inputs from the database (four queries, independent of the job count).

        Nothing is cached on the instance, so a later call sees new attempts and records.

        Returns:
            ``(job entries, plates per job pk, manual records)``.
        """
        from core.models.printing import PrintJobPlate

        entries = list(self.job_entries.select_related("print_job"))
        plates_by_job: dict[int, list[PrintJobPlate]] = {}
        if entries:
            job_ids = {entry.print_job_id for entry in entries}
            plates = PrintJobPlate.objects.with_state().filter(print_job_id__in=job_ids).prefetch_related("plate_parts")
            for plate in plates:
                plates_by_job.setdefault(plate.print_job_id, []).append(plate)
        return entries, plates_by_job, list(self.printed_records.all())


class PrintedPartRecord(models.Model):
    """A manual booking of printed copies of a part (printed outside LayerNexus or not counted).

    Adds to :attr:`Part.printed_quantity` next to the copies counted from passed
    plates.  A part with records is print history and cannot be deleted
    (``PROTECT``); a wrong record is corrected by deleting it and booking anew.
    """

    part = models.ForeignKey(Part, on_delete=models.PROTECT, related_name="printed_records")
    quantity = models.PositiveIntegerField(
        validators=[MinValueValidator(1)],
        help_text="Number of printed copies",
    )
    printed_on = models.DateField(default=timezone.localdate, help_text="When the copies were printed")
    note = models.CharField(max_length=255, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="printed_part_records",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-printed_on", "-pk"]
        constraints = [
            CheckConstraint(condition=Q(quantity__gte=1), name="printedpartrecord_quantity_gte_1"),
        ]

    def __str__(self) -> str:
        return f"{self.quantity}× {self.part.name} on {self.printed_on}"


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
