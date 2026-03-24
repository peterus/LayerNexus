"""Shared test helpers and base classes."""

from django.contrib.auth.models import Group, User
from django.test import TestCase

from core.models import Part, PrinterProfile, Project


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
            project=self.project,
            name="Test Part",
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
        self.other_part = Part.objects.create(project=self.other_project, name="Other Part", quantity=1)
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
        self.part = Part.objects.create(project=self.project, name="RBAC Part", quantity=1)
        self.printer = PrinterProfile.objects.create(name="RBAC Printer", created_by=self.admin_user)
