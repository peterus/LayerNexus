"""View helper functions for the LayerNexus application."""

import logging
from collections.abc import Iterable

from django.contrib.auth.models import User
from django.db.models import QuerySet

from core.models import Part
from core.services.slicing_worker import _start_orcaslicer_worker

logger = logging.getLogger(__name__)

__all__: list[str] = []


def _user_projects_qs(user: "User") -> QuerySet:
    """Return all projects (global access in single-tenant mode).

    Args:
        user: User instance (kept for API compatibility but not used for filtering).

    Returns:
        QuerySet of all Project instances.
    """
    from core.models import Project

    return Project.objects.all()


def _trigger_part_estimation(part: Part) -> None:
    """Queue a part for background estimation if it is estimable.

    Sets the part's estimation_status to 'pending' and ensures the
    unified OrcaSlicer worker thread is running.  The worker processes
    all OrcaSlicer work (estimations and slicing) sequentially.

    Eligibility is delegated to :meth:`Part.is_estimable` (STL present and a preset
    that resolves bottom-up: the part's own preset or the nearest project default).

    Args:
        part: The Part instance to estimate.
    """
    if not part.is_estimable():
        return

    Part.objects.filter(pk=part.pk).update(
        estimation_status=Part.ESTIMATION_PENDING,
        estimation_error="",
    )

    _start_orcaslicer_worker()
    logger.info("Queued estimation for part %s (%s)", part.pk, part.name)


def _requeue_stale_estimates(parts: Iterable[Part]) -> int:
    """Re-queue estimation for copied parts whose effective preset has changed.

    A copied part keeps the estimate of its original. If its effective print preset
    (resolved bottom-up in the new location) differs from the preset that produced
    that estimate, the estimate is cleared and the part is queued again. Parts with
    an unchanged preset keep their copied estimate.

    Args:
        parts: The freshly copied parts.

    Returns:
        Number of parts that were queued for estimation.
    """
    count = 0
    project_presets: dict[int, int | None] = {}
    for part in parts:
        if not part.stl_file:
            continue
        if part.print_preset_id is not None:
            effective_id = part.print_preset_id
        else:
            if part.project_id not in project_presets:
                project_presets[part.project_id] = part.project.effective_default_print_preset_id
            effective_id = project_presets[part.project_id]
        if effective_id == part.estimated_with_preset_id:
            continue
        Part.objects.filter(pk=part.pk).update(
            filament_used_grams=None,
            filament_used_meters=None,
            estimated_print_time=None,
            estimation_status=Part.ESTIMATION_NONE,
            estimation_error="",
            estimated_with_preset=None,
        )
        if effective_id is not None:
            _trigger_part_estimation(part)
            count += 1
    return count
