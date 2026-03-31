"""OrcaSlicer background worker for estimation and slicing jobs."""

from __future__ import annotations

import fcntl
import logging
import threading
from datetime import timedelta
from pathlib import Path
from typing import IO

from django.conf import settings

from core.models import (
    Part,
    PrintJob,
    SpoolmanFilamentMapping,
)
from core.services.orcaslicer import OrcaSlicerAPIClient, OrcaSlicerError
from core.services.slicing import _build_slicer_kwargs, _find_compatible_machine
from core.services.threemf import ThreeMFError, create_3mf_bundle

logger = logging.getLogger(__name__)

__all__: list[str] = [
    "_orcaslicer_worker_active",
    "_orcaslicer_worker_lock",
    "_start_orcaslicer_worker",
    "_orcaslicer_worker_loop",
    "_estimate_part_in_background",
    "_slice_job_in_background",
]

# ---------------------------------------------------------------------------
# Module-level state for the OrcaSlicer worker
# ---------------------------------------------------------------------------
_orcaslicer_worker_lock = threading.Lock()
_orcaslicer_worker_active = False

# File lock to ensure only one worker across all gunicorn processes
_LOCK_FILE = Path(settings.BASE_DIR) / "data" / ".orcaslicer_worker.lock"


def _acquire_file_lock() -> IO[str] | None:
    """Try to acquire an exclusive file lock (non-blocking).

    Returns the open file handle on success, or None if another
    process already holds the lock.
    """
    import errno

    _LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    fh = None
    try:
        fh = open(_LOCK_FILE, "w")  # noqa: SIM115
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fh
    except OSError as exc:
        if fh is not None:
            fh.close()
        if exc.errno in (errno.EACCES, errno.EAGAIN):
            return None
        logger.error("Unexpected error acquiring OrcaSlicer worker lock: %s", exc)
        return None


def _start_orcaslicer_worker() -> None:
    """Start the OrcaSlicer worker thread if not already running.

    The worker processes pending estimation parts and pending slicing
    jobs sequentially, one at a time.  Only one worker thread is active
    at any given time to avoid overloading the OrcaSlicer API.

    A file lock ensures that only one gunicorn process can run the
    worker, even with multiple workers.
    """
    global _orcaslicer_worker_active
    with _orcaslicer_worker_lock:
        if _orcaslicer_worker_active:
            return
        _orcaslicer_worker_active = True

    thread = threading.Thread(
        target=_orcaslicer_worker_loop,
        daemon=True,
    )
    thread.start()
    logger.info("Started OrcaSlicer worker thread")


def _orcaslicer_worker_loop() -> None:
    """Process pending estimation and slicing jobs sequentially.

    Each iteration picks either the next Part with
    ``estimation_status='pending'`` or the next PrintJob with
    ``status='pending'``, processes it via the OrcaSlicer API, and
    repeats until no more pending work remains.
    Slicing jobs are prioritised over estimations because users are
    actively waiting for sliced G-code.

    A file lock ensures only one worker runs across all gunicorn
    processes.  If the lock cannot be acquired, the thread exits
    immediately — the process that holds the lock is already
    processing the queue.
    """
    global _orcaslicer_worker_active
    from django.db import connection
    from django.utils import timezone

    # Acquire cross-process file lock
    lock_fh = _acquire_file_lock()
    if lock_fh is None:
        logger.debug("OrcaSlicer worker: another process holds the lock, exiting")
        with _orcaslicer_worker_lock:
            _orcaslicer_worker_active = False
        return

    try:
        while True:
            # 1. Check for pending slicing jobs (higher priority)
            #    Atomically claim the job: PENDING → SLICING
            next_job_pk = (
                PrintJob.objects.filter(
                    status=PrintJob.STATUS_PENDING,
                )
                .order_by("pk")
                .values_list("pk", flat=True)
                .first()
            )

            if next_job_pk is not None:
                claimed = PrintJob.objects.filter(
                    pk=next_job_pk,
                    status=PrintJob.STATUS_PENDING,
                ).update(
                    status=PrintJob.STATUS_SLICING,
                    slicing_started_at=timezone.now(),
                    slicing_error="",
                )
                if claimed:
                    _slice_job_in_background(next_job_pk)
                continue

            # 2. Check for pending estimations (lower priority)
            #    Atomically claim the part: PENDING → ESTIMATING
            next_estimation_pk = (
                Part.objects.filter(
                    estimation_status=Part.ESTIMATION_PENDING,
                )
                .order_by("pk")
                .values_list("pk", flat=True)
                .first()
            )

            if next_estimation_pk is not None:
                claimed = Part.objects.filter(
                    pk=next_estimation_pk,
                    estimation_status=Part.ESTIMATION_PENDING,
                ).update(estimation_status=Part.ESTIMATION_ESTIMATING)
                if claimed:
                    _estimate_part_in_background(next_estimation_pk)
                continue

            # Nothing left to process
            logger.info("OrcaSlicer worker: no more pending work, stopping")
            break
    finally:
        # Re-check for work while still holding the file lock to prevent
        # another process from acquiring it and causing churn.
        with _orcaslicer_worker_lock:
            has_pending = (
                Part.objects.filter(
                    estimation_status=Part.ESTIMATION_PENDING,
                ).exists()
                or PrintJob.objects.filter(
                    status=PrintJob.STATUS_PENDING,
                ).exists()
            )
            if has_pending:
                # Keep the worker active; loop again on a new thread
                pass
            else:
                _orcaslicer_worker_active = False

        # Release the file lock after the re-check
        fcntl.flock(lock_fh, fcntl.LOCK_UN)
        lock_fh.close()

        connection.close()

        if has_pending:
            # Start a fresh thread (flag is still True, so
            # _start_orcaslicer_worker would bail out — call the
            # loop directly on a new daemon thread).
            thread = threading.Thread(
                target=_orcaslicer_worker_loop,
                daemon=True,
            )
            thread.start()
            logger.info("OrcaSlicer worker: restarted for newly queued work")


def _estimate_part_in_background(part_pk: int) -> None:
    """Slice a single copy of a part to obtain per-copy filament/time estimates.

    Called by the estimation worker thread.  Picks a compatible machine
    profile automatically based on the part's print preset.  Results are
    stored directly on the Part instance (``filament_used_grams``,
    ``filament_used_meters``, ``estimated_print_time``).

    The caller is responsible for managing the DB connection lifecycle.

    Args:
        part_pk: Primary key of the Part to estimate.
    """
    from pathlib import Path as FSPath

    try:
        part = Part.objects.select_related(
            "project__default_print_preset",
            "project__parent__default_print_preset",
            "print_preset",
        ).get(pk=part_pk)

        if not part.stl_file:
            logger.debug("estimate_part(%s): no STL file, skipping", part_pk)
            Part.objects.filter(pk=part_pk).update(
                estimation_status=Part.ESTIMATION_NONE,
            )
            return

        # Resolve profiles (traverses parent project hierarchy)
        print_preset = part.effective_print_preset
        if not print_preset:
            logger.debug("estimate_part(%s): no print preset, skipping", part_pk)
            Part.objects.filter(pk=part_pk).update(
                estimation_status=Part.ESTIMATION_NONE,
            )
            return

        # Find a compatible machine profile for this preset
        machine_profile = _find_compatible_machine(print_preset)
        if not machine_profile:
            logger.warning("estimate_part(%s): no compatible machine profile found", part_pk)
            return

        # Resolve filament profile from Spoolman mapping
        filament_profile = None
        if part.spoolman_filament_id:
            mapping = (
                SpoolmanFilamentMapping.objects.filter(
                    spoolman_filament_id=part.spoolman_filament_id,
                )
                .select_related("orca_filament_profile")
                .first()
            )
            if mapping and mapping.orca_filament_profile:
                filament_profile = mapping.orca_filament_profile

        # Build 3MF with single copy
        stl_path = FSPath(part.stl_file.path)
        threemf_content = create_3mf_bundle([(stl_path, 1)])

        slice_kwargs = _build_slicer_kwargs(
            machine_profile=machine_profile,
            print_preset=print_preset,
            filament_profile=filament_profile,
        )

        # Status already set to ESTIMATING by the worker loop (atomic claim)
        slicer = OrcaSlicerAPIClient(settings.ORCASLICER_API_URL)
        result = slicer.slice_bundle(threemf_content, **slice_kwargs)

        # Re-fetch part to avoid stale state
        part = Part.objects.get(pk=part_pk)

        updated: list[str] = []
        total_g = result.total_filament_grams
        total_mm = result.total_filament_mm
        total_time = result.total_print_time_seconds

        if total_g is not None:
            part.filament_used_grams = round(total_g, 2)
            updated.append("filament_used_grams")
        if total_mm is not None:
            part.filament_used_meters = round(total_mm / 1000.0, 4)
            updated.append("filament_used_meters")
        if total_time is not None:
            part.estimated_print_time = timedelta(seconds=total_time)
            updated.append("estimated_print_time")

        if updated:
            part.estimation_status = Part.ESTIMATION_SUCCESS
            part.estimation_error = ""
            updated.extend(["estimation_status", "estimation_error"])
            part.save(update_fields=updated)
            logger.info(
                "estimate_part(%s): saved estimates — %sg, %sm, %ss",
                part_pk,
                part.filament_used_grams,
                part.filament_used_meters,
                total_time,
            )
        else:
            Part.objects.filter(pk=part_pk).update(
                estimation_status=Part.ESTIMATION_ERROR,
                estimation_error="Slicing returned no filament or time estimates.",
            )
            logger.warning("estimate_part(%s): slicing returned no estimates", part_pk)

    except (OrcaSlicerError, FileNotFoundError) as exc:
        Part.objects.filter(pk=part_pk).update(
            estimation_status=Part.ESTIMATION_ERROR,
            estimation_error=str(exc),
        )
        logger.warning("estimate_part(%s): slicing failed — %s", part_pk, exc)
    except Part.DoesNotExist:
        logger.debug("estimate_part(%s): part no longer exists", part_pk)
    except Exception as exc:
        Part.objects.filter(pk=part_pk).update(
            estimation_status=Part.ESTIMATION_ERROR,
            estimation_error=f"Unexpected error: {exc}",
        )
        logger.exception("estimate_part(%s): unexpected error", part_pk)


def _slice_job_in_background(job_pk: int) -> None:
    """Slice a PrintJob via OrcaSlicer API.

    Called by the unified OrcaSlicer worker.  Builds the 3MF bundle,
    resolves profiles, calls the slicer, and creates ``PrintJobPlate``
    objects for each plate.  On success the job transitions to *sliced*.

    The caller (worker loop) is responsible for DB connection lifecycle.

    Args:
        job_pk: Primary key of the PrintJob to slice.
    """
    from pathlib import Path

    from django.core.files.base import ContentFile

    try:
        # Status already set to SLICING by the worker loop (atomic claim)
        job = PrintJob.objects.prefetch_related("job_parts__part__project").get(pk=job_pk)

        job_parts = job.job_parts.select_related(
            "part__project__default_print_preset",
            "part__project__parent__default_print_preset",
            "part__print_preset",
        )

        # Build 3MF bundle from job parts
        stl_list = [(jp.part.stl_file.path, jp.quantity) for jp in job_parts]
        threemf_content = create_3mf_bundle(stl_list)

        # Resolve profiles
        machine_profile = job.machine_profile
        first_part = job_parts.first().part
        filament_profile = None
        if first_part.spoolman_filament_id:
            mapping = (
                SpoolmanFilamentMapping.objects.filter(
                    spoolman_filament_id=first_part.spoolman_filament_id,
                )
                .select_related("orca_filament_profile")
                .first()
            )
            if mapping and mapping.orca_filament_profile:
                filament_profile = mapping.orca_filament_profile

        print_preset = first_part.effective_print_preset

        slice_kwargs = _build_slicer_kwargs(
            machine_profile=machine_profile,
            print_preset=print_preset,
            filament_profile=filament_profile,
        )

        slicer = OrcaSlicerAPIClient(settings.ORCASLICER_API_URL)
        result = slicer.slice_bundle(threemf_content, **slice_kwargs)

        # Re-fetch job to avoid stale state
        job = PrintJob.objects.get(pk=job_pk)

        media_root = Path(settings.MEDIA_ROOT).resolve()
        gcode_dir = media_root / "gcode_jobs"
        gcode_dir.mkdir(parents=True, exist_ok=True)

        for plate_result in result.plates:
            gcode_filename = f"job_{job_pk}_plate_{plate_result.plate_number}.gcode"
            gcode_path = gcode_dir / gcode_filename
            if not gcode_path.resolve().is_relative_to(media_root):
                raise OrcaSlicerError("Output path is outside MEDIA_ROOT")
            gcode_path.write_bytes(plate_result.gcode_content)
            relative = gcode_path.resolve().relative_to(media_root)

            plate_filament = plate_result.filament_used_grams
            plate_time = plate_result.print_time_seconds

            from core.models import PrintJobPlate

            plate = PrintJobPlate(
                print_job=job,
                plate_number=plate_result.plate_number,
                status=PrintJobPlate.STATUS_WAITING,
            )
            plate.gcode_file.name = str(relative)
            if plate_filament is not None:
                plate.filament_used_grams = plate_filament
            if plate_time is not None:
                plate.print_time_estimate = timedelta(seconds=plate_time)

            # Save thumbnail if extracted from G-code or ZIP
            if plate_result.thumbnail_png:
                thumb_filename = f"job_{job_pk}_plate_{plate_result.plate_number}.png"
                plate.thumbnail.save(
                    thumb_filename,
                    ContentFile(plate_result.thumbnail_png),
                    save=False,
                )
                logger.debug(
                    "Saved thumbnail for plate %d (%d bytes)",
                    plate_result.plate_number,
                    len(plate_result.thumbnail_png),
                )

            plate.save()

        # Update aggregate stats on the job
        job.filament_used_grams = result.total_filament_grams
        job.print_time_estimate = (
            timedelta(seconds=result.total_print_time_seconds) if result.total_print_time_seconds else None
        )
        job.status = PrintJob.STATUS_SLICED
        job.slicing_error = ""
        job.save()
        logger.info(
            "Background slicing completed for job %s (%d plates)",
            job_pk,
            len(result.plates),
        )

        # Back-fill Part estimates with per-copy values.
        job_parts_list = list(job.job_parts.select_related("part"))
        total_copies = sum(jp.quantity for jp in job_parts_list)
        if total_copies > 0:
            total_g = result.total_filament_grams
            total_mm = result.total_filament_mm
            total_time = result.total_print_time_seconds

            per_copy_g = total_g / total_copies if total_g else None
            per_copy_mm = total_mm / total_copies if total_mm else None
            per_copy_time = total_time / total_copies if total_time else None

            for jp in job_parts_list:
                part = jp.part
                updated: list[str] = []
                if per_copy_g is not None and not part.filament_used_grams:
                    part.filament_used_grams = round(per_copy_g, 2)
                    updated.append("filament_used_grams")
                if per_copy_mm is not None and not part.filament_used_meters:
                    part.filament_used_meters = round(per_copy_mm / 1000.0, 4)
                    updated.append("filament_used_meters")
                if per_copy_time is not None and not part.estimated_print_time:
                    part.estimated_print_time = timedelta(seconds=per_copy_time)
                    updated.append("estimated_print_time")
                if updated:
                    part.save(update_fields=updated)

    except (OrcaSlicerError, ThreeMFError, FileNotFoundError) as exc:
        logger.exception("Background slicing failed for job %s", job_pk)
        try:
            job = PrintJob.objects.get(pk=job_pk)
            job.status = PrintJob.STATUS_FAILED
            job.slicing_error = str(exc)
            job.save()
        except PrintJob.DoesNotExist:
            pass
    except PrintJob.DoesNotExist:
        logger.debug("slice_job(%s): job no longer exists", job_pk)
    except Exception as exc:
        logger.exception("Unexpected error during background slicing for job %s", job_pk)
        try:
            job = PrintJob.objects.get(pk=job_pk)
            job.status = PrintJob.STATUS_FAILED
            job.slicing_error = f"Unexpected error: {exc}"
            job.save()
        except PrintJob.DoesNotExist:
            pass
