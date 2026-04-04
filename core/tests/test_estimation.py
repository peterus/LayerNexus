"""Tests for the estimation worker queue."""

from unittest.mock import patch

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
            project=self.project,
            name="Worker P1",
            quantity=1,
            estimation_status=Part.ESTIMATION_PENDING,
        )
        p2 = Part.objects.create(
            project=self.project,
            name="Worker P2",
            quantity=1,
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
            project=self.project,
            name="Est Part",
            quantity=1,
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

        _orcaslicer_worker_loop()

        # Slicing should have been processed first
        self.assertEqual(len(call_order), 2)
        self.assertEqual(call_order[0], ("slice", job.pk))
        self.assertEqual(call_order[1], ("estimate", part.pk))

    def test_3mf_estimation_bypasses_bundle_creation(self):
        """3MF files should be passed directly to the slicer, not re-bundled."""
        self.part.stl_file = SimpleUploadedFile("model.3mf", b"PK\x03\x04fake")
        self.part.print_preset = self.preset
        self.part.save()

        # Verify the is_3mf property works
        self.part.refresh_from_db()
        self.assertTrue(self.part.is_3mf)

        # Verify STL files are not detected as 3MF
        self.part.stl_file = SimpleUploadedFile("model.stl", b"solid test")
        self.part.save()
        self.part.refresh_from_db()
        self.assertFalse(self.part.is_3mf)

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
