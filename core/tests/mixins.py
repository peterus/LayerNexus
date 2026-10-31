"""Shared test helpers and base classes."""

from datetime import datetime
from typing import Any

from django.contrib.auth.models import Group, User
from django.test import TestCase
from django.utils import timezone

from core.models import Part, PrinterProfile, PrintJob, PrintJobPlate, PrintQueue, Project


def queue_state_fields(state: str, when: datetime | None = None) -> dict[str, Any]:
    """Return the ``PrintQueue`` field values that produce an open attempt state.

    Args:
        state: ``"waiting"``, ``"printing"`` or ``"awaiting_review"``.
        when: Timestamp to use (defaults to now).

    Returns:
        Keyword arguments for ``PrintQueue.objects.create()`` / ``.update()``.
    """
    when = when or timezone.now()
    if state == "waiting":
        return {"started_at": None, "completed_at": None}
    if state == "printing":
        return {"started_at": when, "completed_at": None}
    if state == "awaiting_review":
        return {"started_at": when, "completed_at": when}
    raise ValueError(f"Unknown open attempt state: {state}")


def add_attempt(
    plate: PrintJobPlate,
    printer: PrinterProfile | None,
    state: str = "waiting",
    *,
    when: datetime | None = None,
    **kwargs: Any,
) -> PrintQueue:
    """Create a print attempt for ``plate`` in a given state.

    The plate state and the job's display status are derived from attempts, so
    tests set them up through this helper instead of a stored status.

    Args:
        plate: The plate of the attempt.
        printer: Printer of the attempt (``None`` = unknown, as for backfilled history).
        state: ``"waiting"`` / ``"printing"`` / ``"awaiting_review"`` (open) or
            ``"passed"`` / ``"failed"`` / ``"cancelled"`` (resolved).
        when: Timestamp for the state fields (defaults to now).
        **kwargs: Further ``PrintQueue`` field values (e.g. ``is_retry``, ``max_retries``).

    Returns:
        The created ``PrintQueue`` row.
    """
    when = when or timezone.now()
    if state in (PrintQueue.RESULT_PASSED, PrintQueue.RESULT_FAILED, PrintQueue.RESULT_CANCELLED):
        fields: dict[str, Any] = {**queue_state_fields("awaiting_review", when), "result": state, "resolved_at": when}
    else:
        fields = queue_state_fields(state, when)
    return PrintQueue.objects.create(plate=plate, printer=printer, **{**fields, **kwargs})


def complete_plate(plate: PrintJobPlate, printer: PrinterProfile | None = None, **kwargs: Any) -> PrintQueue:
    """Give ``plate`` a passed attempt (derived state ``completed``)."""
    return add_attempt(plate, printer, PrintQueue.RESULT_PASSED, **kwargs)


def fail_plate(plate: PrintJobPlate, printer: PrinterProfile | None = None, max_retries: int = 3) -> list[PrintQueue]:
    """Give ``plate`` ``max_retries`` failed attempts (derived state ``failed``)."""
    return [
        add_attempt(plate, printer, PrintQueue.RESULT_FAILED, is_retry=index > 0, max_retries=max_retries)
        for index in range(max_retries)
    ]


def make_sliced_job(name: str = "Job", plates: int = 1, **kwargs: Any) -> PrintJob:
    """Create a ``sliced`` job with ``plates`` plates (numbered from 1)."""
    job = PrintJob.objects.create(name=name, status=PrintJob.STATUS_SLICED, **kwargs)
    for number in range(1, plates + 1):
        PrintJobPlate.objects.create(print_job=job, plate_number=number)
    return job


def make_completed_job(
    name: str = "Done", plates: int = 1, printer: PrinterProfile | None = None, **kwargs: Any
) -> PrintJob:
    """Create a sliced job whose plates all have a passed attempt (display status ``completed``)."""
    job = make_sliced_job(name, plates, **kwargs)
    for plate in job.plates.all():
        complete_plate(plate, printer)
    return job


class TestDataMixin:
    """Create common test data for reuse across test classes."""

    def setUp(self):
        super().setUp()
        self.user = User.objects.create_user(username="testuser", password="testpass123")
        # Add user to Admin group so they have all permissions
        admin_group = Group.objects.get(name="Admin")
        self.user.groups.add(admin_group)
        self.other_user = User.objects.create_user(username="otheruser", password="otherpass123")
        self.project = Project.objects.create(name="Test Project", description="A test project", created_by=self.user)
        self.part = Part.objects.create(
            name="Test Part",
            project=self.project,
            quantity=3,
            color="red",
            material="PLA",
            filament_used_grams=10.5,
            filament_used_meters=3.4,
        )
        self.printer = PrinterProfile.objects.create(
            name="Test Printer",
            created_by=self.user,
        )
        # Other user data for isolation tests
        self.other_project = Project.objects.create(name="Other Project", created_by=self.other_user)
        self.other_part = Part.objects.create(name="Other Part", project=self.other_project, quantity=1)
        self.other_printer = PrinterProfile.objects.create(name="Other Printer", created_by=self.other_user)


class _RBACTestBase(TestCase):
    """Base class for RBAC tests. Creates users with different roles."""

    def setUp(self):
        super().setUp()
        # Create users for each role
        self.admin_user = User.objects.create_user(username="admin_user", password="testpass123")
        admin_group = Group.objects.get(name="Admin")
        self.admin_user.groups.add(admin_group)
        self.admin_user.is_staff = True
        self.admin_user.is_superuser = True
        self.admin_user.save()

        self.operator_user = User.objects.create_user(username="operator_user", password="testpass123")
        operator_group = Group.objects.get(name="Operator")
        self.operator_user.groups.add(operator_group)

        self.designer_user = User.objects.create_user(username="designer_user", password="testpass123")
        designer_group = Group.objects.get(name="Designer")
        self.designer_user.groups.add(designer_group)

        # Shared test data
        self.project = Project.objects.create(name="RBAC Project", created_by=self.admin_user)
        self.part = Part.objects.create(name="RBAC Part", project=self.project, quantity=1)
        self.printer = PrinterProfile.objects.create(name="RBAC Printer", created_by=self.admin_user)
