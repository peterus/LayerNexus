"""PrintQueue model for the LayerNexus application."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from django.conf import settings
from django.db import models, transaction
from django.db.models import CheckConstraint, Count, Index, Max, OuterRef, Q, Subquery, UniqueConstraint
from django.db.models.functions import Coalesce
from django.utils import timezone

if TYPE_CHECKING:
    from core.models.printers import PrinterProfile
    from core.models.printing import PrintJob, PrintJobPlate


#: How long a start claim (``PrintQueue.start_claimed_at``) keeps the printer
#: busy.  Longer than the G-code upload + start timeouts; a claim left behind
#: by a crashed process expires after this.
START_CLAIM_TIMEOUT = timedelta(minutes=10)

#: Queue priorities (higher runs first); shared with ``PrintJob.requeue_priority``.
PRIORITY_CHOICES = [
    (1, "Low"),
    (2, "Normal"),
    (3, "High"),
    (4, "Urgent"),
]


class PrintQueueQuerySet(models.QuerySet):
    """Filters for the derived state of print attempts (``PrintQueue`` rows)."""

    def open(self) -> PrintQueueQuerySet:
        """Return attempts without a result (waiting, printing or awaiting review)."""
        return self.filter(result="")

    def resolved(self) -> PrintQueueQuerySet:
        """Return attempts with a result (passed, failed or cancelled)."""
        return self.exclude(result="")

    def waiting(self) -> PrintQueueQuerySet:
        """Return open attempts the printer has not started yet (includes "starting")."""
        return self.open().filter(started_at__isnull=True, completed_at__isnull=True)

    def printing(self) -> PrintQueueQuerySet:
        """Return open attempts the printer is printing right now."""
        return self.open().filter(started_at__isnull=False, completed_at__isnull=True)

    def awaiting_review(self) -> PrintQueueQuerySet:
        """Return open attempts the printer has finished that await the operator's verdict."""
        return self.open().filter(completed_at__isnull=False)

    def active(self) -> PrintQueueQuerySet:
        """Return open attempts that keep their printer busy (printing or awaiting review)."""
        return self.filter(PrintQueue.active_q())

    def with_retry_count(self) -> PrintQueueQuerySet:
        """Annotate ``retry_count``: failed attempts of the plate since its last manual enqueue.

        Same value as :meth:`PrintJobPlate.failed_attempts_since_manual_enqueue`,
        computed in SQL so lists stay free of N+1 queries.
        """
        last_manual = (
            PrintQueue.objects.filter(plate=OuterRef(OuterRef("plate")), is_retry=False)
            .order_by("-pk")
            .values("pk")[:1]
        )
        failed = (
            PrintQueue.objects.filter(
                plate=OuterRef("plate"), result=PrintQueue.RESULT_FAILED, pk__gte=Subquery(last_manual)
            )
            .order_by()
            .values("plate")
            .annotate(count=Count("pk"))
            .values("count")
        )
        return self.annotate(retry_count=Coalesce(Subquery(failed), 0))

    def with_exclusion_count(self) -> PrintQueueQuerySet:
        """Annotate the number of part copies excluded in each attempt (read via :attr:`PrintQueue.exclusion_count`)."""
        excluded = (
            PrintAttemptExclusion.objects.filter(attempt=OuterRef("pk"))
            .order_by()
            .values("attempt")
            .annotate(count=Count("pk"))
            .values("count")
        )
        return self.annotate(annotated_exclusion_count=Coalesce(Subquery(excluded), 0))


class PrintQueue(models.Model):
    """One print attempt of a plate on a printer (a queue entry).

    Rows are kept after the print as the plate's print history.  Only facts are
    stored (timestamps, the operator's ``result``); the state is derived — see
    :attr:`state`:

    * **waiting** – no result, not started (**starting** while a start claim is active).
    * **printing** – no result, started, not finished.
    * **awaiting_review** – no result, the printer reported the end.
    * **passed** / **failed** / **cancelled** – the attempt is resolved.

    All transitions are methods on this model (``enqueue``, ``mark_started``,
    ``mark_finished``, ``resolve``, ``remove``).  The plate state and the job's
    display status are derived from these rows (``PrintJobPlate.state``,
    ``PrintJob.display_status``); nothing else is stored.
    """

    # Attempt result (operator verdict) -----------------------------------
    RESULT_PASSED = "passed"
    RESULT_FAILED = "failed"
    RESULT_CANCELLED = "cancelled"
    RESULT_CHOICES = [
        ("", "Open"),
        (RESULT_PASSED, "Passed"),
        (RESULT_FAILED, "Failed"),
        (RESULT_CANCELLED, "Cancelled"),
    ]

    # Derived attempt state ------------------------------------------------
    STATE_WAITING = "waiting"
    STATE_STARTING = "starting"
    STATE_PRINTING = "printing"
    STATE_AWAITING_REVIEW = "awaiting_review"
    STATE_LABELS = {
        STATE_WAITING: "Waiting",
        STATE_STARTING: "Starting",
        STATE_PRINTING: "Printing",
        STATE_AWAITING_REVIEW: "Awaiting Review",
        RESULT_PASSED: "Passed",
        RESULT_FAILED: "Failed",
        RESULT_CANCELLED: "Cancelled",
    }

    PRIORITY_CHOICES = PRIORITY_CHOICES

    # ``null=True`` exists only so historical rows/backfills without a plate can
    # persist; the application treats a plate as REQUIRED — ``PrintQueueForm``
    # keeps it mandatory (``blank`` stays False) and every consumer dereferences
    # ``plate.plate_number``. Do NOT add ``blank=True`` here: it would make the
    # form field optional and let a plateless entry crash the queue views.
    plate = models.ForeignKey(
        "PrintJobPlate",
        on_delete=models.CASCADE,
        related_name="queue_entries",
        null=True,
        help_text="The specific plate (G-code) to print",
    )
    # ``null=True`` only for backfilled historical attempts whose printer is
    # unknown; ``PROTECT`` keeps the print history when a printer is deleted
    # (deactivate it instead, see ``PrinterProfile.is_active``).
    printer = models.ForeignKey(
        "core.PrinterProfile",
        on_delete=models.PROTECT,
        null=True,
        related_name="queue",
    )
    priority = models.IntegerField(choices=PRIORITY_CHOICES, default=2)
    position = models.PositiveIntegerField(default=0, help_text="Position in the queue (0 = next)")

    # Result ----------------------------------------------------------------
    result = models.CharField(
        max_length=20,
        choices=RESULT_CHOICES,
        blank=True,
        default="",
        db_index=True,
        help_text="Operator verdict; empty while the attempt is open",
    )
    resolved_at = models.DateTimeField(null=True, blank=True, help_text="When the result was set")
    is_retry = models.BooleanField(
        default=False,
        help_text="Created by the system after a failed attempt (not enqueued by a user)",
    )

    # Retry logic ---------------------------------------------------------
    max_retries = models.PositiveIntegerField(
        default=3,
        help_text="Maximum failed attempts (since the last manual enqueue) before the plate counts as failed",
    )

    # Timestamps ----------------------------------------------------------
    added_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True, blank=True, help_text="When printing started")
    completed_at = models.DateTimeField(null=True, blank=True, help_text="When the printer finished (before review)")

    # Live status from the printer backend (populated by moonraker_worker) ---
    progress = models.FloatField(
        null=True,
        blank=True,
        help_text="Live print progress 0..1 from the printer backend",
    )
    status_updated_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="Last time the status was updated from the printer backend",
    )
    last_error = models.TextField(
        blank=True,
        default="",
        help_text="Last error reported by the printer backend (if any)",
    )
    start_claimed_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="Set while a print start (G-code upload) is in progress; the printer counts as busy meanwhile",
    )
    excluded_objects = models.JSONField(
        default=list,
        blank=True,
        help_text="Object names the printer excluded during the attempt (Klipper EXCLUDE_OBJECT), as reported",
    )

    objects = PrintQueueQuerySet.as_manager()

    class Meta:
        ordering = ["-priority", "position", "added_at"]
        indexes = [
            Index(fields=["printer", "result"], name="printqueue_printer_result_idx"),
        ]
        constraints = [
            CheckConstraint(
                condition=Q(priority__gte=1, priority__lte=4),
                name="printqueue_priority_between_1_and_4",
            ),
            CheckConstraint(
                condition=Q(position__gte=0),
                name="printqueue_position_gte_0",
            ),
            UniqueConstraint(
                fields=["plate"],
                condition=Q(result=""),
                name="printqueue_one_open_attempt_per_plate",
            ),
        ]
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

    # ── Derived state ───────────────────────────────────────────────────

    @property
    def is_resolved(self) -> bool:
        """Return True once the attempt has a result."""
        return bool(self.result)

    @property
    def has_active_start_claim(self) -> bool:
        """Return True while an unexpired print-start claim is set."""
        return self.start_claimed_at is not None and self.start_claimed_at >= timezone.now() - START_CLAIM_TIMEOUT

    @property
    def state(self) -> str:
        """Return the derived attempt state (``STATE_*`` or ``RESULT_*`` value)."""
        if self.result:
            return self.result
        if self.completed_at is not None:
            return self.STATE_AWAITING_REVIEW
        if self.started_at is not None:
            return self.STATE_PRINTING
        if self.has_active_start_claim:
            return self.STATE_STARTING
        return self.STATE_WAITING

    def get_state_display(self) -> str:
        """Return the human-readable label of :attr:`state`."""
        return self.STATE_LABELS[self.state]

    @property
    def is_waiting(self) -> bool:
        """Return True for an open attempt the printer has not started (incl. "starting")."""
        return not self.result and self.started_at is None and self.completed_at is None

    @property
    def is_printing(self) -> bool:
        """Return True while the printer is printing this attempt."""
        return self.state == self.STATE_PRINTING

    @property
    def is_awaiting_review(self) -> bool:
        """Return True while the attempt awaits the operator's verdict."""
        return self.state == self.STATE_AWAITING_REVIEW

    @classmethod
    def active_q(cls) -> Q:
        """Return a filter for open attempts that keep their printer busy (printing or awaiting review)."""
        return Q(result="") & (Q(started_at__isnull=False) | Q(completed_at__isnull=False))

    @classmethod
    def history_q(cls) -> Q:
        """Return a filter for attempts that are print history (started or resolved)."""
        return ~Q(result="") | Q(started_at__isnull=False) | Q(completed_at__isnull=False)

    @classmethod
    def protected_q(cls) -> Q:
        """Return a filter for attempts whose plate must not be deleted.

        That is print history (:meth:`history_q`) plus a print start in
        progress — deleting it mid-upload would let the print run untracked.
        """
        return cls.history_q() | cls.active_start_claim_q()

    @property
    def is_history(self) -> bool:
        """Return True once the attempt was started or resolved (it must not be deleted)."""
        return bool(self.result) or self.started_at is not None or self.completed_at is not None

    @classmethod
    def active_start_claim_q(cls) -> Q:
        """Return a filter for waiting entries whose print start is in progress (unexpired claim)."""
        return Q(
            result="",
            started_at__isnull=True,
            completed_at__isnull=True,
            start_claimed_at__gte=timezone.now() - START_CLAIM_TIMEOUT,
        )

    @classmethod
    def blocks_job_delete_q(cls) -> Q:
        """Return a filter for attempts that block deleting their print job (see :attr:`PrintJob.can_delete`).

        That is an attempt keeping its printer busy (printing, or finished and
        awaiting review — the parts may still be on the bed) or a print start in
        progress.  Deleting the job would free the printer, and the next queued
        or scheduled print could start onto a full bed.
        """
        return cls.active_q() | cls.active_start_claim_q()

    @property
    def blocks_job_delete(self) -> bool:
        """Return True while this attempt blocks deleting its job (same rule as :meth:`blocks_job_delete_q`)."""
        if self.result:
            return False
        if self.started_at is not None or self.completed_at is not None:
            return True
        return self.has_active_start_claim

    @property
    def is_printer_busy(self) -> bool:
        """Return True if this entry's printer has an active or unreviewed job, or a start in progress."""
        active = self.active_q() & ~Q(pk=self.pk)
        return PrintQueue.objects.filter(active | self.active_start_claim_q(), printer=self.printer).exists()

    # ── Excluded part copies ────────────────────────────────────────────

    #: Attempt states whose excluded copies an operator may correct.
    EXCLUSION_EDITABLE_STATES: tuple[str, ...] = (STATE_AWAITING_REVIEW, RESULT_PASSED)

    @property
    def can_edit_exclusions(self) -> bool:
        """Return True if copies of this attempt may be marked as not ok (awaiting review or passed)."""
        return self.state in self.EXCLUSION_EDITABLE_STATES

    @property
    def exclusion_count(self) -> int:
        """Return the number of part copies excluded in this attempt.

        Uses the :meth:`PrintQueueQuerySet.with_exclusion_count` annotation or the
        prefetched ``exclusions`` when present, else one query.
        """
        annotated = getattr(self, "annotated_exclusion_count", None)
        if annotated is not None:
            return annotated
        if "exclusions" in getattr(self, "_prefetched_objects_cache", {}):
            return len(self.exclusions.all())
        return self.exclusions.count()

    def excluded_summary(self) -> str:
        """Return ``"N part(s) excluded"`` (or ``"N object(s) excluded"`` if unmapped), ``""`` if none."""
        count = self.exclusion_count
        if count:
            return f"{count} part(s) excluded"
        if self.excluded_objects:
            return f"{len(self.excluded_objects)} object(s) excluded"
        return ""

    def record_excluded_objects(self, names: Iterable[Any]) -> bool:
        """Record the objects the printer reports as excluded (Klipper ``exclude_object.excluded_objects``).

        Names not recorded yet (compared case-insensitively) are appended to
        :attr:`excluded_objects`, and the part copies of the plate whose object
        IDs they name (see :func:`core.services.gcode_objects.parse_klipper_object_id`
        and ``PrintJobPlatePart.object_ids``) get a ``klipper`` exclusion.  Names
        already recorded are skipped, so a copy an operator un-marked stays
        un-marked; a shorter list (Klipper resets it per print) drops nothing.
        Ignored once the attempt is resolved (conditional write in the database).

        Args:
            names: The reported object names (non-strings are ignored).

        Returns:
            True if new names were recorded, False otherwise.
        """
        from core.services.gcode_objects import parse_klipper_object_id

        known = {str(name).upper() for name in self.excluded_objects}
        new_names: list[str] = []
        for name in names:
            if isinstance(name, str) and name and name.upper() not in known:
                known.add(name.upper())
                new_names.append(name)
        if not new_names:
            return False
        merged = [*self.excluded_objects, *new_names]
        with transaction.atomic():
            if not PrintQueue.objects.open().filter(pk=self.pk).update(excluded_objects=merged):
                return False
            self.excluded_objects = merged
            object_ids = {parse_klipper_object_id(name) for name in new_names} - {None}
            if object_ids and self.plate_id is not None:
                from core.models.printing import PrintJobPlatePart

                plate_parts = PrintJobPlatePart.objects.filter(plate_id=self.plate_id).only("id", "object_ids")
                PrintAttemptExclusion.objects.bulk_create(
                    [
                        PrintAttemptExclusion(
                            attempt=self, plate_part=plate_part, source=PrintAttemptExclusion.SOURCE_KLIPPER
                        )
                        for plate_part in plate_parts
                        if object_ids.intersection(plate_part.object_ids or [])
                    ],
                    ignore_conflicts=True,
                )
        return True

    def set_excluded_plate_parts(self, plate_part_ids: Iterable[int], *, user: Any = None) -> None:
        """Set which part copies of the plate were not printed ok in this attempt (manual correction).

        Copies in ``plate_part_ids`` without an exclusion get a ``manual`` one;
        exclusions of other copies (from Klipper or manual) are removed.

        Args:
            plate_part_ids: Primary keys of ``PrintJobPlatePart`` rows of this attempt's plate.
            user: The operator making the correction (stored on new rows).

        Raises:
            ValueError: If the attempt is neither awaiting review nor passed, or
                an ID is not a plate part of its plate.
        """
        from core.models.printing import PrintJobPlatePart

        if not self.can_edit_exclusions:
            raise ValueError(f"Attempt {self.pk} is not awaiting review or passed (state: {self.state}).")
        wanted = {int(pk) for pk in plate_part_ids}
        plate_parts = PrintJobPlatePart.objects.filter(plate_id=self.plate_id, pk__in=wanted)
        valid = set(plate_parts.values_list("pk", flat=True))
        if wanted - valid:
            raise ValueError(f"Plate parts {sorted(wanted - valid)} do not belong to attempt {self.pk}.")
        with transaction.atomic():
            self.exclusions.exclude(plate_part_id__in=wanted).delete()
            existing = set(self.exclusions.values_list("plate_part_id", flat=True))
            PrintAttemptExclusion.objects.bulk_create(
                [
                    PrintAttemptExclusion(
                        attempt=self,
                        plate_part_id=pk,
                        source=PrintAttemptExclusion.SOURCE_MANUAL,
                        created_by=user if getattr(user, "is_authenticated", False) else None,
                    )
                    for pk in sorted(wanted - existing)
                ],
                ignore_conflicts=True,
            )

    # ── Transitions ─────────────────────────────────────────────────────

    def mark_started(self, *, remote_filename: str = "", when: datetime | None = None) -> None:
        """Record that the printer accepted the start of this attempt.

        Clears the start claim and the live data of a previous attempt and
        stores the remote file name on the plate.

        Args:
            remote_filename: The G-code file name on the printer (stored on the plate).
            when: Start time (defaults to now).
        """
        when = when or timezone.now()
        # Two separate writes on purpose: the printer is already printing, so
        # the attempt's start must be recorded even if the plate update below
        # fails (otherwise the entry could be started twice).
        self.started_at = when
        self.start_claimed_at = None
        self.progress = None
        self.status_updated_at = None
        self.save(update_fields=["started_at", "start_claimed_at", "progress", "status_updated_at"])
        plate = self.plate
        if plate is not None and remote_filename:
            plate.remote_job_id = remote_filename
            plate.save(update_fields=["remote_job_id"])

    def mark_finished(self, *, when: datetime | None = None, error: str | None = None) -> bool:
        """Record that the printer finished (or stopped) this attempt; it now awaits review.

        The update is conditional in the database: an attempt that has been
        resolved (e.g. removed as ``cancelled``) or already finished in the
        meantime is left untouched, even if this instance is stale.

        Args:
            when: Finish time (defaults to now).
            error: Error reported by the printer; ``""`` clears a previous
                one, ``None`` keeps ``last_error`` unchanged.

        Returns:
            True if the attempt was moved to "awaiting review", False if ignored.
        """
        when = when or timezone.now()
        values: dict[str, Any] = {"completed_at": when, "status_updated_at": when}
        if error is not None:
            values["last_error"] = error
        updated = PrintQueue.objects.printing().filter(pk=self.pk).update(**values)
        if updated:
            for field, value in values.items():
                setattr(self, field, value)
        return bool(updated)

    @classmethod
    def enqueue(cls, *, plate: PrintJobPlate, printer: PrinterProfile, priority: int = 2) -> PrintQueue:
        """Add a user-requested attempt for ``plate`` at the end of ``printer``'s queue.

        Args:
            plate: The plate to print.
            printer: The printer to print on.
            priority: Queue priority (1-4).

        Returns:
            The new open attempt.
        """
        with transaction.atomic():
            max_pos = cls.objects.open().filter(printer=printer).aggregate(m=Max("position"))["m"] or 0
            entry = cls.objects.create(plate=plate, printer=printer, priority=priority, position=max_pos + 1)
        return entry

    def resolve(self, passed: bool) -> str:
        """Set the operator's verdict on an attempt awaiting review.

        **Pass:** ``result=passed``; the plate is completed.

        **Fail:** ``result=failed``.  While the plate's failed attempts since
        the last manual enqueue stay below ``max_retries``, a new open attempt
        (``is_retry=True``) is created first in its priority group
        (``position=0``) on the same printer; otherwise the plate counts as
        failed.

        Callers that must guard against concurrent reviews lock the row first
        (see ``core.services.queue.review_queue_entry``).

        Args:
            passed: True for Pass, False for Fail.

        Returns:
            ``"passed"``, ``"requeued"`` or ``"discarded"``.

        Raises:
            ValueError: If the attempt is not awaiting review.
        """
        if self.state != self.STATE_AWAITING_REVIEW:
            raise ValueError(f"Attempt {self.pk} is not awaiting review (state: {self.state}).")
        with transaction.atomic():
            self.result = self.RESULT_PASSED if passed else self.RESULT_FAILED
            self.resolved_at = timezone.now()
            self.save(update_fields=["result", "resolved_at"])
            plate = self.plate
            outcome = self.RESULT_PASSED
            if not passed:
                failures = plate.failed_attempts_since_manual_enqueue() if plate is not None else 0
                if plate is not None and failures < self.max_retries:
                    # Waiting positions are renumbered 1..n by manual reordering;
                    # position 0 puts the retry first within its priority group
                    # (it was the entry being printed, i.e. it was next in line).
                    PrintQueue.objects.create(
                        plate=plate,
                        printer=self.printer,
                        priority=self.priority,
                        position=0,
                        max_retries=self.max_retries,
                        is_retry=True,
                    )
                    outcome = "requeued"
                else:
                    outcome = "discarded"
        return outcome

    def remove(self) -> None:
        """Remove the attempt from the queue (manual dequeue).

        An attempt that never started is deleted.  A started (or finished)
        attempt is kept as history with ``result=cancelled``; it never counts
        as a failed attempt and frees the plate for a new attempt.
        """
        if self.started_at is None and self.completed_at is None:
            self.delete()
        else:
            self.result = self.RESULT_CANCELLED
            self.resolved_at = timezone.now()
            self.save(update_fields=["result", "resolved_at"])


class PrintAttemptExclusion(models.Model):
    """A part copy of a plate that was not printed (ok) in one print attempt.

    Either excluded on the printer during the print (Klipper ``EXCLUDE_OBJECT``,
    recorded by the worker) or marked by an operator during or after the
    review.  An excluded copy of a passed attempt does not count as printed
    (see :attr:`Part.printed_quantity`); a copy of several meshes is not printed
    as soon as one of its meshes on the plate is excluded.
    """

    SOURCE_KLIPPER = "klipper"
    SOURCE_MANUAL = "manual"
    SOURCE_CHOICES = [
        (SOURCE_KLIPPER, "Excluded on the printer"),
        (SOURCE_MANUAL, "Marked by an operator"),
    ]

    attempt = models.ForeignKey(PrintQueue, on_delete=models.CASCADE, related_name="exclusions")
    plate_part = models.ForeignKey("core.PrintJobPlatePart", on_delete=models.CASCADE, related_name="exclusions")
    source = models.CharField(max_length=20, choices=SOURCE_CHOICES)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="print_attempt_exclusions",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["attempt", "plate_part"]
        constraints = [
            UniqueConstraint(fields=["attempt", "plate_part"], name="printattemptexclusion_unique_copy"),
        ]

    def __str__(self) -> str:
        return f"{self.plate_part} excluded in attempt {self.attempt_id} ({self.get_source_display()})"


class ScheduledQueueStart(models.Model):
    """One-shot, time-controlled start of the next queue job on a printer.

    Created in the evening ("start tomorrow 06:00") and executed exactly once by
    the worker; it is never recurring.  At most one ``pending`` row exists per
    printer.
    """

    STATUS_PENDING = "pending"
    STATUS_STARTED = "started"
    STATUS_SKIPPED = "skipped"
    STATUS_FAILED = "failed"
    STATUS_CANCELLED = "cancelled"
    STATUS_CHOICES = [
        (STATUS_PENDING, "Pending"),
        (STATUS_STARTED, "Started"),
        (STATUS_SKIPPED, "Skipped"),
        (STATUS_FAILED, "Failed"),
        (STATUS_CANCELLED, "Cancelled"),
    ]

    printer = models.ForeignKey("core.PrinterProfile", on_delete=models.CASCADE, related_name="scheduled_starts")
    start_at = models.DateTimeField(help_text="When the next queue job should be started")
    created_by = models.ForeignKey(
        "auth.User",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="scheduled_queue_starts",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_PENDING, db_index=True)
    executed_at = models.DateTimeField(null=True, blank=True, help_text="When the worker processed this schedule")
    result_message = models.TextField(blank=True, default="")

    class Meta:
        ordering = ["start_at"]
        constraints = [
            UniqueConstraint(
                fields=["printer"],
                condition=Q(status="pending"),
                name="scheduledqueuestart_one_pending_per_printer",
            ),
        ]

    def __str__(self) -> str:
        return f"Scheduled start on {self.printer} at {self.start_at:%Y-%m-%d %H:%M} [{self.status}]"
