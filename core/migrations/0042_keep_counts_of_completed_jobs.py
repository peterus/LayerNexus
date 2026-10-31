"""Keep the printed quantities of completed jobs whose copies are not all on plates.

From this release on, ``Part.printed_quantity`` counts a job's printed copies
from its plate parts (copies whose meshes all lie on plates with a passed
attempt).  Until now, a job with every plate completed counted the full job
quantity of each of its parts, also for copies the slicer placed on no plate.

So that no printed quantity drops through the upgrade, such a job — every plate
completed, plate parts recorded and ``plate_parts_known`` set, but a part with
fewer complete copies on the plates than its job quantity — gets
``plate_parts_known=False`` and keeps counting with the old rule (its
"on no plate" warning stays visible).  ``0040`` did the same for the jobs it
backfilled; this covers jobs sliced or completed since.

Reversing is a no-op.
"""

import logging

from django.db import migrations
from django.db.models import Exists, Max, OuterRef, Sum

logger = logging.getLogger(__name__)


def keep_counts_of_completed_jobs(apps, schema_editor) -> None:
    """Set ``plate_parts_known=False`` on completed jobs whose plate parts count fewer copies."""
    PrintJob = apps.get_model("core", "PrintJob")
    PrintJobPlate = apps.get_model("core", "PrintJobPlate")
    PrintJobPart = apps.get_model("core", "PrintJobPart")
    PrintJobPlatePart = apps.get_model("core", "PrintJobPlatePart")
    PrintQueue = apps.get_model("core", "PrintQueue")

    passed = PrintQueue.objects.filter(plate=OuterRef("pk"), result="passed")
    unfinished_jobs = PrintJobPlate.objects.filter(~Exists(passed)).values("print_job_id")
    candidates = (
        PrintJob.objects.filter(plate_parts_known=True, plates__isnull=False)
        .filter(Exists(PrintJobPlatePart.objects.filter(plate__print_job=OuterRef("pk"))))
        .exclude(pk__in=unfinished_jobs)
        .distinct()
    )
    flipped = []
    for job in candidates:
        complete: dict[int, int] = {}
        copies = (
            PrintJobPlatePart.objects.filter(plate__print_job=job)
            .order_by()
            .values("part_id", "copy_index")
            .annotate(on_plates=Sum("meshes_on_plate"), meshes=Max("mesh_count"))
        )
        for row in copies:
            if row["on_plates"] >= row["meshes"]:
                complete[row["part_id"]] = complete.get(row["part_id"], 0) + 1
        entries = PrintJobPart.objects.filter(print_job=job).values_list("part_id", "quantity")
        if any(complete.get(part_id, 0) < quantity for part_id, quantity in entries):
            flipped.append(job.pk)
    if flipped:
        PrintJob.objects.filter(pk__in=flipped).update(plate_parts_known=False)
    logger.info(f"Keep printed counts: {len(flipped)} completed job(s) keep the all-plates rule {flipped}")


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0041_printed_part_records"),
    ]

    operations = [
        migrations.RunPython(keep_counts_of_completed_jobs, migrations.RunPython.noop),
    ]
