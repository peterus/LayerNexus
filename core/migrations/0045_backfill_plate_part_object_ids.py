"""Backfill ``PrintJobPlatePart.object_ids`` of already sliced plates from their G-code files.

New slices store the bundle object IDs of every plate part while slicing; they
map the objects Klipper excludes during a print (``OBJECT_<N>_…``) back to the
part copies.  For plate parts recorded before, this migration re-derives them:

* The bundle object IDs are re-numbered with
  :func:`core.services.threemf.bundle_objects` in ``PrintJobPart`` pk order (the
  bundle order of the slicing worker and of ``0040``), with each part's mesh
  count taken from its recorded plate parts (or, for a part without any, from
  its model file if that is not newer than the G-code).
* Each plate's G-code labels are parsed with
  :func:`core.services.gcode_objects.parse_gcode_object_ids` and every object
  ID is mapped to its (part, copy).

A plate is only updated when the result matches its recorded plate parts
exactly (same copies, one object ID per mesh on the plate); otherwise — G-code
missing or unreadable, labels that do not fit — its plate parts keep an empty
``object_ids`` (Klipper exclusions on that plate cannot be mapped, manual marks
still work).

Idempotent (plate parts with object IDs are skipped); reversing is a no-op (the
previous migration drops the field).
"""

import logging
import mmap
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


def _gcode_object_ids(path: Path) -> list[int]:
    """Parse a plate's G-code via ``mmap`` so a large file is not read into memory at once."""
    from core.services.gcode_objects import parse_gcode_object_ids

    with path.open("rb") as handle:
        if path.stat().st_size == 0:
            return []
        with mmap.mmap(handle.fileno(), 0, access=mmap.ACCESS_READ) as mapped:
            return parse_gcode_object_ids(mapped)


def _file_mesh_count(job_part, gcode_paths: list[Path]) -> int | None:
    """Return the mesh count of a part without recorded plate parts from its model file (``None`` = unknown)."""
    from core.services.threemf import read_model_meshes

    path = _file_path(job_part.part.stl_file)
    if path is None or not gcode_paths:
        return None
    try:
        if path.stat().st_mtime > min(p.stat().st_mtime for p in gcode_paths):
            return None  # replaced after slicing: the bundle may have had another mesh count
        if path.suffix.lower() != ".3mf":
            return 1
        return len(read_model_meshes(path))
    except Exception:  # noqa: BLE001 — any broken file must not block ``migrate`` on startup
        logger.exception("Model file %s is unreadable", path)
        return None


def _backfill_job(job, PrintJobPart, PrintJobPlate, PrintJobPlatePart) -> tuple[int, int]:
    """Fill the object IDs of one job's plate parts.

    Returns:
        Tuple of (plates updated, plates skipped).
    """
    from core.services.threemf import bundle_objects

    plates = list(PrintJobPlate.objects.filter(print_job=job).order_by("plate_number"))
    rows = list(PrintJobPlatePart.objects.filter(plate__print_job=job))
    job_parts = list(PrintJobPart.objects.filter(print_job=job).select_related("part").order_by("pk"))
    rows_by_plate: dict[int, list] = {}
    mesh_counts: dict[int, int] = {}
    for row in rows:
        rows_by_plate.setdefault(row.plate_id, []).append(row)
        mesh_counts[row.part_id] = row.mesh_count
    gcode_paths = {plate.pk: _file_path(plate.gcode_file) for plate in plates}

    parts: list[tuple[int, int]] = []
    for job_part in job_parts:
        mesh_count = mesh_counts.get(job_part.part_id)
        if mesh_count is None:
            mesh_count = _file_mesh_count(job_part, [p for p in gcode_paths.values() if p is not None])
        if mesh_count is None:
            logger.warning("Object IDs of job %s unknown: mesh count of part %s unknown", job.pk, job_part.part_id)
            return 0, len(rows_by_plate)
        parts.append((job_part.quantity, mesh_count))
    by_id = {obj.object_id: obj for obj in bundle_objects(parts)}

    updated = skipped = 0
    for plate in plates:
        plate_rows = rows_by_plate.get(plate.pk)
        if not plate_rows:
            continue
        path = gcode_paths[plate.pk]
        try:
            object_ids = _gcode_object_ids(path) if path is not None else None
        except (OSError, ValueError):
            object_ids = None
        if object_ids is None:
            logger.warning("Object IDs of plate %s unknown: gcode file missing or unreadable", plate.pk)
            skipped += 1
            continue
        ids_by_copy: dict[tuple[int, int], list[int]] = {}
        fits = True
        for object_id in object_ids:
            obj = by_id.get(object_id)
            if obj is None:
                fits = False
                break
            key = (job_parts[obj.part_index].part_id, obj.copy_index)
            ids_by_copy.setdefault(key, []).append(object_id)
        rows_by_copy = {(row.part_id, row.copy_index): row for row in plate_rows}
        fits = (
            fits
            and set(ids_by_copy) == set(rows_by_copy)
            and all(len(ids_by_copy[key]) == row.meshes_on_plate for key, row in rows_by_copy.items())
        )
        if not fits:
            logger.warning("Object IDs of plate %s unknown: labels do not fit its plate parts", plate.pk)
            skipped += 1
            continue
        for key, row in rows_by_copy.items():
            row.object_ids = sorted(ids_by_copy[key])
        PrintJobPlatePart.objects.bulk_update(list(rows_by_copy.values()), ["object_ids"])
        updated += 1
    return updated, skipped


def backfill_object_ids(apps, schema_editor) -> None:
    """Fill ``object_ids`` of every plate part recorded without them."""
    PrintJob = apps.get_model("core", "PrintJob")
    PrintJobPart = apps.get_model("core", "PrintJobPart")
    PrintJobPlate = apps.get_model("core", "PrintJobPlate")
    PrintJobPlatePart = apps.get_model("core", "PrintJobPlatePart")

    job_ids = (
        PrintJobPlatePart.objects.filter(object_ids=[])
        .values_list("plate__print_job_id", flat=True)
        .distinct()
        .order_by("plate__print_job_id")
    )
    updated = skipped = 0
    for job in PrintJob.objects.filter(pk__in=list(job_ids)).order_by("pk"):
        job_updated, job_skipped = _backfill_job(job, PrintJobPart, PrintJobPlate, PrintJobPlatePart)
        updated += job_updated
        skipped += job_skipped
    if updated or skipped:
        logger.warning("Plate part object IDs backfill: %d plate(s) mapped, %d unknown", updated, skipped)


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0044_excluded_objects"),
    ]

    operations = [
        migrations.RunPython(backfill_object_ids, migrations.RunPython.noop),
    ]
