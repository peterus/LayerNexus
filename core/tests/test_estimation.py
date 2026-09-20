"""Tests for the estimation worker queue."""

from unittest.mock import MagicMock, patch

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse

from core.models import Part, PrintJob, PrintJobPart
from core.tests.mixins import TestDataMixin


class EstimationWorkerTests(TestDataMixin, TestCase):
    """Tests for the sequential estimation worker queue."""

    def setUp(self):
        super().setUp()
        from core.models import OrcaPrintPreset

        self.preset = OrcaPrintPreset.objects.create(
            name="Test Preset",
            orca_name="test_preset",
            state=OrcaPrintPreset.STATE_RESOLVED,
        )

    def test_trigger_sets_pending_not_estimating(self):
        """_trigger_part_estimation sets status to PENDING, not ESTIMATING."""
        from core.views.helpers import _trigger_part_estimation

        self.part.stl_file = SimpleUploadedFile("test.stl", b"solid test")
        self.part.print_preset = self.preset
        self.part.save()

        with patch("core.views.helpers._start_orcaslicer_worker"):
            _trigger_part_estimation(self.part)

        self.part.refresh_from_db()
        self.assertEqual(self.part.estimation_status, Part.ESTIMATION_PENDING)

    @patch("core.views.helpers._start_orcaslicer_worker")
    def test_trigger_calls_start_worker(self, mock_start: "patch"):
        """_trigger_part_estimation calls _start_orcaslicer_worker."""
        self.part.stl_file = SimpleUploadedFile("test.stl", b"solid test")
        self.part.print_preset = self.preset
        self.part.save()

        from core.views.helpers import _trigger_part_estimation  # noqa: F811

        _trigger_part_estimation(self.part)

        mock_start.assert_called_once()

    def test_worker_does_not_start_duplicate(self):
        """Only one worker thread should be active at a time."""
        import core.services.slicing_worker as worker_mod
        from core.services.slicing_worker import (
            _orcaslicer_worker_lock,
            _start_orcaslicer_worker,
        )

        # Simulate an active worker
        with _orcaslicer_worker_lock:
            original = worker_mod._orcaslicer_worker_active
            worker_mod._orcaslicer_worker_active = True

        try:
            with patch("threading.Thread") as mock_thread:
                _start_orcaslicer_worker()
                mock_thread.assert_not_called()
        finally:
            with _orcaslicer_worker_lock:
                worker_mod._orcaslicer_worker_active = original

    @patch("core.services.slicing_worker._estimate_part_in_background")
    def test_worker_loop_processes_pending_sequentially(self, mock_estimate: "patch"):
        """Worker loop picks pending parts one by one."""
        import core.services.slicing_worker as worker_mod
        from core.services.slicing_worker import _orcaslicer_worker_loop

        # Create two parts with PENDING status
        p1 = Part.objects.create(
            name="Worker P1",
            estimation_status=Part.ESTIMATION_PENDING,
        )
        p2 = Part.objects.create(
            name="Worker P2",
            estimation_status=Part.ESTIMATION_PENDING,
        )

        # Mock _estimate_part_in_background to mark parts as SUCCESS
        def fake_estimate(part_pk: int) -> None:
            Part.objects.filter(pk=part_pk).update(
                estimation_status=Part.ESTIMATION_SUCCESS,
            )

        mock_estimate.side_effect = fake_estimate

        # Set worker as active (the loop expects this)
        with worker_mod._orcaslicer_worker_lock:
            worker_mod._orcaslicer_worker_active = True

        # Mock the cross-process file lock so the test is hermetic and does
        # not contend on the shared on-disk lock file (a source of flaky
        # failures when tests run in parallel across processes).
        with (
            patch.object(worker_mod, "_acquire_file_lock", return_value=MagicMock()),
            patch("core.services.slicing_worker.fcntl"),
        ):
            _orcaslicer_worker_loop()

        # Both parts should have been processed
        self.assertEqual(mock_estimate.call_count, 2)
        # Worker should have processed p1 first (lower pk)
        first_call_pk = mock_estimate.call_args_list[0][0][0]
        second_call_pk = mock_estimate.call_args_list[1][0][0]
        self.assertEqual(first_call_pk, p1.pk)
        self.assertEqual(second_call_pk, p2.pk)

    @patch("core.services.slicing_worker._estimate_part_in_background")
    def test_worker_loop_stops_when_no_pending(self, mock_estimate: "patch"):
        """Worker loop stops gracefully when no pending work exists."""
        import core.services.slicing_worker as worker_mod
        from core.services.slicing_worker import _orcaslicer_worker_loop

        # No pending parts or jobs
        with worker_mod._orcaslicer_worker_lock:
            worker_mod._orcaslicer_worker_active = True

        with (
            patch.object(worker_mod, "_acquire_file_lock", return_value=MagicMock()),
            patch("core.services.slicing_worker.fcntl"),
        ):
            _orcaslicer_worker_loop()

        mock_estimate.assert_not_called()
        # Worker should have set itself as inactive
        self.assertFalse(worker_mod._orcaslicer_worker_active)

    @patch("core.services.slicing_worker._slice_job_in_background")
    @patch("core.services.slicing_worker._estimate_part_in_background")
    def test_worker_loop_processes_slicing_before_estimations(self, mock_estimate: "patch", mock_slice: "patch"):
        """Worker processes slicing jobs before estimation parts."""
        import core.services.slicing_worker as worker_mod
        from core.services.slicing_worker import _orcaslicer_worker_loop

        # Create a pending estimation
        part = Part.objects.create(
            name="Est Part",
            estimation_status=Part.ESTIMATION_PENDING,
        )

        # Create a pending slicing job
        job = PrintJob.objects.create(
            status=PrintJob.STATUS_PENDING,
            created_by=self.user,
        )

        call_order = []

        def fake_estimate(part_pk: int) -> None:
            call_order.append(("estimate", part_pk))
            Part.objects.filter(pk=part_pk).update(
                estimation_status=Part.ESTIMATION_SUCCESS,
            )

        def fake_slice(job_pk: int) -> None:
            call_order.append(("slice", job_pk))
            PrintJob.objects.filter(pk=job_pk).update(
                status=PrintJob.STATUS_SLICED,
            )

        mock_estimate.side_effect = fake_estimate
        mock_slice.side_effect = fake_slice

        with worker_mod._orcaslicer_worker_lock:
            worker_mod._orcaslicer_worker_active = True

        with (
            patch.object(worker_mod, "_acquire_file_lock", return_value=MagicMock()),
            patch("core.services.slicing_worker.fcntl"),
        ):
            _orcaslicer_worker_loop()

        # Slicing should have been processed first
        self.assertEqual(len(call_order), 2)
        self.assertEqual(call_order[0], ("slice", job.pk))
        self.assertEqual(call_order[1], ("estimate", part.pk))

    @patch("core.views.print_jobs._start_orcaslicer_worker")
    def test_slice_view_queues_job_as_pending(self, mock_start: "patch"):
        """PrintJobSliceView sets job to PENDING and starts worker."""
        from core.models import OrcaMachineProfile

        machine = OrcaMachineProfile.objects.create(
            name="Test Machine",
            orca_name="test_machine",
            state=OrcaMachineProfile.STATE_RESOLVED,
            instantiation=True,
        )
        job = PrintJob.objects.create(
            status=PrintJob.STATUS_DRAFT,
            machine_profile=machine,
            created_by=self.user,
        )
        self.part.stl_file = SimpleUploadedFile("test.stl", b"solid test")
        self.part.save()
        PrintJobPart.objects.create(print_job=job, part=self.part, quantity=1)

        self.client.login(username="testuser", password="testpass123")
        resp = self.client.post(reverse("core:printjob_slice", args=[job.pk]))

        job.refresh_from_db()
        self.assertEqual(job.status, PrintJob.STATUS_PENDING)
        mock_start.assert_called_once()
        self.assertEqual(resp.status_code, 302)


class SlicingWorkerLockTests(TestCase):
    """Tests for the cross-process file-lock handoff of the OrcaSlicer worker.

    The lock must be held *continuously* across the thread handoff so a
    second gunicorn process cannot slip in and start a parallel worker.
    """

    def _set_active(self, value: bool) -> None:
        import core.services.slicing_worker as worker_mod

        with worker_mod._orcaslicer_worker_lock:
            worker_mod._orcaslicer_worker_active = value

    def tearDown(self):
        self._set_active(False)
        super().tearDown()

    def test_continuation_thread_inherits_lock_without_releasing(self):
        """When work remains at handoff, the file lock is passed to the new
        thread and NOT released in between (no window for another process)."""
        import core.services.slicing_worker as worker_mod
        from core.services.slicing_worker import _orcaslicer_worker_loop

        fake_fh = MagicMock()
        self._set_active(True)

        with (
            patch.object(worker_mod, "_acquire_file_lock", return_value=fake_fh),
            patch.object(worker_mod, "_has_pending_work", return_value=True),
            patch("core.services.slicing_worker.fcntl") as mock_fcntl,
            patch("threading.Thread") as mock_thread,
        ):
            _orcaslicer_worker_loop()

        mock_thread.assert_called_once()
        passed_kwargs = mock_thread.call_args.kwargs
        self.assertEqual(passed_kwargs["kwargs"]["lock_fh"], fake_fh)
        # Lock must NOT be released (no flock(UN), no close) during handoff.
        mock_fcntl.flock.assert_not_called()
        fake_fh.close.assert_not_called()

    def test_lock_released_when_no_pending_work(self):
        """With no work left, the lock is released and the worker deactivates."""
        import core.services.slicing_worker as worker_mod
        from core.services.slicing_worker import _orcaslicer_worker_loop

        fake_fh = MagicMock()
        self._set_active(True)

        with (
            patch.object(worker_mod, "_acquire_file_lock", return_value=fake_fh),
            patch.object(worker_mod, "_has_pending_work", return_value=False),
            patch("core.services.slicing_worker.fcntl") as mock_fcntl,
            patch("threading.Thread") as mock_thread,
        ):
            _orcaslicer_worker_loop()

        mock_thread.assert_not_called()
        mock_fcntl.flock.assert_called_once()  # LOCK_UN
        fake_fh.close.assert_called_once()
        self.assertFalse(worker_mod._orcaslicer_worker_active)

    def test_no_pending_releases_lock_before_clearing_active_flag(self):
        """Race guard: on the no-work path the cross-process file lock must
        be released BEFORE the in-process ``_orcaslicer_worker_active`` flag
        is cleared. Otherwise a concurrent enqueue could clear->see the flag,
        spawn a worker that fails to acquire the still-held lock, give up,
        and strand the newly pending item."""
        import core.services.slicing_worker as worker_mod
        from core.services.slicing_worker import _orcaslicer_worker_loop

        fake_fh = MagicMock()
        self._set_active(True)
        observed = {}

        def record_flock(fh, op):
            # Capture whether the worker still advertises itself as active
            # at the moment the file lock is released.
            observed["active_at_release"] = worker_mod._orcaslicer_worker_active

        with (
            patch.object(worker_mod, "_acquire_file_lock", return_value=fake_fh),
            patch.object(worker_mod, "_has_pending_work", return_value=False),
            patch("core.services.slicing_worker.fcntl") as mock_fcntl,
        ):
            mock_fcntl.flock.side_effect = record_flock
            _orcaslicer_worker_loop()

        self.assertTrue(
            observed.get("active_at_release"),
            "file lock was released only after the active flag was cleared",
        )
        self.assertFalse(worker_mod._orcaslicer_worker_active)

    def test_lock_acquire_retries_when_pending_work(self):
        """If the initial file-lock acquisition fails but pending work
        exists, the worker retries (the holder may be about to stop in the
        narrow window after its no-work recheck) instead of giving up and
        stranding the item until the next enqueue."""
        import core.services.slicing_worker as worker_mod
        from core.services.slicing_worker import _orcaslicer_worker_loop

        fake_fh = MagicMock()
        self._set_active(True)

        with (
            patch.object(worker_mod, "_acquire_file_lock", side_effect=[None, fake_fh]) as mock_acq,
            patch.object(worker_mod, "_has_pending_work", return_value=True),
            patch("core.services.slicing_worker.time.sleep") as mock_sleep,
            patch("core.services.slicing_worker.fcntl"),
            patch("threading.Thread"),
        ):
            _orcaslicer_worker_loop()

        self.assertEqual(mock_acq.call_count, 2)  # retried after the first failure
        mock_sleep.assert_called()  # backed off between attempts

    def test_lock_acquire_gives_up_after_bounded_retries(self):
        """The retry is bounded — a persistently held lock does not loop
        forever; the worker eventually deactivates."""
        import core.services.slicing_worker as worker_mod
        from core.services.slicing_worker import _orcaslicer_worker_loop

        self._set_active(True)

        with (
            patch.object(worker_mod, "_acquire_file_lock", return_value=None) as mock_acq,
            patch.object(worker_mod, "_has_pending_work", return_value=True),
            patch("core.services.slicing_worker.time.sleep"),
        ):
            _orcaslicer_worker_loop()

        self.assertFalse(worker_mod._orcaslicer_worker_active)
        self.assertGreaterEqual(mock_acq.call_count, 2)  # retried before giving up

    def test_lock_acquire_failure_without_pending_exits_immediately(self):
        """No pending work + lock held elsewhere → no retry, exit at once."""
        import core.services.slicing_worker as worker_mod
        from core.services.slicing_worker import _orcaslicer_worker_loop

        self._set_active(True)

        with (
            patch.object(worker_mod, "_acquire_file_lock", return_value=None) as mock_acq,
            patch.object(worker_mod, "_has_pending_work", return_value=False),
            patch("core.services.slicing_worker.time.sleep") as mock_sleep,
        ):
            _orcaslicer_worker_loop()

        self.assertFalse(worker_mod._orcaslicer_worker_active)
        self.assertEqual(mock_acq.call_count, 1)  # no retry when nothing pending
        mock_sleep.assert_not_called()

    def test_lock_acquire_give_up_closes_connection(self):
        """The lock-acquisition give-up path runs before the try/finally, so
        it must close this thread's DB connection itself (the retry loop
        opened one via _has_pending_work) to avoid leaking connections."""
        import core.services.slicing_worker as worker_mod
        from core.services.slicing_worker import _orcaslicer_worker_loop

        self._set_active(True)
        with (
            patch.object(worker_mod, "_acquire_file_lock", return_value=None),
            patch.object(worker_mod, "_has_pending_work", return_value=True),
            patch("core.services.slicing_worker.time.sleep"),
            patch("django.db.connection.close") as mock_close,
        ):
            _orcaslicer_worker_loop()

        self.assertFalse(worker_mod._orcaslicer_worker_active)
        mock_close.assert_called()

    def test_pending_check_error_during_retry_is_handled(self):
        """A DB error in the retry-loop pending check must not propagate,
        must not leave the worker flag stuck True, and must close the
        connection."""
        import core.services.slicing_worker as worker_mod
        from core.services.slicing_worker import _orcaslicer_worker_loop

        self._set_active(True)
        with (
            patch.object(worker_mod, "_acquire_file_lock", return_value=None),
            patch.object(worker_mod, "_has_pending_work", side_effect=Exception("db down")),
            patch("core.services.slicing_worker.time.sleep"),
            patch("django.db.connection.close") as mock_close,
        ):
            _orcaslicer_worker_loop()  # must not raise

        self.assertFalse(worker_mod._orcaslicer_worker_active)
        mock_close.assert_called()

    def test_cleanup_pending_check_error_fully_releases(self):
        """A DB error in the finally-block pending re-check must not escape:
        it would otherwise leave the file lock held, the active flag set,
        and the connection open — permanently wedging the worker. Treat it
        as no-pending and fully clean up."""
        import core.services.slicing_worker as worker_mod
        from core.services.slicing_worker import _orcaslicer_worker_loop

        fake_fh = MagicMock()
        self._set_active(True)
        with (
            patch.object(worker_mod, "_acquire_file_lock", return_value=fake_fh),
            patch.object(worker_mod, "_has_pending_work", side_effect=Exception("db down")),
            patch("core.services.slicing_worker.fcntl") as mock_fcntl,
            patch("django.db.connection.close") as mock_close,
            patch("threading.Thread") as mock_thread,
        ):
            _orcaslicer_worker_loop()  # must not raise

        self.assertFalse(worker_mod._orcaslicer_worker_active)
        mock_fcntl.flock.assert_called()  # LOCK_UN
        fake_fh.close.assert_called_once()
        mock_close.assert_called()  # DB connection closed
        mock_thread.assert_not_called()  # no handoff on error

    def test_handoff_failure_releases_lock_under_worker_lock(self):
        """If the continuation Thread.start() fails, the file lock must be
        released and the active flag cleared while holding
        ``_orcaslicer_worker_lock`` (atomically), so a concurrent enqueue
        can't see a freed lock with the flag still set and strand the item."""
        import core.services.slicing_worker as worker_mod
        from core.services.slicing_worker import _orcaslicer_worker_loop

        fake_fh = MagicMock()
        self._set_active(True)
        observed = {}

        def record_flock(fh, op):
            # If the worker holds _orcaslicer_worker_lock at release time,
            # this (same-thread, non-reentrant) acquire fails.
            acquired = worker_mod._orcaslicer_worker_lock.acquire(blocking=False)
            observed["held_by_worker"] = not acquired
            if acquired:
                worker_mod._orcaslicer_worker_lock.release()

        with (
            patch.object(worker_mod, "_acquire_file_lock", return_value=fake_fh),
            patch.object(worker_mod, "_has_pending_work", return_value=True),
            patch("core.services.slicing_worker.fcntl") as mock_fcntl,
            patch("threading.Thread") as mock_thread,
        ):
            mock_thread.return_value.start.side_effect = RuntimeError("can't start thread")
            mock_fcntl.flock.side_effect = record_flock
            _orcaslicer_worker_loop()

        self.assertTrue(
            observed.get("held_by_worker"),
            "file lock released outside _orcaslicer_worker_lock (race window)",
        )
        self.assertFalse(worker_mod._orcaslicer_worker_active)
        fake_fh.close.assert_called_once()

    def test_inherited_lock_is_not_reacquired(self):
        """A continuation loop given a lock handle must not re-acquire it."""
        import core.services.slicing_worker as worker_mod
        from core.services.slicing_worker import _orcaslicer_worker_loop

        fake_fh = MagicMock()
        self._set_active(True)

        with (
            patch.object(worker_mod, "_acquire_file_lock") as mock_acquire,
            patch.object(worker_mod, "_has_pending_work", return_value=False),
            patch("core.services.slicing_worker.fcntl"),
        ):
            _orcaslicer_worker_loop(lock_fh=fake_fh)

        mock_acquire.assert_not_called()
