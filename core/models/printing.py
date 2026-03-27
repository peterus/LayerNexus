"""PrintJob, PrintJobPart, and PrintJobPlate models for the LayerNexus application."""

from __future__ import annotations

from django.conf import settings
from django.core.validators import MinValueValidator
from django.db import models
from django.db.models import Sum


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
    printer = models.ForeignKey(
        "core.PrinterProfile",
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
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_WAITING)

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

    def __str__(self) -> str:
        return f"Plate {self.plate_number} of {self.print_job}"
