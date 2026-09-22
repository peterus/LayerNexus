"""View helper functions for the LayerNexus application."""

import logging

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

    Variant B: eligibility is delegated to :meth:`Part.is_estimable` (STL present and
    a preset that resolves — or is ambiguous — against the containing projects), so a
    legacy part with no own override but a project default is queued instead of being
    dropped here. Ambiguous parts are queued too so the worker records the ambiguous
    status rather than the caller silently swallowing it.

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
