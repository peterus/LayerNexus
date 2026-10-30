"""PrintQueue model for the LayerNexus application."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

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
        return self.open().filter(Q(started_at__isnull=False) | Q(completed_at__isnull=False))

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


class PrintQueue(models.Model):
    """One print attempt of a plate on a printer (a queue entry).

    Rows are kept after the print as the plate's print history.  Only facts are
    stored (timestamps, the operator's ``result``); the state is derived — see
    :attr:`state`:

    * **waiting** – no result, not started (**starting** while a start claim is active).
    * **printing** – no result, started, not finished.
    * **awaiting_review** – no result, the printer reported the end.
    * **passed** / **failed** / **cancelled** – the attempt is resolved.

    All transitions are methods on this model (``mark_started``,
    ``mark_finished``, ``resolve``, ``remove``); they keep the plate's stored
    status in sync via :meth:`PrintJobPlate.refresh_stored_status`.
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

    PRIORITY_CHOICES = [
        (1, "Low"),
        (2, "Normal"),
        (3, "High"),
        (4, "Urgent"),
    ]

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

    @property
    def is_printer_busy(self) -> bool:
        """Return True if this entry's printer has an active or unreviewed job, or a start in progress."""
        others = PrintQueue.objects.filter(printer=self.printer).exclude(pk=self.pk)
        return (
            others.active().exists()
            or PrintQueue.objects.filter(self.active_start_claim_q(), printer=self.printer).exists()
        )

    # ── Transitions ─────────────────────────────────────────────────────

    def mark_started(self, *, remote_filename: str = "", when: datetime | None = None) -> None:
        """Record that the printer accepted the start of this attempt.

        Clears the start claim and the live data of a previous attempt and
        refreshes the stored plate / job status.

        Args:
            remote_filename: The G-code file name on the printer (stored on the plate).
            when: Start time (defaults to now).
        """
        when = when or timezone.now()
        # Not one transaction on purpose: the printer is already printing, so
        # the attempt's start must be recorded even if the plate/job refresh
        # below fails (otherwise the entry could be started twice).
        self.started_at = when
        self.start_claimed_at = None
        self.progress = None
        self.status_updated_at = None
        self.save(update_fields=["started_at", "start_claimed_at", "progress", "status_updated_at"])
        plate = self.plate
        if plate is not None:
            with transaction.atomic():
                if remote_filename:
                    plate.remote_job_id = remote_filename
                    plate.save(update_fields=["remote_job_id"])
                plate.refresh_stored_status()

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
            plate.refresh_stored_status()
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
            if plate is not None:
                plate.refresh_stored_status()
        return outcome

    def remove(self) -> None:
        """Remove the attempt from the queue (manual dequeue).

        An attempt that never started is deleted.  A started (or finished)
        attempt is kept as history with ``result=cancelled``; it never counts
        as a failed attempt and frees the plate for a new attempt.
        """
        with transaction.atomic():
            plate = self.plate
            if self.started_at is None and self.completed_at is None:
                self.delete()
            else:
                self.result = self.RESULT_CANCELLED
                self.resolved_at = timezone.now()
                self.save(update_fields=["result", "resolved_at"])
            if plate is not None:
                plate.refresh_stored_status()


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
