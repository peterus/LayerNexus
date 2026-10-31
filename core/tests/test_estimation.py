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
            project=self.project,
            estimation_status=Part.ESTIMATION_PENDING,
        )
        p2 = Part.objects.create(
            name="Worker P2",
            project=self.project,
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
            project=self.project,
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

    def test_estimate_part_with_own_preset_succeeds(self):
        """The worker reaches a successful estimate using the part's own print preset."""
        from core.services import slicing_worker

        self.part.stl_file = SimpleUploadedFile("test.stl", b"solid test")
        self.part.print_preset = self.preset
        # The worker loop claims a part (PENDING → ESTIMATING) before invoking the slice;
        # the completion write is guarded on that status, so mirror the claim here.
        self.part.estimation_status = Part.ESTIMATION_ESTIMATING
        self.part.save()

        fake_result = MagicMock(
            total_filament_grams=10.0,
            total_filament_mm=3000.0,
            total_print_time_seconds=600,
        )
        fake_client = MagicMock()
        fake_client.slice_bundle.return_value = fake_result

        with (
            patch.object(slicing_worker, "_find_compatible_machine", return_value=MagicMock()),
            patch.object(slicing_worker, "create_3mf_bundle", return_value=(b"3mf", [])),
            patch.object(slicing_worker, "_build_slicer_kwargs", return_value={}),
            patch.object(slicing_worker, "OrcaSlicerAPIClient", return_value=fake_client),
        ):
            slicing_worker._estimate_part_in_background(self.part.pk)

        self.part.refresh_from_db()
        self.assertEqual(self.part.estimation_status, Part.ESTIMATION_SUCCESS)
        self.assertNotIn("select_related", self.part.estimation_error)
        self.assertEqual(self.part.filament_used_grams, 10.0)

    def test_is_3mf_property_detects_extension(self):
        """``Part.is_3mf`` is True only for ``.3mf`` uploads, not ``.stl``."""
        self.part.stl_file = SimpleUploadedFile("model.3mf", b"PK\x03\x04fake")
        self.part.save()
        self.part.refresh_from_db()
        self.assertTrue(self.part.is_3mf)

        self.part.stl_file = SimpleUploadedFile("model.stl", b"solid test")
        self.part.save()
        self.part.refresh_from_db()
        self.assertFalse(self.part.is_3mf)

    def test_3mf_estimation_bypasses_bundle_creation(self):
        """3MF uploads are sent to the slicer verbatim, never re-bundled."""
        from core.services import slicing_worker

        raw_3mf = b"PK\x03\x04fake-3mf-bytes"
        self.part.stl_file = SimpleUploadedFile("model.3mf", raw_3mf)
        self.part.print_preset = self.preset
        # Mirror the worker's PENDING → ESTIMATING claim (completion is guarded on it).
        self.part.estimation_status = Part.ESTIMATION_ESTIMATING
        self.part.save()

        fake_result = MagicMock(
            total_filament_grams=10.0,
            total_filament_mm=3000.0,
            total_print_time_seconds=600,
        )
        fake_client = MagicMock()
        fake_client.slice_bundle.return_value = fake_result

        with (
            patch.object(slicing_worker, "_find_compatible_machine", return_value=MagicMock()),
            patch.object(slicing_worker, "create_3mf_bundle") as mock_bundle,
            patch.object(slicing_worker, "_build_slicer_kwargs", return_value={}),
            patch.object(slicing_worker, "OrcaSlicerAPIClient", return_value=fake_client),
        ):
            slicing_worker._estimate_part_in_background(self.part.pk)

        # The uploaded 3MF must be sliced as-is, without going through the bundler.
        mock_bundle.assert_not_called()
        fake_client.slice_bundle.assert_called_once()
        self.assertEqual(fake_client.slice_bundle.call_args[0][0], raw_3mf)

        self.part.refresh_from_db()
        self.assertEqual(self.part.estimation_status, Part.ESTIMATION_SUCCESS)

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


class SliceJobUsesJobPresetTests(TestCase):
    def test_slice_job_passes_job_print_preset_to_kwargs(self) -> None:
        from unittest import mock

        from core.models import OrcaMachineProfile, OrcaPrintPreset, Part, PrintJob, PrintJobPart
        from core.services import slicing_worker

        job_preset = OrcaPrintPreset.objects.create(
            name="JobPreset", orca_name="JobPreset", state=OrcaPrintPreset.STATE_RESOLVED, instantiation=True
        )
        part_preset = OrcaPrintPreset.objects.create(
            name="PartPreset", orca_name="PartPreset", state=OrcaPrintPreset.STATE_RESOLVED, instantiation=True
        )
        machine = OrcaMachineProfile.objects.create(
            name="M", orca_name="M", state=OrcaMachineProfile.STATE_RESOLVED, instantiation=True
        )
        from core.models import Project

        part = Part.objects.create(name="p", project=Project.objects.create(name="Proj"), print_preset=part_preset)
        part.stl_file.name = "stl_files/x.stl"
        part.save(update_fields=["stl_file"])
        job = PrintJob.objects.create(name="J", machine_profile=machine, print_preset=job_preset)
        PrintJobPart.objects.create(print_job=job, part=part, quantity=1)

        captured: dict = {}

        def fake_build_kwargs(machine_profile, print_preset, filament_profile):
            captured["print_preset"] = print_preset
            return {}

        with (
            mock.patch.object(slicing_worker, "_build_slicer_kwargs", side_effect=fake_build_kwargs),
            mock.patch.object(slicing_worker, "create_3mf_bundle", return_value=(b"3mf", [])),
            mock.patch.object(slicing_worker, "OrcaSlicerAPIClient") as client_cls,
        ):
            client_cls.return_value.slice_bundle.return_value = mock.Mock(
                plates=[], total_filament_grams=None, total_filament_mm=None, total_print_time_seconds=None
            )
            slicing_worker._slice_job_in_background(job.pk)

        self.assertEqual(captured["print_preset"], job_preset)


class EstimationPresetResolutionTests(TestCase):
    """Bottom-up preset resolution: own preset, else nearest project default up the tree."""

    def _preset(self, name):
        from core.models import OrcaPrintPreset

        return OrcaPrintPreset.objects.create(
            name=name, orca_name=name, state=OrcaPrintPreset.STATE_RESOLVED, instantiation=True
        )

    def test_own_preset_wins_over_project_defaults(self) -> None:
        from core.models import Part, Project

        override = self._preset("Override")
        root = Project.objects.create(name="R", default_print_preset=self._preset("PR"))
        sub = Project.objects.create(name="S", parent=root, quantity=1, default_print_preset=self._preset("PS"))
        part = Part.objects.create(project=sub, quantity=1, name="p", print_preset=override)
        self.assertEqual(part.effective_print_preset, override)
        self.assertEqual(part.effective_print_preset_id, override.pk)

    def test_nearest_project_default_wins(self) -> None:
        from core.models import Part, Project

        root = Project.objects.create(name="R", default_print_preset=self._preset("PR"))
        sub_preset = self._preset("PS")
        sub = Project.objects.create(name="S", parent=root, quantity=1, default_print_preset=sub_preset)
        part = Part.objects.create(project=sub, quantity=1, name="p")
        self.assertEqual(part.effective_print_preset, sub_preset)

    def test_inherits_root_default_through_presetless_sub_project(self) -> None:
        from core.models import Part, Project

        root_preset = self._preset("PR")
        root = Project.objects.create(name="R", default_print_preset=root_preset)
        sub = Project.objects.create(name="S", parent=root, quantity=2)
        leaf = Project.objects.create(name="L", parent=sub, quantity=3)
        part = Part.objects.create(project=leaf, quantity=1, name="p")
        self.assertEqual(part.effective_print_preset, root_preset)
        self.assertEqual(part.effective_print_preset_id, root_preset.pk)

    def test_no_preset_anywhere_resolves_to_none(self) -> None:
        from core.models import Part, Project

        root = Project.objects.create(name="R")
        sub = Project.objects.create(name="S", parent=root, quantity=1)
        part = Part.objects.create(project=sub, quantity=1, name="p")
        self.assertIsNone(part.effective_print_preset)
        self.assertIsNone(part.effective_print_preset_id)

    def test_worker_uses_inherited_preset(self) -> None:
        from unittest import mock

        from core.models import Part, Project
        from core.services import slicing_worker

        preset_a = self._preset("A")
        root = Project.objects.create(name="R", default_print_preset=preset_a)
        sub = Project.objects.create(name="S", parent=root, quantity=2)
        part = Part.objects.create(project=sub, quantity=1, name="p", estimation_status=Part.ESTIMATION_ESTIMATING)
        part.stl_file.name = "stl_files/p.stl"
        part.save(update_fields=["stl_file"])

        with (
            mock.patch.object(slicing_worker, "_find_compatible_machine", return_value=None) as find_machine,
            mock.patch.object(slicing_worker, "OrcaSlicerAPIClient") as client_cls,
        ):
            slicing_worker._estimate_part_in_background(part.pk)

        find_machine.assert_called_once_with(preset_a)
        client_cls.assert_not_called()
        part.refresh_from_db()
        self.assertNotEqual(part.estimation_status, Part.ESTIMATION_ERROR)

    def test_worker_no_preset_sets_none(self) -> None:
        from unittest import mock

        from core.models import Part, Project
        from core.services import slicing_worker

        root = Project.objects.create(name="R")
        sub = Project.objects.create(name="S", parent=root, quantity=1)
        part = Part.objects.create(project=sub, quantity=1, name="p", estimation_status=Part.ESTIMATION_ESTIMATING)
        part.stl_file.name = "stl_files/p.stl"
        part.save(update_fields=["stl_file"])

        with (
            mock.patch.object(slicing_worker, "_find_compatible_machine") as find_machine,
            mock.patch.object(slicing_worker, "OrcaSlicerAPIClient") as client_cls,
        ):
            slicing_worker._estimate_part_in_background(part.pk)

        find_machine.assert_not_called()
        client_cls.assert_not_called()
        part.refresh_from_db()
        self.assertEqual(part.estimation_status, Part.ESTIMATION_NONE)
        self.assertIsNone(part.estimated_with_preset_id)


class ReEstimateEligibilityTests(TestCase):
    def _preset(self, name):
        from core.models import OrcaPrintPreset

        return OrcaPrintPreset.objects.create(
            name=name, orca_name=name, state=OrcaPrintPreset.STATE_RESOLVED, instantiation=True
        )

    def test_is_estimable_true_for_legacy_part_with_project_default(self) -> None:
        from core.models import Part, Project

        project = Project.objects.create(name="Proj", default_print_preset=self._preset("P"))
        part = Part.objects.create(project=project, quantity=1, name="p")  # no override
        part.stl_file.name = "stl_files/p.stl"
        part.save(update_fields=["stl_file"])
        self.assertTrue(part.is_estimable())

    def test_is_estimable_false_without_any_preset(self) -> None:
        from core.models import Part, Project

        part = Part.objects.create(project=Project.objects.create(name="Proj"), name="p")
        part.stl_file.name = "stl_files/p.stl"
        part.save(update_fields=["stl_file"])
        self.assertFalse(part.is_estimable())

    def test_is_estimable_true_with_inherited_root_default(self) -> None:
        from core.models import Part, Project

        root = Project.objects.create(name="R", default_print_preset=self._preset("PR"))
        sub = Project.objects.create(name="S", parent=root, quantity=1)
        part = Part.objects.create(project=sub, quantity=1, name="p")
        self.assertFalse(part.is_estimable())  # no model file yet
        part.stl_file.name = "stl_files/p.stl"
        part.save(update_fields=["stl_file"])
        self.assertTrue(part.is_estimable())

    def test_gui_project_re_estimate_queues_legacy_part(self) -> None:
        from unittest import mock

        from django.contrib.auth.models import Group, Permission, User
        from django.urls import reverse

        from core.models import Part, Project

        project = Project.objects.create(name="Proj", default_print_preset=self._preset("P"))
        part = Part.objects.create(project=project, quantity=1, name="p")  # no override, legacy
        part.stl_file.name = "stl_files/p.stl"
        part.save(update_fields=["stl_file"])

        user = User.objects.create_user("designer", password="pw")
        perm = Permission.objects.get(codename="can_manage_projects")
        group, _ = Group.objects.get_or_create(name="Designer")
        group.permissions.add(perm)
        user.groups.add(group)
        self.client.force_login(user)

        with mock.patch("core.views.helpers._start_orcaslicer_worker"):
            resp = self.client.post(reverse("core:project_re_estimate", kwargs={"pk": project.pk}))
        self.assertEqual(resp.status_code, 302)
        part.refresh_from_db()
        # Previously this part was skipped (no own preset); now it is queued.
        self.assertEqual(part.estimation_status, Part.ESTIMATION_PENDING)


class SliceLegacyJobFallbackTests(TestCase):
    """A legacy job with no pinned preset falls back to the first part's own preset (Copilot #54 r3)."""

    def test_slice_legacy_job_uses_first_part_preset(self) -> None:
        from unittest import mock

        from core.models import OrcaMachineProfile, OrcaPrintPreset, Part, PrintJob, PrintJobPart
        from core.services import slicing_worker

        part_preset = OrcaPrintPreset.objects.create(
            name="PartPreset", orca_name="PartPreset", state=OrcaPrintPreset.STATE_RESOLVED, instantiation=True
        )
        machine = OrcaMachineProfile.objects.create(
            name="M", orca_name="M", state=OrcaMachineProfile.STATE_RESOLVED, instantiation=True
        )
        from core.models import Project

        part = Part.objects.create(name="p", project=Project.objects.create(name="Proj"), print_preset=part_preset)
        part.stl_file.name = "stl_files/x.stl"
        part.save(update_fields=["stl_file"])
        # Legacy job: print_preset is NULL (created before pinning existed).
        job = PrintJob.objects.create(name="Legacy", machine_profile=machine)
        PrintJobPart.objects.create(print_job=job, part=part, quantity=1)

        captured = {}

        def fake_build_kwargs(machine_profile, print_preset, filament_profile):
            captured["print_preset"] = print_preset
            return {}

        with (
            mock.patch.object(slicing_worker, "_build_slicer_kwargs", side_effect=fake_build_kwargs),
            mock.patch.object(slicing_worker, "create_3mf_bundle", return_value=(b"3mf", [])),
            mock.patch.object(slicing_worker, "OrcaSlicerAPIClient") as client_cls,
        ):
            client_cls.return_value.slice_bundle.return_value = mock.Mock(
                plates=[], total_filament_grams=None, total_filament_mm=None, total_print_time_seconds=None
            )
            slicing_worker._slice_job_in_background(job.pk)

        self.assertEqual(captured["print_preset"], part_preset)


class WorkerConditionalCompletionTests(TestCase):
    """An in-flight estimate result is discarded if the part was re-queued meanwhile (Copilot #54 r4)."""

    def test_result_discarded_when_part_no_longer_estimating(self) -> None:
        from unittest import mock

        from core.models import OrcaMachineProfile, OrcaPrintPreset, Part, Project
        from core.services import slicing_worker

        preset = OrcaPrintPreset.objects.create(
            name="P", orca_name="P", state=OrcaPrintPreset.STATE_RESOLVED, instantiation=True
        )
        OrcaMachineProfile.objects.create(
            name="M", orca_name="M", state=OrcaMachineProfile.STATE_RESOLVED, instantiation=True
        )
        # Part carries an override so resolution succeeds, but status is PENDING (NOT the
        # ESTIMATING we claim) — simulating a re-queue that happened while slicing was in flight.
        part = Part.objects.create(
            name="p",
            project=Project.objects.create(name="Proj"),
            print_preset=preset,
            estimation_status=Part.ESTIMATION_PENDING,
        )
        part.stl_file.name = "stl_files/p.stl"
        part.save(update_fields=["stl_file"])

        with (
            mock.patch.object(
                slicing_worker, "_find_compatible_machine", return_value=OrcaMachineProfile.objects.first()
            ),
            mock.patch.object(slicing_worker, "_build_slicer_kwargs", return_value={}),
            mock.patch.object(slicing_worker, "create_3mf_bundle", return_value=(b"3mf", [])),
            mock.patch.object(slicing_worker, "OrcaSlicerAPIClient") as client_cls,
        ):
            client_cls.return_value.slice_bundle.return_value = mock.Mock(
                plates=[], total_filament_grams=12.0, total_filament_mm=3400.0, total_print_time_seconds=60
            )
            slicing_worker._estimate_part_in_background(part.pk)

        part.refresh_from_db()
        # The completion must NOT have written a SUCCESS result over the PENDING re-queue.
        self.assertNotEqual(part.estimation_status, Part.ESTIMATION_SUCCESS)
        self.assertIsNone(part.filament_used_grams)


class PartEstimateTransitionTests(TestDataMixin, TestCase):
    """Estimation state transitions live on the Part model."""

    def setUp(self):
        super().setUp()
        from core.models import OrcaPrintPreset

        self.preset = OrcaPrintPreset.objects.create(
            name="Transition Preset",
            orca_name="transition_preset",
            state=OrcaPrintPreset.STATE_RESOLVED,
        )
        self.part.stl_file = SimpleUploadedFile("t.stl", b"solid t")
        self.part.print_preset = self.preset
        self.part.filament_used_grams = 5
        self.part.estimation_status = Part.ESTIMATION_SUCCESS
        self.part.estimation_error = "old"
        self.part.estimated_with_preset = self.preset
        self.part.save()

    def test_request_estimate_resets_and_queues(self):
        """request_estimate clears the estimate and marks the part pending."""
        self.assertTrue(self.part.request_estimate())
        self.part.refresh_from_db()
        self.assertEqual(self.part.estimation_status, Part.ESTIMATION_PENDING)
        self.assertEqual(self.part.estimation_error, "")
        self.assertIsNone(self.part.filament_used_grams)
        self.assertIsNone(self.part.estimated_with_preset)

    def test_request_estimate_without_prereqs_returns_false(self):
        """Without a preset the part is left untouched."""
        Part.objects.filter(pk=self.part.pk).update(print_preset=None)
        self.part.refresh_from_db()
        self.assertFalse(self.part.request_estimate())
        self.part.refresh_from_db()
        self.assertEqual(self.part.estimation_status, Part.ESTIMATION_SUCCESS)
        self.assertEqual(self.part.filament_used_grams, 5)

    def test_claim_estimate_is_atomic(self):
        """Only the first claim of a pending part wins."""
        self.part.request_estimate()
        self.assertTrue(Part.claim_estimate(self.part.pk))
        self.assertFalse(Part.claim_estimate(self.part.pk))
        self.part.refresh_from_db()
        self.assertEqual(self.part.estimation_status, Part.ESTIMATION_ESTIMATING)

    def test_finish_estimate_ignored_when_not_estimating(self):
        """A stale result is discarded when the part was re-queued meanwhile."""
        self.part.request_estimate()
        self.assertFalse(self.part.finish_estimate(filament_used_grams=9))
        self.part.refresh_from_db()
        self.assertEqual(self.part.estimation_status, Part.ESTIMATION_PENDING)
        self.assertIsNone(self.part.filament_used_grams)

    def test_finish_estimate_stores_when_estimating(self):
        """A result is stored while the part is still estimating."""
        self.part.request_estimate()
        Part.claim_estimate(self.part.pk)
        self.assertTrue(self.part.finish_estimate(filament_used_grams=9))
        self.part.refresh_from_db()
        self.assertEqual(self.part.estimation_status, Part.ESTIMATION_SUCCESS)
        self.assertEqual(self.part.filament_used_grams, 9)

    def test_fail_estimate_only_when_estimating(self):
        """fail_estimate records the error only for an in-flight estimation."""
        self.part.request_estimate()
        self.part.fail_estimate("boom")
        self.part.refresh_from_db()
        self.assertEqual(self.part.estimation_status, Part.ESTIMATION_PENDING)
        Part.claim_estimate(self.part.pk)
        self.part.fail_estimate("boom")
        self.part.refresh_from_db()
        self.assertEqual(self.part.estimation_status, Part.ESTIMATION_ERROR)
        self.assertEqual(self.part.estimation_error, "boom")

    def test_requeue_stuck_estimates(self):
        """Parts stuck at estimating go back to pending."""
        Part.objects.filter(pk=self.part.pk).update(estimation_status=Part.ESTIMATION_ESTIMATING)
        self.assertEqual(Part.requeue_stuck_estimates(), 1)
        self.part.refresh_from_db()
        self.assertEqual(self.part.estimation_status, Part.ESTIMATION_PENDING)


class WorkerFailurePathTests(TestCase):
    """_estimate_part_in_background records failures on a claimed (estimating) part."""

    def setUp(self) -> None:
        from core.models import OrcaMachineProfile, OrcaPrintPreset, Project

        self.preset = OrcaPrintPreset.objects.create(
            name="P", orca_name="P", state=OrcaPrintPreset.STATE_RESOLVED, instantiation=True
        )
        self.machine = OrcaMachineProfile.objects.create(
            name="M", orca_name="M", state=OrcaMachineProfile.STATE_RESOLVED, instantiation=True
        )
        self.part = Part.objects.create(
            name="p",
            project=Project.objects.create(name="Proj"),
            print_preset=self.preset,
            estimation_status=Part.ESTIMATION_ESTIMATING,
        )
        self.part.stl_file.name = "stl_files/p.stl"
        self.part.save(update_fields=["stl_file"])

    def _run(self, **slice_kwargs) -> None:
        from core.services import slicing_worker

        with (
            patch.object(slicing_worker, "_find_compatible_machine", return_value=self.machine),
            patch.object(slicing_worker, "_build_slicer_kwargs", return_value={}),
            patch.object(slicing_worker, "create_3mf_bundle", return_value=(b"3mf", [])),
            patch.object(slicing_worker, "OrcaSlicerAPIClient") as client_cls,
        ):
            client_cls.return_value.slice_bundle.configure_mock(**slice_kwargs)
            slicing_worker._estimate_part_in_background(self.part.pk)
        self.part.refresh_from_db()

    def test_slicer_error_marks_part_error(self) -> None:
        """An OrcaSlicerError ends in ERROR with the message."""
        from core.services.orcaslicer import OrcaSlicerError

        self._run(side_effect=OrcaSlicerError("boom"))
        self.assertEqual(self.part.estimation_status, Part.ESTIMATION_ERROR)
        self.assertEqual(self.part.estimation_error, "boom")

    def test_unexpected_error_marks_part_error(self) -> None:
        """Any other exception ends in ERROR with an 'Unexpected error' prefix."""
        self._run(side_effect=RuntimeError("kaput"))
        self.assertEqual(self.part.estimation_status, Part.ESTIMATION_ERROR)
        self.assertEqual(self.part.estimation_error, "Unexpected error: kaput")

    def test_no_estimates_marks_part_error(self) -> None:
        """A slice without filament/time results ends in ERROR."""
        self._run(
            return_value=MagicMock(
                plates=[], total_filament_grams=None, total_filament_mm=None, total_print_time_seconds=None
            )
        )
        self.assertEqual(self.part.estimation_status, Part.ESTIMATION_ERROR)
        self.assertEqual(self.part.estimation_error, "Slicing returned no filament or time estimates.")

    def test_load_failure_marks_part_error(self) -> None:
        """If loading the part itself fails, the stub still records the error."""
        from core.services import slicing_worker

        with patch.object(Part.objects, "select_related", side_effect=RuntimeError("db gone")):
            slicing_worker._estimate_part_in_background(self.part.pk)
        self.part.refresh_from_db()
        self.assertEqual(self.part.estimation_status, Part.ESTIMATION_ERROR)
        self.assertEqual(self.part.estimation_error, "Unexpected error: db gone")
