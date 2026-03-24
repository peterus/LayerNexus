from django.apps import AppConfig


class CoreConfig(AppConfig):
    """Core application configuration for LayerNexus."""

    name = "core"

    def ready(self) -> None:
        """Reset stuck worker items and restart the OrcaSlicer worker on startup.

        When the container restarts, parts stuck at 'estimating' and jobs stuck
        at 'slicing' were being processed by the now-dead worker thread.  Reset
        them to 'pending' so the worker can pick them up again.
        """
        import os

        # Only run in the main process, not in management commands or migrations
        if os.environ.get("RUN_MAIN") == "true" or os.environ.get("SERVER_SOFTWARE", "").startswith("gunicorn"):
            import contextlib

            from django.db import OperationalError, ProgrammingError

            with contextlib.suppress(OperationalError, ProgrammingError):
                self._recover_stuck_items()

    def _recover_stuck_items(self) -> None:
        """Reset stuck estimations and slicing jobs, then restart the worker."""
        import logging

        from core.models import Part, PrintJob

        logger = logging.getLogger(__name__)

        # Reset parts stuck at 'estimating' → 'pending'
        stuck_estimations = Part.objects.filter(
            estimation_status=Part.ESTIMATION_ESTIMATING,
        ).update(estimation_status=Part.ESTIMATION_PENDING)

        if stuck_estimations:
            logger.warning(
                "Reset %d stuck estimating part(s) to pending on startup",
                stuck_estimations,
            )

        # Reset jobs stuck at 'slicing' → 'pending'
        stuck_slicing = PrintJob.objects.filter(
            status=PrintJob.STATUS_SLICING,
        ).update(status=PrintJob.STATUS_PENDING, slicing_started_at=None)

        if stuck_slicing:
            logger.warning(
                "Reset %d stuck slicing job(s) to pending on startup",
                stuck_slicing,
            )

        # Restart the worker if there's pending work
        has_pending = (
            Part.objects.filter(estimation_status=Part.ESTIMATION_PENDING).exists()
            or PrintJob.objects.filter(status=PrintJob.STATUS_PENDING).exists()
        )

        if has_pending:
            from core.views import _start_orcaslicer_worker

            _start_orcaslicer_worker()
            logger.info("OrcaSlicer worker started on application startup")
