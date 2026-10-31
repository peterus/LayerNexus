"""PrintJob, PrintJobPart, and PrintJobPlate models for the LayerNexus application.

Only facts are stored on these models: the slicing pipeline of a job
(``PrintJob.status``) and the plates with their G-code.  The print state of a
plate and the display status of a job are **derived** from the print attempts
(``PrintQueue`` rows) — see :attr:`PrintJobPlate.state` and
:attr:`PrintJob.display_status` (plus the queryset annotations
:meth:`PrintJobPlateQuerySet.with_state` and
:meth:`PrintJobQuerySet.with_display_status` for lists).
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from django.conf import settings
from django.core.validators import MinValueValidator
from django.db import models, transaction
from django.db.models import (
    Case,
    CharField,
    CheckConstraint,
    Count,
    Exists,
    F,
    IntegerField,
    Max,
    Min,
    OuterRef,
    Q,
    Subquery,
    Sum,
    Value,
    When,
)
from django.db.models.functions import Coalesce
from django.utils import timezone

if TYPE_CHECKING:
    from collections.abc import Iterable

    from core.models.parts import Part
    from core.models.printers import PrinterProfile
    from core.models.queue import PrintQueue


def _count_subquery(queryset: models.QuerySet, group_field: str) -> Coalesce:
    """Return ``COUNT(*)`` of ``queryset`` (already filtered on an ``OuterRef``) as a subquery expression."""
    counted = queryset.order_by().values(group_field).annotate(n=Count("pk")).values("n")
    return Coalesce(Subquery(counted, output_field=IntegerField()), 0)


def _part_labels(parts: Iterable[Part]) -> dict[int, str]:
    """Return part pk → display name; parts sharing a name get their project's name appended."""
    parts = {part.pk: part for part in parts}
    name_counts: dict[str, int] = {}
    for part in parts.values():
        name_counts[part.name] = name_counts.get(part.name, 0) + 1
    return {
        pk: part.name if name_counts[part.name] == 1 else f"{part.name} ({part.project.name})"
        for pk, part in parts.items()
    }


class PrintJobQuerySet(models.QuerySet):
    """Annotations for the derived display status of print jobs."""

    def with_display_status(self) -> PrintJobQuerySet:
        """Annotate ``annotated_display_status`` (same value as :attr:`PrintJob.display_status`).

        Computed in SQL from the slicing status and the plates' derived states,
        so job lists can filter and count by it without N+1 queries.
        """
        from core.models.queue import PrintQueue

        attempts = PrintQueue.objects.filter(plate__print_job=OuterRef("pk"))
        plates = PrintJobPlate.objects.filter(print_job=OuterRef("pk"))
        plate_states = PrintJobPlate.objects.with_state().filter(print_job=OuterRef("pk"))
        queryset = self.annotate(
            job_has_attempts=Exists(attempts),
            job_has_active_attempt=Exists(attempts.filter(PrintQueue.active_q())),
            job_plate_count=_count_subquery(plates, "print_job"),
            # Completed = has a passed attempt: a cheap EXISTS instead of the full plate-state CASE.
            job_completed_plates=_count_subquery(
                plates.filter(Exists(PrintQueue.objects.filter(plate=OuterRef("pk"), result=PrintQueue.RESULT_PASSED))),
                "print_job",
            ),
            job_failed_plates=_count_subquery(
                plate_states.filter(annotated_state=PrintJobPlate.STATE_FAILED), "print_job"
            ),
        )
        # Nested: the slicing branch is decided once instead of repeating EXISTS in every When.
        slicing_display = Case(
            When(status=PrintJob.STATUS_FAILED, then=Value(PrintJob.DISPLAY_SLICING_FAILED)),
            When(status=PrintJob.STATUS_SLICED, then=Value(PrintJob.DISPLAY_READY)),
            When(status=PrintJob.STATUS_PENDING, then=Value(PrintJob.DISPLAY_PENDING)),
            When(status=PrintJob.STATUS_SLICING, then=Value(PrintJob.DISPLAY_SLICING)),
            default=Value(PrintJob.DISPLAY_DRAFT),
            output_field=CharField(),
        )
        return queryset.annotate(
            annotated_display_status=Case(
                When(job_has_attempts=False, then=slicing_display),
                When(job_has_active_attempt=True, then=Value(PrintJob.DISPLAY_PRINTING)),
                When(
                    Q(job_plate_count__gt=0, job_completed_plates=F("job_plate_count")),
                    then=Value(PrintJob.DISPLAY_COMPLETED),
                ),
                When(
                    Q(job_plate_count__gt=0, job_failed_plates__gt=0)
                    & Q(job_plate_count=F("job_completed_plates") + F("job_failed_plates")),
                    then=Value(PrintJob.DISPLAY_PARTIALLY_FAILED),
                ),
                default=Value(PrintJob.DISPLAY_IN_PROGRESS),
                output_field=CharField(),
            )
        )

    def contents_editable(self) -> PrintJobQuerySet:
        """Return jobs whose parts may still change (see :attr:`PrintJob.can_change_contents`).

        That is: no print attempt yet and the slicer is not working on the job.
        Adding a part to a sliced (or slicing-failed) job discards its plates.
        """
        from core.models.queue import PrintQueue

        return self.exclude(status__in=PrintJob.SLICING_IN_PROGRESS).exclude(
            Exists(PrintQueue.objects.filter(plate__print_job=OuterRef("pk")))
        )

    def finished(self) -> PrintJobQuerySet:
        """Return jobs whose plates are all completed or failed (display ``completed`` / ``partially_failed``)."""
        return self.with_display_status().filter(annotated_display_status__in=PrintJob.FINISHED_DISPLAY_STATUSES)

    def unfinished(self) -> PrintJobQuerySet:
        """Return all other jobs (drafts, slicing, ready, printing, in progress)."""
        return self.with_display_status().exclude(annotated_display_status__in=PrintJob.FINISHED_DISPLAY_STATUSES)


class PrintJob(models.Model):
    """Tracks a print job that may contain one or more parts.

    Parts are linked via the ``PrintJobPart`` through model.  ``status`` stores
    the **slicing pipeline** only (``draft`` → ``pending`` → ``slicing`` →
    ``sliced`` | ``failed``).  After slicing, each plate of G-code is stored on a
    ``PrintJobPlate`` child object; the print progress is derived from the
    plates' print attempts (:attr:`display_status`).
    """

    STATUS_DRAFT = "draft"
    STATUS_PENDING = "pending"
    STATUS_SLICING = "slicing"
    STATUS_SLICED = "sliced"
    STATUS_FAILED = "failed"
    STATUS_CHOICES = [
        (STATUS_DRAFT, "Draft"),
        (STATUS_PENDING, "Pending"),
        (STATUS_SLICING, "Slicing"),
        (STATUS_SLICED, "Sliced"),
        (STATUS_FAILED, "Slicing failed"),
    ]

    # Derived display status ------------------------------------------------
    DISPLAY_DRAFT = "draft"
    DISPLAY_PENDING = "pending"
    DISPLAY_SLICING = "slicing"
    DISPLAY_SLICING_FAILED = "slicing_failed"
    DISPLAY_READY = "ready"
    DISPLAY_PRINTING = "printing"
    DISPLAY_IN_PROGRESS = "in_progress"
    DISPLAY_COMPLETED = "completed"
    DISPLAY_PARTIALLY_FAILED = "partially_failed"
    DISPLAY_CHOICES = [
        (DISPLAY_DRAFT, "Draft"),
        (DISPLAY_PENDING, "Pending"),
        (DISPLAY_SLICING, "Slicing"),
        (DISPLAY_SLICING_FAILED, "Slicing failed"),
        (DISPLAY_READY, "Ready"),
        (DISPLAY_PRINTING, "Printing"),
        (DISPLAY_IN_PROGRESS, "In progress"),
        (DISPLAY_COMPLETED, "Completed"),
        (DISPLAY_PARTIALLY_FAILED, "Partially failed"),
    ]
    #: Display statuses of a finished job (every plate completed or failed).
    FINISHED_DISPLAY_STATUSES: tuple[str, ...] = (DISPLAY_COMPLETED, DISPLAY_PARTIALLY_FAILED)
    #: Slicing statuses while the slicer works on the job (contents must not change).
    SLICING_IN_PROGRESS: tuple[str, ...] = (STATUS_PENDING, STATUS_SLICING)

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
    status = models.CharField(
        "slicing status",
        max_length=20,
        choices=STATUS_CHOICES,
        default=STATUS_DRAFT,
        db_index=True,
    )

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
    plate_parts_known = models.BooleanField(
        default=True,
        help_text=(
            "Whether the plate parts (which part copies lie on which plate) are reliable enough to count "
            "printed parts. If not, a part counts as printed only once every plate of the job is completed."
        ),
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
    # Indexed: lists sort by it, so the database can stop after one page instead of
    # computing the display-status annotation for every job.
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = PrintJobQuerySet.as_manager()

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
    def last_printer(self) -> PrinterProfile | None:
        """Return the printer of the job's most recent print attempt (``None`` without attempts)."""
        from core.models.printers import PrinterProfile

        return (
            PrinterProfile.objects.filter(queue__plate__print_job=self)
            .select_related("orca_machine_profile")
            .order_by("-queue__pk")
            .first()
        )

    @property
    def bed_dimensions(self) -> tuple[float, float, float] | None:
        """Build volume (x, y, z in mm) of the machine this job is sliced for.

        Uses the job's machine profile and falls back to the printer of the most
        recent print attempt.
        A source only counts if it defines both bed X and Y; a missing
        printable height defaults to 256 mm. The size is the extent of
        ``printable_area`` (max - min); an origin offset (e.g. a bed centred on 0)
        is not modelled.

        Returns:
            ``(x, y, z)`` or ``None`` if no source provides a bed size.
        """
        sources = [self.machine_profile]
        if not self._bed_from(self.machine_profile):
            sources.append(self.last_printer)
        for source in sources:
            size = self._bed_from(source)
            if size is not None:
                return size
        return None

    @staticmethod
    def _bed_from(source: object | None) -> tuple[float, float, float] | None:
        """Return the bed size of a machine profile or printer, or ``None`` if it has none."""
        if source is None:
            return None
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
        """Whether the job has plates and every one of them has a passed attempt."""
        plates = list(self.plates.all())
        return bool(plates) and all(plate.state == PrintJobPlate.STATE_COMPLETED for plate in plates)

    @property
    def plate_count(self) -> int:
        """Number of plates in this job."""
        return self.plates.count()

    # ── Derived print status ────────────────────────────────────────────

    @property
    def print_started(self) -> bool:
        """Return True once any print attempt exists for one of the job's plates.

        From then on the job's contents (parts, machine profile) are fixed, it
        cannot be re-sliced and it cannot be deleted — that would lose the print
        history.  A waiting attempt counts too; removing it from the queue (it
        is deleted when never started) makes the job editable again.
        """
        if "job_has_attempts" in self.__dict__:
            return bool(self.job_has_attempts)
        plates = self._prefetched_plates()
        if plates is not None and all(plate.attempts_prefetched for plate in plates):
            return any(plate.attempts() for plate in plates)
        from core.models.queue import PrintQueue

        return PrintQueue.objects.filter(plate__print_job=self).exists()

    @property
    def can_change_contents(self) -> bool:
        """Return True while parts and the machine profile may change (no attempt, not slicing)."""
        return not self.print_started and self.status not in self.SLICING_IN_PROGRESS

    @property
    def can_reslice(self) -> bool:
        """Return True while re-slicing would not delete print history."""
        return not self.print_started

    @property
    def can_delete(self) -> bool:
        """Return True while the job has no print attempt (deleting it keeps every print history)."""
        return not self.print_started

    def _prefetched_plates(self) -> list[PrintJobPlate] | None:
        """Return the prefetched plates, or ``None`` if they are not in the instance cache."""
        cache = getattr(self, "_prefetched_objects_cache", {})
        if "plates" not in cache:
            return None
        return list(cache["plates"])

    @property
    def display_status(self) -> str:
        """Return the derived display status (``DISPLAY_*``).

        * No print attempt yet → the slicing status (``draft`` / ``pending`` /
          ``slicing`` / ``slicing_failed``, or ``ready`` once sliced).
        * ``printing`` – a plate is printing (or awaiting review) right now.
        * ``completed`` – every plate has a passed attempt.
        * ``partially_failed`` – every plate is completed or failed, at least one failed.
        * ``in_progress`` – any other job with attempts (plates still to queue or waiting).

        Uses the :meth:`PrintJobQuerySet.with_display_status` annotation when present.
        """
        if "annotated_display_status" in self.__dict__:
            return self.annotated_display_status
        plates = list(self.plates.all())
        states = [plate.state for plate in plates]
        if not any(plate.attempts() for plate in plates):
            return {
                self.STATUS_FAILED: self.DISPLAY_SLICING_FAILED,
                self.STATUS_SLICED: self.DISPLAY_READY,
                self.STATUS_PENDING: self.DISPLAY_PENDING,
                self.STATUS_SLICING: self.DISPLAY_SLICING,
            }.get(self.status, self.DISPLAY_DRAFT)
        if any(plate.has_active_attempt for plate in plates):
            return self.DISPLAY_PRINTING
        completed = states.count(PrintJobPlate.STATE_COMPLETED)
        failed = states.count(PrintJobPlate.STATE_FAILED)
        if states and completed == len(states):
            return self.DISPLAY_COMPLETED
        if states and failed and completed + failed == len(states):
            return self.DISPLAY_PARTIALLY_FAILED
        return self.DISPLAY_IN_PROGRESS

    def get_display_status_display(self) -> str:
        """Return the human-readable label of :attr:`display_status`."""
        return dict(self.DISPLAY_CHOICES)[self.display_status]

    @property
    def is_finished(self) -> bool:
        """Return True once every plate is completed or failed."""
        return self.display_status in self.FINISHED_DISPLAY_STATUSES

    @property
    def started_at(self) -> datetime | None:
        """Return when the first print attempt of the job started (derived)."""
        from core.models.queue import PrintQueue

        return PrintQueue.objects.filter(plate__print_job=self).aggregate(first=Min("started_at"))["first"]

    @property
    def completed_at(self) -> datetime | None:
        """Return when the last plate passed, once all plates are completed (derived)."""
        plates = list(self.plates.all())
        if not plates or any(plate.state != PrintJobPlate.STATE_COMPLETED for plate in plates):
            return None
        # Per plate (index on plate, or the prefetched attempts) — not a scan over all passed attempts.
        times = [plate.completed_at for plate in plates if plate.completed_at is not None]
        return max(times) if times else None

    # ── Transitions ─────────────────────────────────────────────────────

    def lock_for_contents_change(self) -> PrintJob:
        """Re-read the job under lock and check that its contents may still change.

        Call inside ``transaction.atomic()`` before changing parts or the machine
        profile, so the decision (and whether the slicing must be discarded) uses
        the current row, not one loaded earlier in the request.

        Returns:
            The freshly read job.

        Raises:
            ValueError: If the job has print attempts, is being sliced or was deleted.
        """
        locked = PrintJob.objects.select_for_update().filter(pk=self.pk).first()
        if locked is None:
            raise ValueError(f"Job {self.pk} no longer exists.")
        if not locked.can_change_contents:
            raise ValueError(f"Job {self.pk} cannot change its contents (print started or slicing).")
        return locked

    # ── Plate parts ─────────────────────────────────────────────────────

    def has_plate_parts(self) -> bool:
        """Return True if any plate of the job has recorded plate parts."""
        return PrintJobPlatePart.objects.filter(plate__print_job=self).exists()

    def unplaced_copies(self) -> dict[int, int]:
        """Count the part copies that lie on no plate (completely).

        A copy of a multi-mesh part counts as unplaced unless all of its meshes
        lie on plates.  Only meaningful once the job has plates with recorded
        plate parts; otherwise (not sliced, or plate assignment unknown) the
        result is empty.

        Returns:
            Part primary key → number of copies not (completely) on any plate;
            parts with every copy placed are omitted.
        """
        if not self.plates.exists() or not (self.plate_parts_known or self.has_plate_parts()):
            return {}
        complete: dict[int, int] = {}
        copies = (
            PrintJobPlatePart.objects.filter(plate__print_job=self)
            .order_by()
            .values("part_id", "copy_index")
            .annotate(on_plates=Sum("meshes_on_plate"), meshes=Max("mesh_count"))
        )
        for row in copies:
            if row["on_plates"] >= row["meshes"]:
                complete[row["part_id"]] = complete.get(row["part_id"], 0) + 1
        missing: dict[int, int] = {}
        for part_id, quantity in self.job_parts.values_list("part_id", "quantity"):
            unplaced = quantity - complete.get(part_id, 0)
            if unplaced > 0:
                missing[part_id] = unplaced
        return missing

    def plate_assignment_warnings(self) -> list[str]:
        """Return user-facing warnings about the plate assignment of the job's parts.

        Returns:
            ``"N× <part> on no plate"`` per part with unplaced copies, or a single
            "assignment unknown" warning if the plates' parts could not be determined.
        """
        if not self.plates.exists():
            return []
        if not self.plate_parts_known and not self.has_plate_parts():
            return [
                "Plate assignment unknown: it could not be determined which parts lie on which plate. "
                "Printed parts of this job count only once every plate is completed."
            ]
        missing = self.unplaced_copies()
        if not missing:
            return []
        from core.models.parts import Part

        labels = _part_labels(Part.objects.filter(job_entries__print_job=self).select_related("project"))
        return [f"{count}× {labels[part_id]} on no plate" for part_id, count in sorted(missing.items())]

    def discard_slicing(self) -> None:
        """Throw the slicing result away and put the job back to ``draft``.

        Deletes the plates (their G-code and preview files are removed on commit
        by the plate delete signal, as for a re-slice) and clears the aggregate
        slicing stats.  Called when the contents change after slicing; the user
        starts the next slice.  Only allowed while :attr:`can_change_contents`.

        Raises:
            ValueError: If the job's contents may not change (print started or slicing).
        """
        with transaction.atomic():
            self.lock_for_contents_change()
            self.plates.all().delete()
            self.status = self.STATUS_DRAFT
            self.slicing_error = ""
            self.slicing_started_at = None
            self.filament_used_grams = None
            self.print_time_estimate = None
            self.save(
                update_fields=[
                    "status",
                    "slicing_error",
                    "slicing_started_at",
                    "filament_used_grams",
                    "print_time_estimate",
                    "updated_at",
                ]
            )


class PrintJobPart(models.Model):
    """Through model linking a part to a print job with quantity."""

    print_job = models.ForeignKey(PrintJob, on_delete=models.CASCADE, related_name="job_parts")
    # ``PROTECT``: a part used in a print job is not deletable (the job is either
    # unfinished or print history); the delete guards explain why before this fires.
    part = models.ForeignKey("core.Part", on_delete=models.PROTECT, related_name="job_entries")
    quantity = models.PositiveIntegerField(
        default=1,
        validators=[MinValueValidator(1)],
        help_text="How many copies of this part are in the job",
    )

    class Meta:
        unique_together = ["print_job", "part"]

    def __str__(self) -> str:
        return f"{self.part.name} ×{self.quantity} in {self.print_job}"


class PrintJobPlateQuerySet(models.QuerySet):
    """Annotations and filters for the derived plate state."""

    def with_state(self) -> PrintJobPlateQuerySet:
        """Annotate ``annotated_state`` (same value as :attr:`PrintJobPlate.state`), computed in SQL."""
        from core.models.queue import START_CLAIM_TIMEOUT, PrintQueue

        attempts = PrintQueue.objects.filter(plate=OuterRef("pk"))
        open_attempts = attempts.filter(result="")
        last_manual = PrintQueue.objects.filter(plate=OuterRef(OuterRef("pk")), is_retry=False).order_by("-pk")
        failures = PrintQueue.objects.filter(
            plate=OuterRef("pk"),
            result=PrintQueue.RESULT_FAILED,
            pk__gte=Coalesce(Subquery(last_manual.values("pk")[:1]), 0),
        )
        last_manual_here = attempts.filter(is_retry=False).order_by("-pk")
        last_attempt = attempts.order_by("-pk")
        queryset = self.annotate(
            plate_failures=_count_subquery(failures, "plate"),
            plate_max_retries=Coalesce(
                Subquery(last_manual_here.values("max_retries")[:1]),
                Subquery(last_attempt.values("max_retries")[:1]),
                0,
                output_field=IntegerField(),
            ),
        )
        claim_cutoff = timezone.now() - START_CLAIM_TIMEOUT
        return queryset.annotate(
            annotated_state=Case(
                When(
                    Exists(attempts.filter(result=PrintQueue.RESULT_PASSED)), then=Value(PrintJobPlate.STATE_COMPLETED)
                ),
                When(
                    Exists(open_attempts.filter(completed_at__isnull=False)),
                    then=Value(PrintJobPlate.STATE_AWAITING_REVIEW),
                ),
                When(Exists(open_attempts.filter(started_at__isnull=False)), then=Value(PrintJobPlate.STATE_PRINTING)),
                When(
                    Exists(open_attempts.filter(start_claimed_at__gte=claim_cutoff)),
                    then=Value(PrintJobPlate.STATE_STARTING),
                ),
                When(Exists(open_attempts), then=Value(PrintJobPlate.STATE_WAITING)),
                When(
                    Q(plate_failures__gt=0, plate_failures__gte=F("plate_max_retries")),
                    then=Value(PrintJobPlate.STATE_FAILED),
                ),
                default=Value(PrintJobPlate.STATE_NOT_QUEUED),
                output_field=CharField(),
            )
        )

    def queueable(self) -> PrintJobPlateQuerySet:
        """Return plates that can be added to the print queue.

        A plate is queueable when its job is sliced, it has G-code, and it is
        neither completed nor already queued (no open attempt) — i.e. its state is
        ``not_queued`` or ``failed`` (a failed plate may be queued again; that
        manual enqueue resets its failure counter).
        """
        from core.models.queue import PrintQueue

        # Cheap indexed pre-filters first (completed plates and open attempts are the bulk of
        # the history), so the state CASE only runs for the remaining candidates.
        attempts = PrintQueue.objects.filter(plate=OuterRef("pk"))
        candidates = (
            self.filter(print_job__status=PrintJob.STATUS_SLICED, gcode_file__isnull=False)
            .exclude(gcode_file="")
            .exclude(Exists(attempts.filter(result=PrintQueue.RESULT_PASSED)))
            .exclude(Exists(attempts.filter(result="")))
        )
        return candidates.with_state().filter(annotated_state__in=PrintJobPlate.QUEUEABLE_STATES)


class PrintJobPlate(models.Model):
    """A single plate (build plate) within a multi-part print job.

    When OrcaSlicer returns multiple plates (ZIP with multiple G-code
    files), each one becomes a PrintJobPlate.  Single-plate jobs still
    get exactly one plate.

    The plate's print state is derived from its print attempts, see :attr:`state`.
    """

    # Derived plate state (priority order of :attr:`state`) ------------------
    STATE_COMPLETED = "completed"
    STATE_WAITING = "waiting"
    STATE_STARTING = "starting"
    STATE_PRINTING = "printing"
    STATE_AWAITING_REVIEW = "awaiting_review"
    STATE_FAILED = "failed"
    STATE_NOT_QUEUED = "not_queued"
    STATE_LABELS = {
        STATE_COMPLETED: "Completed",
        STATE_WAITING: "Queued",
        STATE_STARTING: "Starting",
        STATE_PRINTING: "Printing",
        STATE_AWAITING_REVIEW: "Awaiting review",
        STATE_FAILED: "Failed",
        STATE_NOT_QUEUED: "Not queued",
    }
    #: States of a plate that can be (re-)queued.
    QUEUEABLE_STATES: tuple[str, ...] = (STATE_NOT_QUEUED, STATE_FAILED)
    #: States of a plate with an open attempt.
    QUEUED_STATES: tuple[str, ...] = (STATE_WAITING, STATE_STARTING, STATE_PRINTING, STATE_AWAITING_REVIEW)

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

    # Per-plate stats
    filament_used_grams = models.FloatField(null=True, blank=True)
    print_time_estimate = models.DurationField(null=True, blank=True)

    # Remote printer job tracking (per plate)
    remote_job_id = models.CharField(
        max_length=255,
        blank=True,
        help_text="Filename or job ID on the remote printer after upload",
    )

    objects = PrintJobPlateQuerySet.as_manager()

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

    # ── Attempts ────────────────────────────────────────────────────────

    @property
    def attempts_prefetched(self) -> bool:
        """Return True if ``queue_entries`` is in the prefetch cache."""
        return "queue_entries" in getattr(self, "_prefetched_objects_cache", {})

    def attempts(self) -> list[PrintQueue]:
        """Return the plate's print attempts, oldest first (from the prefetch cache when present).

        Without a prefetch the attempts are loaded once and cached on the instance.
        """
        if self.attempts_prefetched:
            return sorted(self._prefetched_objects_cache["queue_entries"], key=lambda attempt: attempt.pk)
        if "_attempts_cache" not in self.__dict__:
            self._attempts_cache = list(self.queue_entries.select_related("printer").order_by("pk"))
        return self._attempts_cache

    @property
    def open_attempt(self) -> PrintQueue | None:
        """Return the plate's open attempt (no result yet), or ``None``."""
        return next((attempt for attempt in self.attempts() if not attempt.result), None)

    @property
    def passed_attempt(self) -> PrintQueue | None:
        """Return the plate's (first) passed attempt, or ``None``."""
        from core.models.queue import PrintQueue

        return next((attempt for attempt in self.attempts() if attempt.result == PrintQueue.RESULT_PASSED), None)

    @property
    def resolved_attempts(self) -> list[PrintQueue]:
        """Return the plate's resolved attempts (print history), oldest first."""
        return [attempt for attempt in self.attempts() if attempt.result]

    @property
    def has_active_attempt(self) -> bool:
        """Return True while an open attempt keeps a printer busy (printing or awaiting review)."""
        attempt = self.open_attempt
        return attempt is not None and (attempt.started_at is not None or attempt.completed_at is not None)

    # ── Derived state ───────────────────────────────────────────────────

    @property
    def state(self) -> str:
        """Return the derived plate state (``STATE_*``), in this priority:

        1. a passed attempt → ``completed``;
        2. an open attempt → ``waiting`` / ``starting`` / ``printing`` / ``awaiting_review``;
        3. ``max_retries`` failed attempts since the last manual enqueue → ``failed``;
        4. otherwise → ``not_queued``.

        Uses the :meth:`PrintJobPlateQuerySet.with_state` annotation when present.
        """
        if "annotated_state" in self.__dict__:
            return self.annotated_state
        from core.models.queue import PrintQueue

        attempts = self.attempts()
        if any(attempt.result == PrintQueue.RESULT_PASSED for attempt in attempts):
            return self.STATE_COMPLETED
        open_attempt = self.open_attempt
        if open_attempt is not None:
            return {
                PrintQueue.STATE_AWAITING_REVIEW: self.STATE_AWAITING_REVIEW,
                PrintQueue.STATE_PRINTING: self.STATE_PRINTING,
                PrintQueue.STATE_STARTING: self.STATE_STARTING,
            }.get(open_attempt.state, self.STATE_WAITING)
        manual = [attempt for attempt in attempts if not attempt.is_retry]
        cycle = attempts[attempts.index(manual[-1]) :] if manual else attempts
        failures = sum(1 for attempt in cycle if attempt.result == PrintQueue.RESULT_FAILED)
        max_retries = manual[-1].max_retries if manual else (attempts[-1].max_retries if attempts else 0)
        if failures and failures >= max_retries:
            return self.STATE_FAILED
        return self.STATE_NOT_QUEUED

    def get_state_display(self) -> str:
        """Return the human-readable label of :attr:`state`."""
        return self.STATE_LABELS[self.state]

    @property
    def is_queueable(self) -> bool:
        """Return True if the plate can be queued (see :meth:`PrintJobPlateQuerySet.queueable`)."""
        return (
            self.state in self.QUEUEABLE_STATES
            and bool(self.gcode_file)
            and self.print_job.status == PrintJob.STATUS_SLICED
        )

    @property
    def started_at(self) -> datetime | None:
        """Return when the plate's latest attempt started (derived)."""
        started = [attempt.started_at for attempt in self.attempts() if attempt.started_at is not None]
        return max(started) if started else None

    @property
    def completed_at(self) -> datetime | None:
        """Return when the plate passed (``resolved_at`` of its passed attempt), else ``None``."""
        attempt = self.passed_attempt
        return attempt.resolved_at if attempt is not None else None

    def parts_summary(self) -> list[str]:
        """Return the parts on this plate as ``"<part> ×<copies>"`` labels (from the prefetch cache when present).

        Copies of a multi-mesh part with only some meshes on this plate are
        labelled ``"<part> (partial) ×<copies>"``.  Parts sharing a name are told
        apart by their project.
        """
        if "plate_parts" in getattr(self, "_prefetched_objects_cache", {}):
            entries = list(self.plate_parts.all())
        else:
            entries = list(self.plate_parts.select_related("part__project"))
        labels = _part_labels(entry.part for entry in entries)
        counts: dict[tuple[int, bool], int] = {}
        for entry in entries:
            key = (entry.part_id, entry.meshes_on_plate < entry.mesh_count)
            counts[key] = counts.get(key, 0) + 1
        return [
            f"{labels[part_id]}{' (partial)' if partial else ''} ×{count}"
            for (part_id, partial), count in counts.items()
        ]

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


class PrintJobPlatePart(models.Model):
    """Which part copy (or which share of its meshes) lies on a plate.

    Recorded from the G-code object labels when a job is sliced (see
    :mod:`core.services.gcode_objects`).  A copy of a part with several meshes
    (multi-object 3MF) can be split across plates; it is complete once the
    ``meshes_on_plate`` of all its rows add up to ``mesh_count``.
    """

    plate = models.ForeignKey(PrintJobPlate, on_delete=models.CASCADE, related_name="plate_parts")
    # ``PROTECT``: a plate's parts are print history once the plate has attempts.
    part = models.ForeignKey("core.Part", on_delete=models.PROTECT, related_name="plate_entries")
    copy_index = models.PositiveIntegerField(help_text="Copy of the part within the job (0-based)")
    meshes_on_plate = models.PositiveIntegerField(
        default=1,
        validators=[MinValueValidator(1)],
        help_text="How many meshes of this copy lie on the plate",
    )
    mesh_count = models.PositiveIntegerField(
        default=1,
        validators=[MinValueValidator(1)],
        help_text="Total number of meshes of the copy",
    )

    class Meta:
        ordering = ["plate", "part", "copy_index"]
        constraints = [
            models.UniqueConstraint(fields=["plate", "part", "copy_index"], name="printjobplatepart_unique_copy"),
        ]

    def __str__(self) -> str:
        share = "" if self.meshes_on_plate == self.mesh_count else f" ({self.meshes_on_plate}/{self.mesh_count} meshes)"
        return f"{self.part.name} copy {self.copy_index + 1}{share} on {self.plate}"
