"""Backfill the plate parts of already sliced jobs from their G-code files.

New slices record which part copies lie on which plate while slicing
(``PrintJobPlatePart``).  For jobs sliced before, this migration re-derives the
same mapping from the G-code files in ``media/gcode_jobs``:

* The bundle object IDs are re-numbered with
  :func:`core.services.threemf.bundle_objects`, assuming the bundle used the
  ``PrintJobPart`` pk order (the old worker had no explicit ordering) and the
  mesh count of each part's *current* model file.
* Each plate's G-code object labels are parsed with
  :func:`core.services.gcode_objects.parse_gcode_object_ids` and assigned with
  :func:`core.services.gcode_objects.assign_plate_objects`.

A job gets ``plate_parts_known=False`` and no plate parts when the mapping is
not reliable: a G-code or model file is missing or unreadable, a model file is
newer than the job's G-code (replaced after slicing), or the labels do not fit
the bundle (unknown object ID, object on several plates, several unlabelled
plates).  Fewer objects on the plates than copies are *not* an error — those
copies were not placed by the slicer and show up as a warning.

Printed quantities must not drop: today a job counts all its parts once every
plate is completed.  If such a completed job has copies that are not
(completely) on any plate, its plate parts are kept for the warning, but
``plate_parts_known`` is set to ``False`` so it keeps counting with that rule.

Idempotent (jobs that already have plate parts are skipped); reversing is a
no-op (the previous migration drops the table and field).
"""

import logging
from pathlib import Path

from django.db import migrations

logger = logging.getLogger(__name__)


def _file_path(field_file) -> Path | None:
    """Return the existing local path of a historical ``FieldFile``, or ``None``."""
    if not field_file or not field_file.name:
        return None
    try:
        path = Path(field_file.path)
    except Exception:  # noqa: BLE001 — e.g. a suspicious stored name; treat as missing
        return None
    return path if path.is_file() else None


def _job_plate_shares(plates: list, job_parts: list) -> tuple[dict | None, str]:
    """Derive the plate shares of one legacy job.

    Args:
        plates: The job's plates (historical model instances).
        job_parts: The job's ``PrintJobPart`` rows in pk order, with ``part`` loaded.

    Returns:
        Tuple of (plate number → list of ``PlateCopyShare``, or ``None`` if
        unknown; the reason when unknown).
    """
    from core.services.gcode_objects import assign_plate_objects, parse_gcode_object_ids
    from core.services.threemf import bundle_objects, read_model_meshes

    gcode_paths: dict[int, Path] = {}
    for plate in plates:
        path = _file_path(plate.gcode_file)
        if path is None:
            return None, "gcode file missing"
        gcode_paths[plate.plate_number] = path

    model_paths: list[Path] = []
    for job_part in job_parts:
        path = _file_path(job_part.part.stl_file)
        if path is None:
            return None, "model file missing"
        model_paths.append(path)

    try:
        replaced = bool(model_paths) and max(p.stat().st_mtime for p in model_paths) > min(
            p.stat().st_mtime for p in gcode_paths.values()
        )
    except OSError:
        return None, "file not accessible"
    if replaced:
        return None, "model file replaced after slicing"

    parts: list[tuple[int, int]] = []
    for job_part, path in zip(job_parts, model_paths, strict=True):
        try:
            mesh_count = len(read_model_meshes(path))
        except Exception:  # noqa: BLE001 — any broken upload must not block ``migrate`` on startup
            logger.exception("Model file %s of part %s is unreadable", path, job_part.part_id)
            return None, "model file unreadable"
        parts.append((job_part.quantity, mesh_count))

    try:
        plate_ids = {number: parse_gcode_object_ids(path.read_bytes()) for number, path in gcode_paths.items()}
    except OSError:
        return None, "gcode file unreadable"
    shares = assign_plate_objects(bundle_objects(parts), plate_ids)
    if shares is None:
        return None, "labels do not fit the bundle"
    return shares, ""


def backfill_plate_parts(apps, schema_editor) -> None:
    """Create ``PrintJobPlatePart`` rows for every sliced job without them."""
    PrintJob = apps.get_model("core", "PrintJob")
    PrintJobPart = apps.get_model("core", "PrintJobPart")
    PrintJobPlate = apps.get_model("core", "PrintJobPlate")
    PrintJobPlatePart = apps.get_model("core", "PrintJobPlatePart")

    mapped = legacy = 0
    unknown: dict[str, int] = {}
    for job in PrintJob.objects.filter(plates__isnull=False).distinct().order_by("pk"):
        if PrintJobPlatePart.objects.filter(plate__print_job=job).exists():
            continue
        plates = list(PrintJobPlate.objects.filter(print_job=job).order_by("plate_number"))
        job_parts = list(PrintJobPart.objects.filter(print_job=job).select_related("part").order_by("pk"))
        shares, reason = _job_plate_shares(plates, job_parts)
        if shares is None:
            unknown[reason] = unknown.get(reason, 0) + 1
            logger.warning("Plate parts of job %s unknown: %s", job.pk, reason)
            PrintJob.objects.filter(pk=job.pk).update(plate_parts_known=False)
            continue

        plates_by_number = {plate.plate_number: plate for plate in plates}
        rows = [
            PrintJobPlatePart(
                plate=plates_by_number[number],
                part_id=job_parts[share.part_index].part_id,
                copy_index=share.copy_index,
                meshes_on_plate=share.meshes_on_plate,
                mesh_count=share.mesh_count,
            )
            for number, plate_shares in shares.items()
            for share in plate_shares
        ]
        PrintJobPlatePart.objects.bulk_create(rows)

        # Complete copies: all meshes of the copy on some plate.
        on_plates: dict[tuple[int, int], int] = {}
        mesh_counts: dict[tuple[int, int], int] = {}
        for plate_shares in shares.values():
            for share in plate_shares:
                key = (share.part_index, share.copy_index)
                on_plates[key] = on_plates.get(key, 0) + share.meshes_on_plate
                mesh_counts[key] = share.mesh_count
        complete = sum(1 for key, count in on_plates.items() if count >= mesh_counts[key])
        total = sum(job_part.quantity for job_part in job_parts)
        # The legacy rule counts every part of a job whose plates all have a passed attempt.
        all_passed = not PrintJobPlate.objects.filter(print_job=job).exclude(queue_entries__result="passed").exists()
        known = not (all_passed and complete < total)
        if not known:
            legacy += 1
            logger.warning(
                "Job %s is completed but %d of %d copies lie on no plate; it keeps the legacy count",
                job.pk,
                total - complete,
                total,
            )
        PrintJob.objects.filter(pk=job.pk).update(plate_parts_known=known)
        mapped += 1

    if mapped or unknown:
        logger.warning(
            "Plate parts backfill: %d job(s) mapped (%d completed with unplaced copies keep the legacy count), "
            "unknown: %s",
            mapped,
            legacy,
            unknown or "none",
        )


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0039_plate_parts"),
    ]

    operations = [
        migrations.RunPython(backfill_plate_parts, migrations.RunPython.noop),
    ]
