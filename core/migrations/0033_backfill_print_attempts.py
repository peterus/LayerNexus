"""Backfill print attempts from the stored queue / plate status.

Queue entries used to be deleted after the review; from now on every
``PrintQueue`` row is a print attempt kept as history, and its state is
derived from timestamps + ``result``.  This migration:

1. translates the ``status`` of open queue entries into timestamps
   (``printing`` without ``started_at`` -> set it, ``awaiting_review``
   without ``completed_at`` -> set it, ``waiting`` -> clear both);
2. keeps an open entry's ``retry_count`` as that many ``failed`` attempts
   (so the next failure still discards at the same count);
3. resolves duplicate open entries of one plate (keeps the most advanced
   one; never-started others are deleted, started ones become ``cancelled``), so the new
   "one open attempt per plate" constraint can be added;
3b. resets plates stuck in ``printing`` without an open entry to ``waiting``;
4. creates a ``passed`` attempt for each ``completed`` plate and
   ``max_retries`` ``failed`` attempts for each ``failed`` plate that has
   none yet (printer from ``PrintJob.printer`` if set, else unknown).

Apart from 3b (``printing`` -> ``waiting``) the stored ``PrintJobPlate.status``
is not changed; ``completed`` plates stay untouched, so printed quantities and
project progress stay exactly as before.  Idempotent.
"""

from django.db import migrations
from django.utils import timezone

#: Rank of the old open statuses; the most advanced open entry of a plate is kept open.
_OPEN_RANK = {"awaiting_review": 2, "printing": 1, "waiting": 0}

#: Default ``PrintQueue.max_retries`` (failed attempts that make a plate "failed").
DEFAULT_MAX_RETRIES = 3

#: Legacy ``status`` for created resolved attempts.  The old constraint "one
#: *waiting* entry per plate" still exists until the next migration, so the
#: (soon dropped) column must not keep its ``waiting`` default.
RESOLVED_LEGACY_STATUS = "awaiting_review"


def backfill_print_attempts(apps, schema_editor) -> None:
    """Create/translate print attempts (see module docstring)."""
    PrintQueue = apps.get_model("core", "PrintQueue")
    PrintJobPlate = apps.get_model("core", "PrintJobPlate")
    now = timezone.now()

    # 1 + 3: open entries (all rows without result are pre-migration queue entries).
    open_entries = list(PrintQueue.objects.filter(result="").order_by("pk"))
    by_plate: dict[int | None, list] = {}
    for entry in open_entries:
        when = entry.status_updated_at or entry.added_at or now
        if entry.status == "printing":
            entry.started_at = entry.started_at or when
            entry.completed_at = None
        elif entry.status == "awaiting_review":
            entry.completed_at = entry.completed_at or when
        else:
            entry.started_at = None
            entry.completed_at = None
        entry.save(update_fields=["started_at", "completed_at"])
        by_plate.setdefault(entry.plate_id, []).append(entry)

    for plate_id, entries in by_plate.items():
        if plate_id is None or len(entries) < 2:
            continue
        keep = max(entries, key=lambda e: (_OPEN_RANK.get(e.status, 0), -e.pk))
        for entry in entries:
            if entry.pk == keep.pk:
                continue
            if entry.started_at is None and entry.completed_at is None:
                entry.delete()  # never started: removing it is a delete, as in the app
            else:
                entry.result = "cancelled"
                entry.resolved_at = now
                entry.status = RESOLVED_LEGACY_STATUS
                entry.save(update_fields=["result", "resolved_at", "status"])

    # 2: an open entry's retry_count becomes that many failed attempts after it
    # (the open entry stays the last manual one, so the counter continues).
    for entry in PrintQueue.objects.filter(result="", retry_count__gt=0, plate__isnull=False):
        existing = PrintQueue.objects.filter(plate_id=entry.plate_id, result="failed", pk__gt=entry.pk).count()
        resolved_at = entry.status_updated_at or entry.added_at or now
        for _ in range(max(entry.retry_count - existing, 0)):
            PrintQueue.objects.create(
                plate_id=entry.plate_id,
                printer_id=entry.printer_id,
                priority=entry.priority,
                max_retries=entry.max_retries,
                status=RESOLVED_LEGACY_STATUS,
                result="failed",
                resolved_at=resolved_at,
                is_retry=True,
            )

    # 3b: plates left "printing" without an open entry (old Operator dequeue or
    # direct G-code upload) were stuck; they are not queued, i.e. "waiting".
    PrintJobPlate.objects.filter(status="printing").exclude(queue_entries__result="").update(status="waiting")

    # 4: plates whose attempts were deleted after the review.
    plates = PrintJobPlate.objects.filter(status__in=["completed", "failed"]).select_related("print_job")
    for plate in plates:
        attempts = PrintQueue.objects.filter(plate=plate)
        printer_id = plate.print_job.printer_id
        if plate.status == "completed":
            if attempts.filter(result="passed").exists():
                continue
            PrintQueue.objects.create(
                plate=plate,
                printer_id=printer_id,
                started_at=plate.started_at,
                completed_at=plate.completed_at,
                resolved_at=plate.completed_at or plate.started_at,
                status=RESOLVED_LEGACY_STATUS,
                result="passed",
            )
        else:
            if attempts.filter(result="failed").exists() or attempts.filter(result="").exists():
                continue
            for index in range(DEFAULT_MAX_RETRIES):
                PrintQueue.objects.create(
                    plate=plate,
                    printer_id=printer_id,
                    started_at=plate.started_at,
                    resolved_at=plate.completed_at or plate.started_at,
                    status=RESOLVED_LEGACY_STATUS,
                    result="failed",
                    is_retry=index > 0,
                    max_retries=DEFAULT_MAX_RETRIES,
                )


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0032_print_attempt_fields"),
    ]

    operations = [
        migrations.RunPython(backfill_print_attempts, migrations.RunPython.noop),
    ]
