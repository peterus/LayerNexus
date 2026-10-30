"""View helper functions for the LayerNexus application."""

import logging
from collections.abc import Iterable

from django.contrib.auth.models import User
from django.db import transaction
from django.db.models import QuerySet

from core.models import Part, Project
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
    return Project.objects.all()


def _trigger_part_estimation(part: Part) -> bool:
    """Queue a part for background estimation and make sure the worker is running.

    Delegates the reset-and-queue step to :meth:`Part.request_estimate` (eligibility:
    STL present and a preset that resolves bottom-up). The unified OrcaSlicer worker
    processes all estimations and slicing sequentially.

    Args:
        part: The Part instance to estimate.

    Returns:
        ``True`` if the part was queued, ``False`` if it is not estimable.
    """
    if not part.request_estimate():
        return False

    _start_orcaslicer_worker()
    logger.info("Queued estimation for part %s (%s)", part.pk, part.name)
    return True


def _requeue_stale_estimates(parts: Iterable[Part]) -> int:
    """Re-queue estimation for copied or moved parts whose effective preset has changed.

    A copied or moved part keeps its estimate. If its effective print preset (resolved
    bottom-up in the new location) differs from the preset that produced that estimate,
    the estimate is cleared and the part is queued again (or left at ``none`` if no
    preset resolves any more). Parts with an unchanged preset keep their estimate. All
    resets run in one transaction and the worker is started once afterwards.

    Args:
        parts: The copied or moved parts.

    Returns:
        Number of parts that were queued for estimation.
    """
    count = 0
    project_presets: dict[int, int | None] = {}
    # One shared instance per project, so each ancestor chain is loaded (and cached) once.
    projects: dict[int, Project] = {}
    with transaction.atomic():
        for part in parts:
            if not part.stl_file:
                continue
            part.project = projects.setdefault(part.project_id, part.project)
            if part.print_preset_id is not None:
                effective_id = part.print_preset_id
            else:
                if part.project_id not in project_presets:
                    project_presets[part.project_id] = part.project.effective_default_print_preset_id
                effective_id = project_presets[part.project_id]
            if effective_id == part.estimated_with_preset_id:
                continue
            part.clear_estimate(queued=effective_id is not None)
            if effective_id is not None:
                count += 1
    if count:
        # Started after the commit so the worker sees the pending parts.
        _start_orcaslicer_worker()
        logger.info("Re-queued estimation for %d part(s) with a changed preset", count)
    return count
