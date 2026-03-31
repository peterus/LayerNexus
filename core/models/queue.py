"""PrintQueue model for the LayerNexus application."""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.db import models
from django.db.models import CheckConstraint, Index, Q, UniqueConstraint

if TYPE_CHECKING:
    from core.models.printing import PrintJob


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

    class Meta:
        ordering = ["-priority", "position", "added_at"]
        indexes = [
            Index(fields=["status", "printer"], name="printqueue_status_printer_idx"),
        ]
        constraints = [
            CheckConstraint(
                check=Q(priority__gte=1, priority__lte=4),
                name="printqueue_priority_between_1_and_4",
            ),
            CheckConstraint(
                check=Q(position__gte=0),
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
