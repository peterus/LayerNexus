import contextlib
import logging
import os
import threading

from django.apps import AppConfig, apps

logger = logging.getLogger(__name__)


class CoreConfig(AppConfig):
    """Core application configuration for LayerNexus."""

    name = "core"

    def ready(self) -> None:
        """Schedule stuck-worker recovery shortly after startup.

        When the container restarts, parts stuck at 'estimating' and jobs stuck
        at 'slicing' were being processed by the now-dead worker thread.  They
        must be reset to 'pending' so the worker can pick them up again.

        The recovery touches the database, which must never happen inside
        ``AppConfig.ready()`` — Django raises ``RuntimeWarning: Accessing the
        database during app initialization is discouraged`` because the app
        registry is not yet fully populated.  Instead of querying here, the
        work is deferred to a short-lived background thread that waits for the
        registry to finish loading before running (see
        :meth:`_run_stuck_item_recovery`).

        Only the main server process performs recovery — never management
        commands or migrations.
        """
        # Only run in the main server process (Django autoreload main or gunicorn),
        # not in management commands or migrations.
        if os.environ.get("RUN_MAIN") == "true" or os.environ.get("SERVER_SOFTWARE", "").startswith("gunicorn"):
            self._schedule_stuck_item_recovery()

    def _schedule_stuck_item_recovery(self) -> None:
        """Start a daemon thread that runs recovery once the app registry is ready.

        Deferring to a thread keeps ``ready()`` free of database access so the
        app registry can finish populating first.
        """
        thread = threading.Thread(
            target=self._run_stuck_item_recovery,
            name="layernexus-stuck-item-recovery",
            daemon=True,
        )
        thread.start()

    def _run_stuck_item_recovery(self) -> None:
        """Wait for app init to finish, then recover stuck items in this thread.

        Blocks until the app registry reports ready so no query runs during app
        initialization, suppresses database errors that can occur before
        migrations have run, and always closes the thread-local database
        connection afterwards to avoid leaking it.
        """
        from django.db import OperationalError, ProgrammingError, connection

        # Wait for the app registry to finish populating before touching the DB.
        apps.ready_event.wait()

        try:
            with contextlib.suppress(OperationalError, ProgrammingError):
                self._recover_stuck_items()
        finally:
            connection.close()

    def _recover_stuck_items(self) -> None:
        """Reset stuck estimations and slicing jobs, then restart the worker."""
        from core.models import Part, PrintJob

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
            from core.services.slicing_worker import _start_orcaslicer_worker

            _start_orcaslicer_worker()
            logger.info("OrcaSlicer worker started on application startup")
