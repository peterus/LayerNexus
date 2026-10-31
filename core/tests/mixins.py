"""Shared test helpers and base classes."""

from datetime import datetime
from typing import Any

from django.contrib.auth.models import Group, User
from django.test import TestCase
from django.utils import timezone

from core.models import Part, PrinterProfile, Project


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
