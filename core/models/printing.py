"""PrintJob, PrintJobPart, and PrintJobPlate models for the LayerNexus application."""

from __future__ import annotations

from django.conf import settings
from django.core.validators import MinValueValidator
from django.db import models
from django.db.models import CheckConstraint, Q, Sum
from django.utils import timezone


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
    #: Statuses of a finished job; every other status counts as unfinished.
    TERMINAL_STATUSES: tuple[str, ...] = (STATUS_COMPLETED, STATUS_FAILED, STATUS_CANCELLED)

    name = models.CharField(
        max_length=255,
        blank=True,
        help_text="Optional descriptive name for this job",
    )
    parts = models.ManyToManyField(
        "core.Part",
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
    print_preset = models.ForeignKey(
        "OrcaPrintPreset",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="print_jobs",
        help_text="Print preset resolved for this job at creation (one preset per job).",
    )
    printer = models.ForeignKey(
        "core.PrinterProfile",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="print_jobs",
        help_text="Assigned when the job is added to a print queue",
    )
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_DRAFT, db_index=True)

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
    def bed_dimensions(self) -> tuple[float, float, float] | None:
        """Build volume (x, y, z in mm) of the machine this job is sliced for.

        Uses the job's machine profile and falls back to the assigned printer.
        A source only counts if it defines both bed X and Y; a missing
        printable height defaults to 256 mm. The size is the extent of
        ``printable_area`` (max - min); an origin offset (e.g. a bed centred on 0)
        is not modelled.

        Returns:
            ``(x, y, z)`` or ``None`` if no source provides a bed size.
        """
        sources = (self.machine_profile, self.printer)
        for source in sources:
            if source is None:
                continue
            x, y, z = source.bed_size_x, source.bed_size_y, getattr(source, "bed_size_z", None)
            if z is None:
                z = getattr(source, "printable_height", None)
            if x and y and x > 0 and y > 0:
                return (float(x), float(y), float(z) if z and z > 0 else 256.0)
        return None

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

    def refresh_stored_print_status(self) -> None:
        """Re-derive the stored print status (``printing`` / ``completed``) from the plates.

        Only touches jobs in the print phase (``sliced`` / ``printing`` /
        ``completed``): a job whose plates are all completed is ``completed``,
        a job with a started print attempt is ``printing``.  Called by the
        plate after each attempt transition — transitional until the print
        status is derived on the fly.
        """
        if self.status not in (self.STATUS_SLICED, self.STATUS_PRINTING, self.STATUS_COMPLETED):
            return
        from core.models.queue import PrintQueue

        now = timezone.now()
        update_fields: list[str] = []
        attempts = PrintQueue.objects.filter(plate__print_job=self)
        if self.all_plates_completed:
            new_status = self.STATUS_COMPLETED
            if self.completed_at is None:
                self.completed_at = now
                update_fields.append("completed_at")
        elif attempts.filter(Q(started_at__isnull=False) | Q(completed_at__isnull=False)).exists():
            new_status = self.STATUS_PRINTING
        else:
            new_status = self.status if self.status != self.STATUS_COMPLETED else self.STATUS_PRINTING
        if new_status == self.STATUS_PRINTING and self.started_at is None:
            self.started_at = now
            update_fields.append("started_at")
        if new_status != self.status:
            self.status = new_status
            update_fields.append("status")
        if update_fields:
            self.save(update_fields=update_fields)


class PrintJobPart(models.Model):
    """Through model linking a part to a print job with quantity."""

    print_job = models.ForeignKey(PrintJob, on_delete=models.CASCADE, related_name="job_parts")
    part = models.ForeignKey("core.Part", on_delete=models.CASCADE, related_name="job_entries")
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
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_WAITING, db_index=True)

    # Per-plate stats
    filament_used_grams = models.FloatField(null=True, blank=True)
    print_time_estimate = models.DurationField(null=True, blank=True)

    # Remote printer job tracking (per plate)
    remote_job_id = models.CharField(
        max_length=255,
        blank=True,
        help_text="Filename or job ID on the remote printer after upload",
    )

    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["plate_number"]
        unique_together = ["print_job", "plate_number"]
        constraints = [
            CheckConstraint(
                condition=Q(plate_number__gte=1),
                name="printjobplate_plate_number_gte_1",
            ),
        ]

    def __str__(self) -> str:
        return f"Plate {self.plate_number} of {self.print_job}"

    def failed_attempts_since_manual_enqueue(self) -> int:
        """Count failed attempts since (and including) the last user-enqueued attempt.

        Retries created by the system (``is_retry=True``) do not reset the
        count; a manual enqueue does.  ``cancelled`` attempts never count.

        Returns:
            The number of ``failed`` attempts in the current retry cycle.
        """
        from core.models.queue import PrintQueue

        attempts = self.queue_entries.all()
        last_manual = attempts.filter(is_retry=False).order_by("-pk").values_list("pk", flat=True).first()
        if last_manual is not None:
            attempts = attempts.filter(pk__gte=last_manual)
        return attempts.filter(result=PrintQueue.RESULT_FAILED).count()

    def refresh_stored_status(self) -> None:
        """Re-derive and store ``status`` / ``started_at`` / ``completed_at`` from the print attempts.

        The **only** place that writes ``PrintJobPlate.status`` (transitional
        until the plate status is derived on the fly).  Priority:

        1. a passed attempt -> ``completed`` (at its ``resolved_at``);
        2. an open attempt -> ``printing`` once started, else ``waiting``;
        3. ``max_retries`` failed attempts since the last manual enqueue -> ``failed``;
        4. otherwise ``waiting``.

        Also refreshes the job's stored print status.
        """
        from core.models.queue import PrintQueue

        attempts = list(self.queue_entries.order_by("pk"))
        passed = [a for a in attempts if a.result == PrintQueue.RESULT_PASSED]
        open_attempt = next((a for a in attempts if not a.result), None)
        started = [a.started_at for a in attempts if a.started_at is not None]

        completed_at = self.completed_at
        if passed:
            status = self.STATUS_COMPLETED
            completed_at = passed[0].resolved_at or completed_at
        elif open_attempt is not None:
            started_open = open_attempt.started_at is not None or open_attempt.completed_at is not None
            status = self.STATUS_PRINTING if started_open else self.STATUS_WAITING
        else:
            manual = [a for a in attempts if not a.is_retry]
            max_retries = manual[-1].max_retries if manual else (attempts[-1].max_retries if attempts else 0)
            failures = self.failed_attempts_since_manual_enqueue()
            status = self.STATUS_FAILED if failures and failures >= max_retries else self.STATUS_WAITING

        self.status = status
        self.started_at = max(started) if started else self.started_at
        self.completed_at = completed_at
        self.save(update_fields=["status", "started_at", "completed_at"])
        self.print_job.refresh_stored_print_status()
