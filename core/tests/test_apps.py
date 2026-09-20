"""Tests for :mod:`core.apps` startup stuck-item recovery.

The recovery must reset stuck estimating parts and slicing jobs on server
startup, restart the worker when work is pending, and — crucially — never
touch the database from inside ``AppConfig.ready()`` (which would raise
``RuntimeWarning: Accessing the database during app initialization``).
"""

import threading
from unittest.mock import patch

from django.apps import apps
from django.test import TestCase

from core.models import Part, PrintJob
from core.tests.mixins import TestDataMixin


class RecoverStuckItemsTests(TestDataMixin, TestCase):
    """Behaviour of :meth:`CoreConfig._recover_stuck_items`."""

    def setUp(self) -> None:
        super().setUp()
        self.config = apps.get_app_config("core")

    def test_resets_stuck_estimating_parts_to_pending(self) -> None:
        """Parts stuck at 'estimating' are reset to 'pending'."""
        self.part.estimation_status = Part.ESTIMATION_ESTIMATING
        self.part.save(update_fields=["estimation_status"])

        with patch("core.services.slicing_worker._start_orcaslicer_worker"):
            self.config._recover_stuck_items()

        self.part.refresh_from_db()
        self.assertEqual(self.part.estimation_status, Part.ESTIMATION_PENDING)

    def test_resets_stuck_slicing_jobs_to_pending(self) -> None:
        """Jobs stuck at 'slicing' are reset to 'pending' and clear the timestamp."""
        from django.utils import timezone

        job = PrintJob.objects.create(
            status=PrintJob.STATUS_SLICING,
            slicing_started_at=timezone.now(),
            created_by=self.user,
        )

        with patch("core.services.slicing_worker._start_orcaslicer_worker"):
            self.config._recover_stuck_items()

        job.refresh_from_db()
        self.assertEqual(job.status, PrintJob.STATUS_PENDING)
        self.assertIsNone(job.slicing_started_at)

    def test_starts_worker_when_pending_work_exists(self) -> None:
        """The OrcaSlicer worker is (re)started when there is pending work."""
        self.part.estimation_status = Part.ESTIMATION_PENDING
        self.part.save(update_fields=["estimation_status"])

        with patch("core.services.slicing_worker._start_orcaslicer_worker") as mock_start:
            self.config._recover_stuck_items()

        mock_start.assert_called_once()

    def test_does_not_start_worker_without_pending_work(self) -> None:
        """The worker is not started when nothing is pending."""
        Part.objects.update(estimation_status=Part.ESTIMATION_SUCCESS)
        PrintJob.objects.update(status=PrintJob.STATUS_SLICED)

        with patch("core.services.slicing_worker._start_orcaslicer_worker") as mock_start:
            self.config._recover_stuck_items()

        mock_start.assert_not_called()


class ReadyDefersRecoveryTests(TestCase):
    """``ready()`` must defer recovery and never query the database itself."""

    def setUp(self) -> None:
        super().setUp()
        self.config = apps.get_app_config("core")

    def test_ready_schedules_recovery_in_server_process(self) -> None:
        """In a gunicorn/main process, ready() schedules recovery without querying."""
        with (
            patch.object(self.config, "_schedule_stuck_item_recovery") as mock_schedule,
            patch.dict("os.environ", {"SERVER_SOFTWARE": "gunicorn/21.2.0"}),
            self.assertNumQueries(0),
        ):
            self.config.ready()

        mock_schedule.assert_called_once()

    def test_ready_does_nothing_outside_server_process(self) -> None:
        """Management commands / migrations (no server env) skip recovery entirely."""
        with (
            patch.object(self.config, "_schedule_stuck_item_recovery") as mock_schedule,
            patch.dict("os.environ", {}, clear=False),
        ):
            import os

            os.environ.pop("RUN_MAIN", None)
            os.environ.pop("SERVER_SOFTWARE", None)
            self.config.ready()

        mock_schedule.assert_not_called()

    def test_schedule_runs_recovery_in_background_thread(self) -> None:
        """The scheduled daemon thread actually invokes the recovery."""
        ran = threading.Event()

        with patch.object(self.config, "_run_stuck_item_recovery", side_effect=ran.set) as mock_run:
            self.config._schedule_stuck_item_recovery()
            self.assertTrue(ran.wait(timeout=5), "recovery thread did not run")

        mock_run.assert_called_once()

    def test_run_recovery_waits_for_registry_then_recovers(self) -> None:
        """_run_stuck_item_recovery runs recovery once the registry is ready."""
        self.assertTrue(apps.ready_event.is_set())

        with patch.object(self.config, "_recover_stuck_items") as mock_recover:
            self.config._run_stuck_item_recovery()

        mock_recover.assert_called_once()
