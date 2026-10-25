"""PrintQueue model for the LayerNexus application."""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING

from django.db import models
from django.db.models import CheckConstraint, Index, Q, UniqueConstraint
from django.utils import timezone

if TYPE_CHECKING:
    from core.models.printing import PrintJob


#: How long a start claim (``PrintQueue.start_claimed_at``) keeps the printer
#: busy.  Longer than the G-code upload + start timeouts; a claim left behind
#: by a crashed process expires after this.
START_CLAIM_TIMEOUT = timedelta(minutes=10)


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
    printer = models.ForeignKey("core.PrinterProfile", on_delete=models.CASCADE, related_name="queue")
    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default=STATUS_WAITING,
        db_index=True,
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

    class Meta:
        ordering = ["-priority", "position", "added_at"]
        indexes = [
            Index(fields=["status", "printer"], name="printqueue_status_printer_idx"),
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
                condition=Q(status="waiting"),
                name="printqueue_unique_plate_when_waiting",
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

    @classmethod
    def active_start_claim_q(cls) -> Q:
        """Return a filter for waiting entries whose print start is in progress (unexpired claim)."""
        return Q(status=cls.STATUS_WAITING, start_claimed_at__gte=timezone.now() - START_CLAIM_TIMEOUT)

    @property
    def is_printer_busy(self) -> bool:
        """Return True if this entry's printer has an active or unreviewed job, or a start in progress."""
        active = Q(status__in=[self.STATUS_PRINTING, self.STATUS_AWAITING_REVIEW]) & ~Q(pk=self.pk)
        return PrintQueue.objects.filter(active | self.active_start_claim_q(), printer=self.printer).exists()


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
