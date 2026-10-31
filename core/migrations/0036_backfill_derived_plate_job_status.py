"""Prepare deriving the plate state and the job's print status from print attempts.

The next migration drops ``PrintJobPlate.status`` / ``started_at`` /
``completed_at`` and ``PrintJob.printer`` / ``started_at`` / ``completed_at``;
from then on the plate state and the job's display status are computed from
the ``PrintQueue`` rows (print attempts).  This migration makes sure nothing
that is stored today gets lost by that:

1. **Plates** — since ``0033`` the stored plate status is only written from the
   attempts, so it normally matches already.  Defensively (e.g. a status edited
   in the admin), a ``completed`` plate without a passed attempt gets one
   (times from the plate, printer from ``PrintJob.printer``), and a ``failed``
   plate that would not derive as failed gets the missing ``failed`` attempts.
   A plate never derives *lower* than its stored status, so printed quantities
   and project progress cannot drop.
2. **Jobs** — ``PrintJob.status`` keeps only the slicing pipeline: the print
   statuses ``printing`` / ``completed`` and the never-set ``uploading`` /
   ``uploaded`` / ``cancelled`` become ``sliced`` (the print status is derived).

Reversing re-derives the dropped columns from the attempts (after the next
migration's reverse has re-added them).  Idempotent.
"""

from django.db import migrations
from django.utils import timezone

#: Default ``PrintQueue.max_retries`` (failed attempts that make a plate "failed").
DEFAULT_MAX_RETRIES = 3

#: Former print statuses of ``PrintJob.status``; all become ``sliced``.
PRINT_PHASE_STATUSES = ("printing", "completed", "uploading", "uploaded", "cancelled")


def _cycle(attempts: list) -> tuple[list, int]:
    """Return the attempts since the last manual enqueue and that enqueue's ``max_retries``."""
    manual = [index for index, attempt in enumerate(attempts) if not attempt.is_retry]
    if manual:
        start = manual[-1]
        return attempts[start:], attempts[start].max_retries
    return attempts, attempts[-1].max_retries if attempts else DEFAULT_MAX_RETRIES


def _derived_state(attempts: list) -> str:
    """Derive the plate state like ``PrintJobPlate.state`` (attempts oldest first)."""
    if any(attempt.result == "passed" for attempt in attempts):
        return "completed"
    open_attempt = next((attempt for attempt in attempts if not attempt.result), None)
    if open_attempt is not None:
        started = open_attempt.started_at is not None or open_attempt.completed_at is not None
        return "printing" if started else "waiting"
    cycle, max_retries = _cycle(attempts)
    failures = sum(1 for attempt in cycle if attempt.result == "failed")
    if failures and failures >= max_retries:
        return "failed"
    return "not_queued"


def backfill(apps, schema_editor) -> None:
    """Back the stored plate status with attempts and reduce the job status to slicing (see module docstring)."""
    PrintJob = apps.get_model("core", "PrintJob")
    PrintJobPlate = apps.get_model("core", "PrintJobPlate")
    PrintQueue = apps.get_model("core", "PrintQueue")

    plates = PrintJobPlate.objects.filter(status__in=["completed", "failed"]).select_related("print_job")
    for plate in plates:
        attempts = list(PrintQueue.objects.filter(plate=plate).order_by("pk"))
        state = _derived_state(attempts)
        printer_id = plate.print_job.printer_id
        if plate.status == "completed" and state != "completed":
            PrintQueue.objects.create(
                plate=plate,
                printer_id=printer_id,
                started_at=plate.started_at,
                completed_at=plate.completed_at,
                resolved_at=plate.completed_at or plate.started_at or timezone.now(),
                result="passed",
                is_retry=bool(attempts),
            )
        elif plate.status == "failed" and state == "not_queued":
            cycle, max_retries = _cycle(attempts)
            failures = sum(1 for attempt in cycle if attempt.result == "failed")
            for index in range(max(max_retries, 1) - failures):
                PrintQueue.objects.create(
                    plate=plate,
                    printer_id=printer_id,
                    started_at=plate.started_at,
                    resolved_at=plate.completed_at or plate.started_at or timezone.now(),
                    result="failed",
                    is_retry=bool(attempts) or index > 0,
                    max_retries=max_retries if attempts else DEFAULT_MAX_RETRIES,
                )

    PrintJob.objects.filter(status__in=PRINT_PHASE_STATUSES).update(status="sliced")


def restore_stored_status(apps, schema_editor) -> None:
    """Re-derive the dropped plate / job columns from the attempts (rollback)."""
    PrintJob = apps.get_model("core", "PrintJob")
    PrintJobPlate = apps.get_model("core", "PrintJobPlate")
    PrintQueue = apps.get_model("core", "PrintQueue")

    stored = {"completed": "completed", "printing": "printing", "failed": "failed"}
    for plate in PrintJobPlate.objects.all():
        attempts = list(PrintQueue.objects.filter(plate=plate).order_by("pk"))
        passed = next((attempt for attempt in attempts if attempt.result == "passed"), None)
        started = [attempt.started_at for attempt in attempts if attempt.started_at is not None]
        plate.status = stored.get(_derived_state(attempts), "waiting")
        plate.started_at = max(started) if started else None
        plate.completed_at = passed.resolved_at if passed is not None else None
        plate.save(update_fields=["status", "started_at", "completed_at"])

    for job in PrintJob.objects.filter(status="sliced"):
        plates = list(PrintJobPlate.objects.filter(print_job=job))
        attempts = PrintQueue.objects.filter(plate__print_job=job)
        latest = attempts.filter(printer__isnull=False).order_by("-pk").first()
        job.printer_id = latest.printer_id if latest is not None else None
        started = [a.started_at for a in attempts if a.started_at is not None]
        job.started_at = min(started) if started else None
        if plates and all(plate.status == "completed" for plate in plates):
            job.status = "completed"
            job.completed_at = max((p.completed_at for p in plates if p.completed_at), default=None)
        elif started:
            job.status = "printing"
        job.save(update_fields=["status", "printer", "started_at", "completed_at"])


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0035_print_attempts_irreversible"),
    ]

    operations = [
        migrations.RunPython(backfill, restore_stored_status),
    ]
