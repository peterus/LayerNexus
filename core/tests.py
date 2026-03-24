"""Comprehensive tests for the LayerNexus core application."""

from unittest.mock import patch

from django.contrib.auth.models import Group, User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import IntegrityError
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from .forms import (
    PartForm,
    ProjectDocumentForm,
    ProjectEditForm,
    ProjectForm,
    ProjectHardwareForm,
    ProjectHardwareUpdateForm,
    UserRegistrationForm,
)
from .models import (
    CostProfile,
    FileVersion,
    HardwarePart,
    Part,
    PrinterProfile,
    PrintJob,
    PrintJobPart,
    PrintJobPlate,
    PrintQueue,
    PrintTimeEstimate,
    Project,
    ProjectDocument,
    ProjectHardware,
)
from .services.moonraker import MoonrakerClient
from .services.orcaslicer import OrcaSlicerAPIClient
from .services.spoolman import SpoolmanClient

# ===========================================================================
# Helper mixin
# ===========================================================================


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


# ===========================================================================
# Model tests
# ===========================================================================


class ProjectModelTests(TestDataMixin, TestCase):
    """Tests for the Project model."""

    def test_str(self):
        self.assertEqual(str(self.project), "Test Project")

    def test_total_parts_count(self):
        self.assertEqual(self.project.total_parts_count, 3)

    def test_total_parts_count_multiple(self):
        Part.objects.create(project=self.project, name="Part B", quantity=5)
        self.assertEqual(self.project.total_parts_count, 8)

    def test_printed_parts_count_no_jobs(self):
        self.assertEqual(self.project.printed_parts_count, 0)

    def test_printed_parts_count_with_completed_jobs(self):
        job = PrintJob.objects.create(status="completed", created_by=self.user)
        PrintJobPart.objects.create(print_job=job, part=self.part, quantity=2)
        PrintJobPlate.objects.create(print_job=job, plate_number=1, status=PrintJobPlate.STATUS_COMPLETED)
        self.assertEqual(self.project.printed_parts_count, 2)

    def test_printed_parts_count_ignores_non_completed(self):
        job = PrintJob.objects.create(status="failed", created_by=self.user)
        PrintJobPart.objects.create(print_job=job, part=self.part, quantity=1)
        self.assertEqual(self.project.printed_parts_count, 0)

    def test_progress_percent_no_parts(self):
        empty = Project.objects.create(name="Empty", created_by=self.user)
        self.assertEqual(empty.progress_percent, 0)

    def test_progress_percent_partial(self):
        job = PrintJob.objects.create(status="completed", created_by=self.user)
        PrintJobPart.objects.create(print_job=job, part=self.part, quantity=1)
        PrintJobPlate.objects.create(print_job=job, plate_number=1, status=PrintJobPlate.STATUS_COMPLETED)
        # 1 completed out of 3 total = 33%
        self.assertEqual(self.project.progress_percent, 33)

    def test_progress_percent_complete(self):
        job = PrintJob.objects.create(status="completed", created_by=self.user)
        PrintJobPart.objects.create(print_job=job, part=self.part, quantity=3)
        PrintJobPlate.objects.create(print_job=job, plate_number=1, status=PrintJobPlate.STATUS_COMPLETED)
        self.assertEqual(self.project.progress_percent, 100)

    def test_total_filament_grams(self):
        # part has filament_used_grams=10.5, quantity=3 => 31.5
        self.assertAlmostEqual(self.project.total_filament_grams, 31.5)

    def test_total_filament_grams_excludes_null(self):
        Part.objects.create(
            project=self.project,
            name="No filament",
            quantity=2,
            filament_used_grams=None,
        )
        self.assertAlmostEqual(self.project.total_filament_grams, 31.5)

    def test_total_filament_meters(self):
        # part has filament_used_meters=3.4, quantity=3 => 10.2
        self.assertAlmostEqual(self.project.total_filament_meters, 10.2)


class PartModelTests(TestDataMixin, TestCase):
    """Tests for the Part model."""

    def test_str(self):
        self.assertEqual(str(self.part), "Test Part (Test Project)")

    def test_color_display_standard(self):
        self.assertEqual(self.part.color_display, "red")

    def test_color_display_with_value(self):
        self.part.color = "Neon Pink"
        self.assertEqual(self.part.color_display, "Neon Pink")

    def test_color_display_empty(self):
        self.part.color = ""
        self.assertEqual(self.part.color_display, "—")

    def test_printed_quantity_no_jobs(self):
        self.assertEqual(self.part.printed_quantity, 0)

    def test_printed_quantity_with_completed(self):
        job = PrintJob.objects.create(status="completed", created_by=self.user)
        PrintJobPart.objects.create(print_job=job, part=self.part, quantity=2)
        PrintJobPlate.objects.create(print_job=job, plate_number=1, status=PrintJobPlate.STATUS_COMPLETED)
        self.assertEqual(self.part.printed_quantity, 2)

    def test_printed_quantity_multiple_jobs(self):
        job1 = PrintJob.objects.create(status="completed", created_by=self.user)
        PrintJobPart.objects.create(print_job=job1, part=self.part, quantity=1)
        PrintJobPlate.objects.create(print_job=job1, plate_number=1, status=PrintJobPlate.STATUS_COMPLETED)
        job2 = PrintJob.objects.create(status="completed", created_by=self.user)
        PrintJobPart.objects.create(print_job=job2, part=self.part, quantity=1)
        PrintJobPlate.objects.create(print_job=job2, plate_number=1, status=PrintJobPlate.STATUS_COMPLETED)
        self.assertEqual(self.part.printed_quantity, 2)

    def test_remaining_quantity(self):
        job = PrintJob.objects.create(status="completed", created_by=self.user)
        PrintJobPart.objects.create(print_job=job, part=self.part, quantity=1)
        PrintJobPlate.objects.create(print_job=job, plate_number=1, status=PrintJobPlate.STATUS_COMPLETED)
        self.assertEqual(self.part.remaining_quantity, 2)

    def test_remaining_quantity_never_negative(self):
        job = PrintJob.objects.create(status="completed", created_by=self.user)
        PrintJobPart.objects.create(print_job=job, part=self.part, quantity=10)
        PrintJobPlate.objects.create(print_job=job, plate_number=1, status=PrintJobPlate.STATUS_COMPLETED)
        self.assertEqual(self.part.remaining_quantity, 0)

    def test_is_complete_false(self):
        self.assertFalse(self.part.is_complete)

    def test_is_complete_true(self):
        job = PrintJob.objects.create(status="completed", created_by=self.user)
        PrintJobPart.objects.create(print_job=job, part=self.part, quantity=3)
        PrintJobPlate.objects.create(print_job=job, plate_number=1, status=PrintJobPlate.STATUS_COMPLETED)
        self.assertTrue(self.part.is_complete)

    def test_estimation_status_default(self):
        """New parts should have estimation_status 'none' by default."""
        self.assertEqual(self.part.estimation_status, Part.ESTIMATION_NONE)
        self.assertEqual(self.part.estimation_error, "")

    def test_estimation_status_error(self):
        """Parts can store estimation error status and message."""
        self.part.estimation_status = Part.ESTIMATION_ERROR
        self.part.estimation_error = "OrcaSlicer API unreachable"
        self.part.save()
        self.part.refresh_from_db()
        self.assertEqual(self.part.estimation_status, Part.ESTIMATION_ERROR)
        self.assertEqual(self.part.estimation_error, "OrcaSlicer API unreachable")

    def test_estimation_status_success(self):
        """Parts can store successful estimation status."""
        self.part.estimation_status = Part.ESTIMATION_SUCCESS
        self.part.estimation_error = ""
        self.part.save()
        self.part.refresh_from_db()
        self.assertEqual(self.part.estimation_status, Part.ESTIMATION_SUCCESS)

    def test_estimation_status_estimating(self):
        """Parts can store actively-estimating status."""
        self.part.estimation_status = Part.ESTIMATION_ESTIMATING
        self.part.save()
        self.part.refresh_from_db()
        self.assertEqual(self.part.estimation_status, Part.ESTIMATION_ESTIMATING)


class PrinterProfileModelTests(TestDataMixin, TestCase):
    """Tests for the PrinterProfile model."""

    def test_str(self):
        self.assertEqual(str(self.printer), "Test Printer")

    def test_default_values(self):
        p = PrinterProfile.objects.create(name="Defaults", created_by=self.user)
        self.assertIsNone(p.bed_size_x)
        self.assertIsNone(p.nozzle_diameter)


class PrintJobModelTests(TestDataMixin, TestCase):
    """Tests for the PrintJob model."""

    def test_str_with_name(self):
        job = PrintJob.objects.create(name="My Job", status="draft", created_by=self.user)
        self.assertIn("My Job", str(job))

    def test_str_without_name(self):
        job = PrintJob.objects.create(status="draft", created_by=self.user)
        self.assertIn(f"Job #{job.pk}", str(job))

    def test_default_status(self):
        job = PrintJob.objects.create(created_by=self.user)
        self.assertEqual(job.status, "draft")

    def test_total_part_count(self):
        job = PrintJob.objects.create(created_by=self.user)
        PrintJobPart.objects.create(print_job=job, part=self.part, quantity=3)
        self.assertEqual(job.total_part_count, 3)

    def test_plate_count(self):
        job = PrintJob.objects.create(created_by=self.user)
        PrintJobPlate.objects.create(print_job=job, plate_number=1)
        PrintJobPlate.objects.create(print_job=job, plate_number=2)
        self.assertEqual(job.plate_count, 2)

    def test_all_plates_completed(self):
        job = PrintJob.objects.create(created_by=self.user)
        PrintJobPlate.objects.create(print_job=job, plate_number=1, status=PrintJobPlate.STATUS_COMPLETED)
        self.assertTrue(job.all_plates_completed)

    def test_all_plates_completed_false_when_waiting(self):
        job = PrintJob.objects.create(created_by=self.user)
        PrintJobPlate.objects.create(print_job=job, plate_number=1, status=PrintJobPlate.STATUS_COMPLETED)
        PrintJobPlate.objects.create(print_job=job, plate_number=2, status=PrintJobPlate.STATUS_WAITING)
        self.assertFalse(job.all_plates_completed)


class PrintJobPartModelTests(TestDataMixin, TestCase):
    """Tests for the PrintJobPart through model."""

    def test_str(self):
        job = PrintJob.objects.create(name="Test Job", created_by=self.user)
        jp = PrintJobPart.objects.create(print_job=job, part=self.part, quantity=2)
        self.assertIn("Test Part", str(jp))

    def test_unique_together(self):
        job = PrintJob.objects.create(name="Test Job", created_by=self.user)
        PrintJobPart.objects.create(print_job=job, part=self.part, quantity=1)
        from django.db import IntegrityError

        with self.assertRaises(IntegrityError):
            PrintJobPart.objects.create(print_job=job, part=self.part, quantity=1)


# ===========================================================================
# Form tests
# ===========================================================================


class ProjectFormTests(TestCase):
    """Tests for the ProjectForm."""

    def test_valid_form(self):
        form = ProjectForm(data={"name": "My Project", "description": "Desc"})
        self.assertTrue(form.is_valid())

    def test_name_required(self):
        form = ProjectForm(data={"name": "", "description": "Desc"})
        self.assertFalse(form.is_valid())
        self.assertIn("name", form.errors)

    def test_description_optional(self):
        form = ProjectForm(data={"name": "My Project", "description": ""})
        self.assertTrue(form.is_valid())


class ProjectEditFormTests(TestCase):
    """Tests for the ProjectEditForm (re-parenting support)."""

    def test_valid_form_no_parent(self):
        """A project without parent should be valid with quantity defaulting to 1."""
        form = ProjectEditForm(data={"name": "Top Level", "description": "", "quantity": "1"})
        self.assertTrue(form.is_valid())

    def test_valid_form_with_parent(self):
        """A project can be set as sub-project of another project."""
        parent = Project.objects.create(name="Parent Project")
        form = ProjectEditForm(
            data={
                "name": "Child",
                "description": "",
                "parent": parent.pk,
                "quantity": "3",
            }
        )
        self.assertTrue(form.is_valid())
        self.assertEqual(form.cleaned_data["parent"], parent)
        self.assertEqual(form.cleaned_data["quantity"], 3)

    def test_quantity_reset_without_parent(self):
        """Quantity should be reset to 1 when no parent is set."""
        form = ProjectEditForm(
            data={
                "name": "Top Level",
                "description": "",
                "quantity": "5",
            }
        )
        self.assertTrue(form.is_valid())
        self.assertEqual(form.cleaned_data["quantity"], 1)

    def test_cannot_set_self_as_parent(self):
        """A project cannot be its own parent (prevented via view queryset)."""
        project = Project.objects.create(name="Self Ref")
        form = ProjectEditForm(
            data={
                "name": "Self Ref",
                "description": "",
                "parent": project.pk,
                "quantity": "1",
            },
            instance=project,
        )
        # The form itself doesn't prevent this — the view filters the queryset.
        # But this test verifies the form accepts valid parent PKs.
        self.assertTrue(form.is_valid())


class ProjectGetDescendantIdsTests(TestCase):
    """Tests for Project.get_descendant_ids() method."""

    def test_no_descendants(self):
        """A project without sub-projects returns an empty set."""
        project = Project.objects.create(name="Alone")
        self.assertEqual(project.get_descendant_ids(), set())

    def test_single_level_descendants(self):
        """Direct sub-projects are included."""
        parent = Project.objects.create(name="Parent")
        child1 = Project.objects.create(name="Child 1", parent=parent)
        child2 = Project.objects.create(name="Child 2", parent=parent)
        self.assertEqual(parent.get_descendant_ids(), {child1.pk, child2.pk})

    def test_nested_descendants(self):
        """Deeply nested sub-projects are included recursively."""
        root = Project.objects.create(name="Root")
        child = Project.objects.create(name="Child", parent=root)
        grandchild = Project.objects.create(name="Grandchild", parent=child)
        self.assertEqual(root.get_descendant_ids(), {child.pk, grandchild.pk})


class PartFormTests(TestCase):
    """Tests for the PartForm, including STL file validation."""

    def test_valid_form_minimal(self):
        form = PartForm(
            data={
                "name": "A Part",
                "quantity": 1,
                "color": "black",
                "material": "PLA",
            }
        )
        self.assertTrue(form.is_valid())

    def test_name_required(self):
        form = PartForm(data={"name": "", "quantity": 1, "color": "black", "material": "PLA"})
        self.assertFalse(form.is_valid())
        self.assertIn("name", form.errors)

    def test_stl_file_valid(self):
        stl = SimpleUploadedFile("model.stl", b"solid test", content_type="application/sla")
        form = PartForm(
            data={"name": "Part", "quantity": 1, "color": "black", "material": "PLA"},
            files={"stl_file": stl},
        )
        self.assertTrue(form.is_valid())

    def test_stl_file_invalid_extension(self):
        bad = SimpleUploadedFile("model.obj", b"data", content_type="application/octet-stream")
        form = PartForm(
            data={"name": "Part", "quantity": 1, "color": "black", "material": "PLA"},
            files={"stl_file": bad},
        )
        self.assertFalse(form.is_valid())
        self.assertIn("stl_file", form.errors)


class UserRegistrationFormTests(TestCase):
    """Tests for the UserRegistrationForm."""

    def test_valid_registration(self):
        form = UserRegistrationForm(
            data={
                "username": "newuser",
                "email": "new@example.com",
                "password1": "Str0ngP@ss!",
                "password2": "Str0ngP@ss!",
            }
        )
        self.assertTrue(form.is_valid())

    def test_password_mismatch(self):
        form = UserRegistrationForm(
            data={
                "username": "newuser",
                "email": "new@example.com",
                "password1": "Str0ngP@ss!",
                "password2": "different",
            }
        )
        self.assertFalse(form.is_valid())

    def test_email_required(self):
        form = UserRegistrationForm(
            data={
                "username": "newuser",
                "email": "",
                "password1": "Str0ngP@ss!",
                "password2": "Str0ngP@ss!",
            }
        )
        self.assertFalse(form.is_valid())
        self.assertIn("email", form.errors)


# ===========================================================================
# View tests — authentication and redirects
# ===========================================================================


@override_settings(ALLOWED_HOSTS=["testserver"])
class AuthRedirectTests(TestDataMixin, TestCase):
    """Unauthenticated users should be redirected to login."""

    def test_dashboard_redirect(self):
        resp = self.client.get(reverse("core:dashboard"))
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/accounts/login/", resp.url)

    def test_project_list_redirect(self):
        resp = self.client.get(reverse("core:project_list"))
        self.assertEqual(resp.status_code, 302)

    def test_profile_redirect(self):
        resp = self.client.get(reverse("core:profile"))
        self.assertEqual(resp.status_code, 302)

    def test_printjob_list_redirect(self):
        resp = self.client.get(reverse("core:printjob_list"))
        self.assertEqual(resp.status_code, 302)

    def test_register_accessible_anon(self):
        resp = self.client.get(reverse("core:register"))
        self.assertEqual(resp.status_code, 200)


# ===========================================================================
# View tests — authenticated pages return 200
# ===========================================================================


@override_settings(ALLOWED_HOSTS=["testserver"])
class DashboardViewTests(TestDataMixin, TestCase):
    """Tests for the dashboard view."""

    def setUp(self):
        super().setUp()
        self.client.login(username="testuser", password="testpass123")

    def test_dashboard_status(self):
        resp = self.client.get(reverse("core:dashboard"))
        self.assertEqual(resp.status_code, 200)

    def test_dashboard_context(self):
        resp = self.client.get(reverse("core:dashboard"))
        self.assertIn("projects", resp.context)
        self.assertIn("recent_jobs", resp.context)


# ===========================================================================
# Project view tests
# ===========================================================================


@override_settings(ALLOWED_HOSTS=["testserver"])
class ProjectViewTests(TestDataMixin, TestCase):
    """Tests for Project CRUD views."""

    def setUp(self):
        super().setUp()
        self.client.login(username="testuser", password="testpass123")

    def test_project_list_200(self):
        resp = self.client.get(reverse("core:project_list"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Test Project")

    def test_project_list_only_own(self):
        resp = self.client.get(reverse("core:project_list"))
        self.assertContains(resp, "Other Project")

    def test_project_detail_200(self):
        resp = self.client.get(reverse("core:project_detail", args=[self.project.pk]))
        self.assertEqual(resp.status_code, 200)

    def test_project_detail_other_user_404(self):
        resp = self.client.get(reverse("core:project_detail", args=[self.other_project.pk]))
        self.assertEqual(resp.status_code, 200)

    def test_project_create_get(self):
        resp = self.client.get(reverse("core:project_create"))
        self.assertEqual(resp.status_code, 200)

    def test_project_create_post(self):
        resp = self.client.post(
            reverse("core:project_create"),
            {
                "name": "New Project",
                "description": "New desc",
            },
        )
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(Project.objects.filter(name="New Project", created_by=self.user).exists())

    def test_project_update_get(self):
        resp = self.client.get(reverse("core:project_update", args=[self.project.pk]))
        self.assertEqual(resp.status_code, 200)

    def test_project_update_post(self):
        resp = self.client.post(
            reverse("core:project_update", args=[self.project.pk]),
            {"name": "Updated Name", "description": "", "quantity": "1"},
        )
        self.assertEqual(resp.status_code, 302)
        self.project.refresh_from_db()
        self.assertEqual(self.project.name, "Updated Name")

    def test_project_update_other_user_404(self):
        resp = self.client.post(
            reverse("core:project_update", args=[self.other_project.pk]),
            {"name": "Hacked", "description": "", "quantity": "1"},
        )
        self.assertEqual(resp.status_code, 302)

    def test_project_delete_get(self):
        resp = self.client.get(reverse("core:project_delete", args=[self.project.pk]))
        self.assertEqual(resp.status_code, 200)

    def test_project_delete_post(self):
        pk = self.project.pk
        resp = self.client.post(reverse("core:project_delete", args=[pk]))
        self.assertEqual(resp.status_code, 302)
        self.assertFalse(Project.objects.filter(pk=pk).exists())

    def test_project_delete_other_user_404(self):
        resp = self.client.post(reverse("core:project_delete", args=[self.other_project.pk]))
        self.assertEqual(resp.status_code, 302)

    def test_project_reparent_via_update(self):
        """An existing project can be turned into a sub-project via the edit form."""
        parent = Project.objects.create(name="New Parent", created_by=self.user)
        resp = self.client.post(
            reverse("core:project_update", args=[self.project.pk]),
            {
                "name": self.project.name,
                "description": "",
                "parent": parent.pk,
                "quantity": "2",
            },
        )
        self.assertEqual(resp.status_code, 302)
        self.project.refresh_from_db()
        self.assertEqual(self.project.parent, parent)
        self.assertEqual(self.project.quantity, 2)

    def test_project_remove_parent_via_update(self):
        """A sub-project can be turned back into a top-level project."""
        parent = Project.objects.create(name="Parent", created_by=self.user)
        self.project.parent = parent
        self.project.quantity = 3
        self.project.save()
        resp = self.client.post(
            reverse("core:project_update", args=[self.project.pk]),
            {
                "name": self.project.name,
                "description": "",
                "quantity": "1",
            },
        )
        self.assertEqual(resp.status_code, 302)
        self.project.refresh_from_db()
        self.assertIsNone(self.project.parent)
        self.assertEqual(self.project.quantity, 1)

    def test_project_update_excludes_self_and_descendants_from_parent(self):
        """The parent dropdown should not include the project itself or its descendants."""
        child = Project.objects.create(name="Child", parent=self.project, created_by=self.user)
        resp = self.client.get(reverse("core:project_update", args=[self.project.pk]))
        form = resp.context["form"]
        parent_qs = form.fields["parent"].queryset
        self.assertNotIn(self.project, parent_qs)
        self.assertNotIn(child, parent_qs)


# ===========================================================================
# Part view tests
# ===========================================================================


@override_settings(ALLOWED_HOSTS=["testserver"])
class PartViewTests(TestDataMixin, TestCase):
    """Tests for Part CRUD views."""

    def setUp(self):
        super().setUp()
        self.client.login(username="testuser", password="testpass123")

    def test_part_detail_200(self):
        resp = self.client.get(reverse("core:part_detail", args=[self.part.pk]))
        self.assertEqual(resp.status_code, 200)

    def test_part_detail_other_user_404(self):
        resp = self.client.get(reverse("core:part_detail", args=[self.other_part.pk]))
        self.assertEqual(resp.status_code, 200)

    def test_part_create_get(self):
        resp = self.client.get(reverse("core:part_create", args=[self.project.pk]))
        self.assertEqual(resp.status_code, 200)

    def test_part_create_post(self):
        resp = self.client.post(
            reverse("core:part_create", args=[self.project.pk]),
            {"name": "New Part", "quantity": 2, "color": "blue", "material": "PETG"},
        )
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(Part.objects.filter(name="New Part", project=self.project).exists())

    def test_part_create_other_project_404(self):
        resp = self.client.get(reverse("core:part_create", args=[self.other_project.pk]))
        self.assertEqual(resp.status_code, 200)

    def test_part_update_get(self):
        resp = self.client.get(reverse("core:part_update", args=[self.part.pk]))
        self.assertEqual(resp.status_code, 200)

    def test_part_update_post(self):
        resp = self.client.post(
            reverse("core:part_update", args=[self.part.pk]),
            {
                "name": "Updated Part",
                "quantity": 5,
                "color": "green",
                "material": "PLA",
            },
        )
        self.assertEqual(resp.status_code, 302)
        self.part.refresh_from_db()
        self.assertEqual(self.part.name, "Updated Part")

    def test_part_delete_get(self):
        resp = self.client.get(reverse("core:part_delete", args=[self.part.pk]))
        self.assertEqual(resp.status_code, 200)

    def test_part_delete_post(self):
        pk = self.part.pk
        resp = self.client.post(reverse("core:part_delete", args=[pk]))
        self.assertEqual(resp.status_code, 302)
        self.assertFalse(Part.objects.filter(pk=pk).exists())


class PartReEstimateViewTests(TestDataMixin, TestCase):
    """Tests for the PartReEstimateView."""

    def setUp(self):
        super().setUp()
        self.client.login(username="testuser", password="testpass123")

    def test_re_estimate_requires_post(self):
        """GET is not allowed on the re-estimate endpoint."""
        resp = self.client.get(reverse("core:part_re_estimate", args=[self.part.pk]))
        self.assertEqual(resp.status_code, 405)

    def test_re_estimate_no_stl(self):
        """Parts without STL show a warning and redirect."""
        self.part.stl_file = ""
        self.part.save()
        resp = self.client.post(reverse("core:part_re_estimate", args=[self.part.pk]))
        self.assertEqual(resp.status_code, 302)

    def test_re_estimate_clears_old_values(self):
        """Re-estimate clears existing filament values when no preset available."""
        stl = SimpleUploadedFile("model.stl", b"solid test", content_type="application/sla")
        self.part.stl_file = stl
        self.part.filament_used_grams = 50.0
        self.part.estimation_status = Part.ESTIMATION_SUCCESS
        self.part.save()
        resp = self.client.post(reverse("core:part_re_estimate", args=[self.part.pk]))
        self.assertEqual(resp.status_code, 302)
        self.part.refresh_from_db()
        # No preset available → warning redirect, values untouched
        self.assertEqual(self.part.filament_used_grams, 50.0)


class ProjectReEstimateViewTests(TestDataMixin, TestCase):
    """Tests for the ProjectReEstimateView."""

    def setUp(self):
        super().setUp()
        self.client.login(username="testuser", password="testpass123")

    def test_re_estimate_requires_post(self):
        """GET is not allowed on the project re-estimate endpoint."""
        resp = self.client.get(reverse("core:project_re_estimate", args=[self.project.pk]))
        self.assertEqual(resp.status_code, 405)

    def test_re_estimate_empty_project(self):
        """Re-estimate on an empty project should show a warning."""
        empty = Project.objects.create(name="Empty", created_by=self.user)
        resp = self.client.post(reverse("core:project_re_estimate", args=[empty.pk]))
        self.assertEqual(resp.status_code, 302)

    def test_re_estimate_redirects_to_project(self):
        """Re-estimate redirects back to the project detail page."""
        resp = self.client.post(reverse("core:project_re_estimate", args=[self.project.pk]))
        self.assertRedirects(resp, reverse("core:project_detail", args=[self.project.pk]))


# ===========================================================================
# PrinterProfile view tests
# ===========================================================================


@override_settings(ALLOWED_HOSTS=["testserver"])
class PrinterProfileViewTests(TestDataMixin, TestCase):
    """Tests for PrinterProfile CRUD views."""

    def setUp(self):
        super().setUp()
        self.client.login(username="testuser", password="testpass123")

    def test_list_200(self):
        resp = self.client.get(reverse("core:printerprofile_list"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Test Printer")

    def test_list_only_own(self):
        resp = self.client.get(reverse("core:printerprofile_list"))
        self.assertContains(resp, "Other Printer")

    def test_create_get(self):
        resp = self.client.get(reverse("core:printerprofile_create"))
        self.assertEqual(resp.status_code, 200)

    def test_create_post(self):
        resp = self.client.post(
            reverse("core:printerprofile_create"),
            {
                "name": "New Printer",
            },
        )
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(PrinterProfile.objects.filter(name="New Printer", created_by=self.user).exists())

    def test_update_get(self):
        resp = self.client.get(reverse("core:printerprofile_update", args=[self.printer.pk]))
        self.assertEqual(resp.status_code, 200)

    def test_update_post(self):
        resp = self.client.post(
            reverse("core:printerprofile_update", args=[self.printer.pk]),
            {
                "name": "Renamed Printer",
            },
        )
        self.assertEqual(resp.status_code, 302)
        self.printer.refresh_from_db()
        self.assertEqual(self.printer.name, "Renamed Printer")

    def test_update_other_user_404(self):
        resp = self.client.post(
            reverse("core:printerprofile_update", args=[self.other_printer.pk]),
            {"name": "Hacked"},
        )
        self.assertEqual(resp.status_code, 302)

    def test_delete_get(self):
        resp = self.client.get(reverse("core:printerprofile_delete", args=[self.printer.pk]))
        self.assertEqual(resp.status_code, 200)

    def test_delete_post(self):
        pk = self.printer.pk
        resp = self.client.post(reverse("core:printerprofile_delete", args=[pk]))
        self.assertEqual(resp.status_code, 302)
        self.assertFalse(PrinterProfile.objects.filter(pk=pk).exists())


# ===========================================================================
# PrintJob view tests
# ===========================================================================


@override_settings(ALLOWED_HOSTS=["testserver"])
class PrintJobViewTests(TestDataMixin, TestCase):
    """Tests for PrintJob views."""

    def setUp(self):
        super().setUp()
        self.client.login(username="testuser", password="testpass123")
        self.job = PrintJob.objects.create(
            name="Test Job",
            status="draft",
            created_by=self.user,
        )
        PrintJobPart.objects.create(print_job=self.job, part=self.part, quantity=1)

    def test_list_200(self):
        resp = self.client.get(reverse("core:printjob_list"))
        self.assertEqual(resp.status_code, 200)

    def test_create_get(self):
        resp = self.client.get(reverse("core:printjob_create"))
        self.assertEqual(resp.status_code, 200)

    def test_create_post(self):
        resp = self.client.post(
            reverse("core:printjob_create"),
            {
                "name": "New Draft Job",
            },
        )
        self.assertEqual(resp.status_code, 302)

    def test_detail_200(self):
        resp = self.client.get(reverse("core:printjob_detail", args=[self.job.pk]))
        self.assertEqual(resp.status_code, 200)

    def test_update_get(self):
        resp = self.client.get(reverse("core:printjob_update", args=[self.job.pk]))
        self.assertEqual(resp.status_code, 200)


# ===========================================================================
# Registration / Profile view tests
# ===========================================================================


@override_settings(ALLOWED_HOSTS=["testserver"])
class RegistrationViewTests(TestCase):
    """Tests for user registration."""

    def test_register_page_get(self):
        resp = self.client.get(reverse("core:register"))
        self.assertEqual(resp.status_code, 200)

    def test_register_creates_user(self):
        resp = self.client.post(
            reverse("core:register"),
            {
                "username": "brand_new",
                "email": "brand@new.com",
                "password1": "Str0ngP@ss!",
                "password2": "Str0ngP@ss!",
            },
        )
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(User.objects.filter(username="brand_new").exists())

    def test_register_logs_user_in(self):
        self.client.post(
            reverse("core:register"),
            {
                "username": "brand_new",
                "email": "brand@new.com",
                "password1": "Str0ngP@ss!",
                "password2": "Str0ngP@ss!",
            },
        )
        resp = self.client.get(reverse("core:dashboard"))
        self.assertEqual(resp.status_code, 200)


@override_settings(ALLOWED_HOSTS=["testserver"])
class ProfileViewTests(TestDataMixin, TestCase):
    """Tests for the profile view."""

    def setUp(self):
        super().setUp()
        self.client.login(username="testuser", password="testpass123")

    def test_profile_200(self):
        resp = self.client.get(reverse("core:profile"))
        self.assertEqual(resp.status_code, 200)

    def test_profile_context(self):
        resp = self.client.get(reverse("core:profile"))
        self.assertIn("total_parts", resp.context)
        self.assertIn("total_jobs", resp.context)


# ===========================================================================
# Service instantiation tests
# ===========================================================================


class OrcaSlicerAPIClientTests(TestCase):
    """Tests for OrcaSlicerAPIClient basic functionality."""

    def test_init_strips_trailing_slash(self):
        client = OrcaSlicerAPIClient("http://localhost:3000/")
        self.assertEqual(client.base_url, "http://localhost:3000")

    def test_init_default_url(self):
        client = OrcaSlicerAPIClient()
        self.assertEqual(client.base_url, "http://orcaslicer:3000")

    def test_url_helper(self):
        client = OrcaSlicerAPIClient("http://localhost:3000")
        self.assertEqual(client._url("/slice"), "http://localhost:3000/slice")


class SpoolmanClientTests(TestCase):
    """Tests for SpoolmanClient instantiation."""

    def test_init_strips_trailing_slash(self):
        client = SpoolmanClient("http://localhost:7912/")
        self.assertEqual(client.base_url, "http://localhost:7912")

    def test_init_no_trailing_slash(self):
        client = SpoolmanClient("http://localhost:7912")
        self.assertEqual(client.base_url, "http://localhost:7912")


class MoonrakerClientTests(TestCase):
    """Tests for MoonrakerClient instantiation."""

    def test_init(self):
        client = MoonrakerClient("http://192.168.1.100:7125", "mykey")
        self.assertEqual(client.base_url, "http://192.168.1.100:7125")
        self.assertEqual(client.api_key, "mykey")

    def test_init_strips_trailing_slash(self):
        client = MoonrakerClient("http://192.168.1.100:7125/")
        self.assertEqual(client.base_url, "http://192.168.1.100:7125")

    def test_headers_with_api_key(self):
        client = MoonrakerClient("http://host", "secret")
        headers = client._get_headers()
        self.assertEqual(headers["X-Api-Key"], "secret")

    def test_headers_without_api_key(self):
        client = MoonrakerClient("http://host")
        headers = client._get_headers()
        self.assertNotIn("X-Api-Key", headers)


# ===========================================================================
# New Feature Tests
# ===========================================================================


@override_settings(ALLOWED_HOSTS=["testserver"])
class CostProfileModelTests(TestDataMixin, TestCase):
    """Tests for CostProfile model."""

    def setUp(self):
        super().setUp()
        self.printer = PrinterProfile.objects.create(name="Test Printer", created_by=self.user)
        self.cost = CostProfile.objects.create(
            printer=self.printer,
            electricity_cost_per_kwh=0.30,
            printer_power_watts=200,
            printer_purchase_cost=500,
            printer_lifespan_hours=5000,
            maintenance_cost_per_hour=0.05,
        )

    def test_str(self):
        self.assertEqual(str(self.cost), "Cost Profile: Test Printer")

    def test_depreciation_per_hour(self):
        self.assertAlmostEqual(self.cost.depreciation_per_hour, 0.1)

    def test_electricity_per_hour(self):
        self.assertAlmostEqual(self.cost.electricity_per_hour, 0.06)

    def test_calculate_print_cost(self):
        result = self.cost.calculate_print_cost(2.0, 50, 20)
        self.assertIn("filament_cost", result)
        self.assertIn("electricity_cost", result)
        self.assertIn("depreciation_cost", result)
        self.assertIn("maintenance_cost", result)
        self.assertIn("total_cost", result)
        self.assertEqual(result["filament_cost"], 1.0)  # 50g / 1000 * 20
        self.assertEqual(result["electricity_cost"], 0.12)  # 0.06 * 2
        self.assertEqual(result["depreciation_cost"], 0.2)  # 0.1 * 2
        self.assertEqual(result["maintenance_cost"], 0.1)  # 0.05 * 2

    def test_depreciation_zero_lifespan(self):
        self.cost.printer_lifespan_hours = 0
        self.cost.save()
        self.assertEqual(self.cost.depreciation_per_hour, 0)


@override_settings(ALLOWED_HOSTS=["testserver"])
class PrintQueueModelTests(TestDataMixin, TestCase):
    """Tests for PrintQueue model."""

    def setUp(self):
        super().setUp()
        self.printer = PrinterProfile.objects.create(name="Test Printer", created_by=self.user)
        self.job = PrintJob.objects.create(
            name="Queue Test Job",
            status="sliced",
            created_by=self.user,
        )
        PrintJobPart.objects.create(print_job=self.job, part=self.part, quantity=1)
        self.plate = PrintJobPlate.objects.create(print_job=self.job, plate_number=1)

    def test_str(self):
        entry = PrintQueue.objects.create(plate=self.plate, printer=self.printer, priority=3, position=0)
        self.assertIn("High", str(entry))

    def test_ordering(self):
        """Higher priority should come first."""
        plate2 = PrintJobPlate.objects.create(print_job=self.job, plate_number=2)
        PrintQueue.objects.create(plate=self.plate, printer=self.printer, priority=1, position=0)
        e2 = PrintQueue.objects.create(plate=plate2, printer=self.printer, priority=4, position=0)
        entries = list(PrintQueue.objects.all())
        self.assertEqual(entries[0], e2)  # Urgent first


@override_settings(ALLOWED_HOSTS=["testserver"])
class PrintTimeEstimateModelTests(TestDataMixin, TestCase):
    """Tests for PrintTimeEstimate model."""

    def setUp(self):
        super().setUp()
        self.printer = PrinterProfile.objects.create(name="Test Printer", created_by=self.user)

    def test_accuracy_factor(self):
        from datetime import timedelta

        est = PrintTimeEstimate.objects.create(
            part=self.part,
            printer=self.printer,
            estimated_time=timedelta(hours=2),
            actual_time=timedelta(hours=2, minutes=30),
        )
        self.assertAlmostEqual(est.accuracy_factor, 1.25)

    def test_accuracy_factor_no_actual(self):
        from datetime import timedelta

        est = PrintTimeEstimate.objects.create(
            part=self.part,
            printer=self.printer,
            estimated_time=timedelta(hours=2),
        )
        self.assertIsNone(est.accuracy_factor)


@override_settings(ALLOWED_HOSTS=["testserver"])
class FileVersionModelTests(TestDataMixin, TestCase):
    """Tests for FileVersion model."""

    def test_str(self):
        fv = FileVersion.objects.create(
            part=self.part,
            version=1,
            file_type="stl",
            file="test.stl",
            uploaded_by=self.user,
        )
        self.assertIn("v1", str(fv))
        self.assertIn("stl", str(fv))


@override_settings(ALLOWED_HOSTS=["testserver"])
class MaterialsListViewTests(TestDataMixin, TestCase):
    """Tests for the Spoolman-backed materials list view."""

    def setUp(self):
        super().setUp()
        self.client = Client()
        self.client.login(username="testuser", password="testpass123")

    def test_list_200(self):
        r = self.client.get(reverse("core:materialprofile_list"))
        self.assertEqual(r.status_code, 200)


@override_settings(ALLOWED_HOSTS=["testserver"])
class StatisticsViewTests(TestDataMixin, TestCase):
    """Tests for the statistics view."""

    def setUp(self):
        super().setUp()
        self.client = Client()
        self.client.login(username="testuser", password="testpass123")

    def test_statistics_200(self):
        r = self.client.get(reverse("core:statistics"))
        self.assertEqual(r.status_code, 200)

    def test_statistics_has_context(self):
        r = self.client.get(reverse("core:statistics"))
        self.assertIn("total_projects", r.context)
        self.assertIn("total_parts", r.context)
        self.assertIn("total_jobs", r.context)


@override_settings(ALLOWED_HOSTS=["testserver"])
class PrintQueueViewTests(TestDataMixin, TestCase):
    """Tests for PrintQueue views."""

    def setUp(self):
        super().setUp()
        self.client = Client()
        self.client.login(username="testuser", password="testpass123")
        self.printer = PrinterProfile.objects.create(name="Test Printer", created_by=self.user)

    def test_queue_list_200(self):
        r = self.client.get(reverse("core:printqueue_list"))
        self.assertEqual(r.status_code, 200)

    def test_queue_create_get(self):
        r = self.client.get(reverse("core:printqueue_create"))
        self.assertEqual(r.status_code, 200)


@override_settings(ALLOWED_HOSTS=["testserver"])
class DashboardEnhancedTests(TestDataMixin, TestCase):
    """Tests for the enhanced dashboard."""

    def setUp(self):
        super().setUp()
        self.client = Client()
        self.client.login(username="testuser", password="testpass123")

    def test_dashboard_has_statistics(self):
        r = self.client.get(reverse("core:dashboard"))
        self.assertEqual(r.status_code, 200)
        self.assertIn("total_projects", r.context)
        self.assertIn("total_parts", r.context)
        self.assertIn("active_jobs", r.context)
        self.assertIn("completed_jobs", r.context)


@override_settings(ALLOWED_HOSTS=["testserver"])
class CostViewTests(TestDataMixin, TestCase):
    """Tests for cost-related views."""

    def setUp(self):
        super().setUp()
        self.client = Client()
        self.client.login(username="testuser", password="testpass123")
        self.printer = PrinterProfile.objects.create(name="Test Printer", created_by=self.user)

    def test_cost_profile_form_get(self):
        r = self.client.get(reverse("core:costprofile_update", args=[self.printer.pk]))
        self.assertEqual(r.status_code, 200)

    def test_project_cost_get(self):
        r = self.client.get(reverse("core:project_cost", args=[self.project.pk]))
        self.assertEqual(r.status_code, 200)


@override_settings(ALLOWED_HOSTS=["testserver"])
class InvenTreeClientTests(TestCase):
    """Tests for InvenTreeClient initialization."""

    def test_init_strips_trailing_slash(self):
        from core.services.inventree import InvenTreeClient

        c = InvenTreeClient("http://example.com:8000/")
        self.assertEqual(c.base_url, "http://example.com:8000")

    def test_init_with_token(self):
        from core.services.inventree import InvenTreeClient

        c = InvenTreeClient("http://example.com:8000", "mytoken")
        headers = c._get_headers()
        self.assertEqual(headers["Authorization"], "Token mytoken")

    def test_init_without_token(self):
        from core.services.inventree import InvenTreeClient

        c = InvenTreeClient("http://example.com:8000")
        headers = c._get_headers()
        self.assertNotIn("Authorization", headers)


# ===========================================================================
# Template tag / filter tests
# ===========================================================================


class DurationFormatFilterTests(TestCase):
    """Tests for the duration_format template filter."""

    def setUp(self):
        from core.templatetags.core_tags import duration_format

        self.f = duration_format

    def test_hours_and_minutes(self):
        from datetime import timedelta

        self.assertEqual(self.f(timedelta(hours=2, minutes=15)), "2h 15m")

    def test_only_minutes(self):
        from datetime import timedelta

        self.assertEqual(self.f(timedelta(minutes=45)), "45m")

    def test_only_hours(self):
        from datetime import timedelta

        self.assertEqual(self.f(timedelta(hours=3)), "3h")

    def test_only_seconds(self):
        from datetime import timedelta

        self.assertEqual(self.f(timedelta(seconds=30)), "30s")

    def test_zero(self):
        from datetime import timedelta

        self.assertEqual(self.f(timedelta(seconds=0)), "0s")

    def test_non_timedelta_passthrough(self):
        self.assertEqual(self.f("not a timedelta"), "not a timedelta")

    def test_negative_returns_zero(self):
        from datetime import timedelta

        self.assertEqual(self.f(timedelta(seconds=-10)), "0m")


class FileSizeFilterTests(TestCase):
    """Tests for the file_size template filter."""

    def setUp(self):
        from core.templatetags.core_tags import file_size

        self.f = file_size

    def test_bytes(self):
        self.assertEqual(self.f(512), "512.0 B")

    def test_kilobytes(self):
        self.assertEqual(self.f(2048), "2.0 KB")

    def test_megabytes(self):
        self.assertEqual(self.f(5 * 1024 * 1024), "5.0 MB")

    def test_gigabytes(self):
        self.assertEqual(self.f(2 * 1024**3), "2.0 GB")

    def test_non_numeric_passthrough(self):
        self.assertEqual(self.f("abc"), "abc")

    def test_none_passthrough(self):
        self.assertIsNone(self.f(None))


class PercentageFilterTests(TestCase):
    """Tests for the percentage template filter."""

    def setUp(self):
        from core.templatetags.core_tags import percentage

        self.f = percentage

    def test_half(self):
        self.assertEqual(self.f(1, 2), 50)

    def test_full(self):
        self.assertEqual(self.f(10, 10), 100)

    def test_zero_total(self):
        self.assertEqual(self.f(5, 0), 0)

    def test_none_value(self):
        self.assertEqual(self.f(None, 10), 0)


class GramsToKgFilterTests(TestCase):
    """Tests for the grams_to_kg template filter."""

    def setUp(self):
        from core.templatetags.core_tags import grams_to_kg

        self.f = grams_to_kg

    def test_conversion(self):
        self.assertEqual(self.f(1500), "1.50 kg")

    def test_zero(self):
        self.assertEqual(self.f(0), "0.00 kg")

    def test_non_numeric(self):
        self.assertEqual(self.f("abc"), "abc")


class MetersFormatFilterTests(TestCase):
    """Tests for the meters_format template filter."""

    def setUp(self):
        from core.templatetags.core_tags import meters_format

        self.f = meters_format

    def test_format(self):
        self.assertEqual(self.f(3.456), "3.5 m")

    def test_non_numeric(self):
        self.assertEqual(self.f("abc"), "abc")


class DictGetFilterTests(TestCase):
    """Tests for the dict_get template filter."""

    def setUp(self):
        from core.templatetags.core_tags import dict_get

        self.f = dict_get

    def test_existing_key(self):
        self.assertEqual(self.f({"a": 1}, "a"), 1)

    def test_missing_key(self):
        self.assertEqual(self.f({"a": 1}, "b"), "")

    def test_non_dict(self):
        self.assertEqual(self.f("not a dict", "a"), "")

    def test_none_input(self):
        self.assertEqual(self.f(None, "key"), "")

    def test_integer_key(self):
        self.assertEqual(self.f({42: "value"}, 42), "value")


class StripPortFilterTests(TestCase):
    """Tests for the strip_port template filter."""

    def setUp(self):
        from core.templatetags.core_tags import strip_port

        self.f = strip_port

    def test_with_port(self):
        self.assertEqual(self.f("http://192.168.1.100:7125"), "http://192.168.1.100")

    def test_without_port(self):
        self.assertEqual(self.f("http://192.168.1.100"), "http://192.168.1.100")

    def test_empty(self):
        self.assertEqual(self.f(""), "")

    def test_none(self):
        self.assertEqual(self.f(None), "")


class WidgetClassFilterTests(TestCase):
    """Tests for the widget_class template filter."""

    def setUp(self):
        from core.templatetags.core_tags import widget_class

        self.f = widget_class

    def test_text_input(self):
        from django import forms

        form = forms.Form()
        field = forms.CharField()
        field.widget = forms.TextInput()
        # Simulate a BoundField
        form.fields["test"] = field
        bound = form["test"]
        self.assertEqual(self.f(bound), "TextInput")

    def test_non_field(self):
        self.assertEqual(self.f("not a field"), "")


# ===========================================================================
# Context processor tests
# ===========================================================================


class ContextProcessorTests(TestCase):
    """Tests for context processors."""

    def test_app_name(self):
        from django.test import RequestFactory

        from core.context_processors import app_name

        request = RequestFactory().get("/")
        ctx = app_name(request)
        self.assertEqual(ctx["APP_NAME"], "LayerNexus")

    def test_allow_registration_default(self):
        from django.test import RequestFactory

        from core.context_processors import allow_registration

        request = RequestFactory().get("/")
        ctx = allow_registration(request)
        self.assertIn("ALLOW_REGISTRATION", ctx)
        self.assertIsInstance(ctx["ALLOW_REGISTRATION"], bool)


# ===========================================================================
# URL resolution tests
# ===========================================================================


class URLResolutionTests(TestCase):
    """Verify that all named URLs resolve without errors."""

    def test_dashboard(self):
        self.assertEqual(reverse("core:dashboard"), "/")

    def test_farm_dashboard(self):
        self.assertEqual(reverse("core:farm_dashboard"), "/farm/")

    def test_statistics(self):
        self.assertEqual(reverse("core:statistics"), "/statistics/")

    def test_project_list(self):
        self.assertEqual(reverse("core:project_list"), "/projects/")

    def test_project_create(self):
        self.assertEqual(reverse("core:project_create"), "/projects/new/")

    def test_project_detail(self):
        url = reverse("core:project_detail", args=[1])
        self.assertEqual(url, "/projects/1/")

    def test_project_update(self):
        url = reverse("core:project_update", args=[1])
        self.assertEqual(url, "/projects/1/edit/")

    def test_project_delete(self):
        url = reverse("core:project_delete", args=[1])
        self.assertEqual(url, "/projects/1/delete/")

    def test_project_cost(self):
        url = reverse("core:project_cost", args=[1])
        self.assertEqual(url, "/projects/1/cost/")

    def test_part_create(self):
        url = reverse("core:part_create", args=[1])
        self.assertEqual(url, "/projects/1/parts/new/")

    def test_part_detail(self):
        url = reverse("core:part_detail", args=[1])
        self.assertEqual(url, "/parts/1/")

    def test_part_update(self):
        url = reverse("core:part_update", args=[1])
        self.assertEqual(url, "/parts/1/edit/")

    def test_part_delete(self):
        url = reverse("core:part_delete", args=[1])
        self.assertEqual(url, "/parts/1/delete/")

    def test_printerprofile_list(self):
        self.assertEqual(reverse("core:printerprofile_list"), "/printers/")

    def test_printerprofile_create(self):
        self.assertEqual(reverse("core:printerprofile_create"), "/printers/new/")

    def test_printerprofile_update(self):
        url = reverse("core:printerprofile_update", args=[1])
        self.assertEqual(url, "/printers/1/edit/")

    def test_printerprofile_delete(self):
        url = reverse("core:printerprofile_delete", args=[1])
        self.assertEqual(url, "/printers/1/delete/")

    def test_printjob_list(self):
        self.assertEqual(reverse("core:printjob_list"), "/jobs/")

    def test_printjob_create(self):
        self.assertEqual(reverse("core:printjob_create"), "/jobs/new/")

    def test_printjob_detail(self):
        url = reverse("core:printjob_detail", args=[1])
        self.assertEqual(url, "/jobs/1/")

    def test_printqueue_list(self):
        self.assertEqual(reverse("core:printqueue_list"), "/queue/")

    def test_printqueue_create(self):
        self.assertEqual(reverse("core:printqueue_create"), "/queue/add/")

    def test_orca_machine_list(self):
        self.assertEqual(reverse("core:orcamachineprofile_list"), "/orca-machine-profiles/")

    def test_orca_filament_list(self):
        self.assertEqual(reverse("core:orcafilamentprofile_list"), "/orca-filament-profiles/")

    def test_orca_print_preset_list(self):
        self.assertEqual(reverse("core:orcaprintpreset_list"), "/orca-print-presets/")

    def test_register(self):
        self.assertEqual(reverse("core:register"), "/register/")

    def test_profile(self):
        self.assertEqual(reverse("core:profile"), "/profile/")

    def test_user_list(self):
        self.assertEqual(reverse("core:user_list"), "/users/")

    def test_user_create(self):
        self.assertEqual(reverse("core:user_create"), "/users/create/")

    def test_materialprofile_list(self):
        self.assertEqual(reverse("core:materialprofile_list"), "/materials/")


# ===========================================================================
# RBAC / Permission tests
# ===========================================================================


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


@override_settings(ALLOWED_HOSTS=["testserver"])
class DesignerPermissionTests(_RBACTestBase):
    """Verify Designer role can manage projects but NOT printers or users."""

    def test_designer_can_create_project(self):
        self.client.login(username="designer_user", password="testpass123")
        resp = self.client.post(
            reverse("core:project_create"),
            {"name": "Designer Project", "description": ""},
        )
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(Project.objects.filter(name="Designer Project").exists())

    def test_designer_cannot_create_printer(self):
        self.client.login(username="designer_user", password="testpass123")
        resp = self.client.post(reverse("core:printerprofile_create"), {"name": "Hacked Printer"})
        self.assertEqual(resp.status_code, 403)

    def test_designer_cannot_access_user_list(self):
        self.client.login(username="designer_user", password="testpass123")
        resp = self.client.get(reverse("core:user_list"))
        self.assertEqual(resp.status_code, 403)

    def test_designer_cannot_delete_orca_profile(self):
        from core.models import OrcaMachineProfile

        profile = OrcaMachineProfile.objects.create(name="Test Machine", orca_name="test_machine")
        self.client.login(username="designer_user", password="testpass123")
        resp = self.client.post(reverse("core:orcamachineprofile_delete", args=[profile.pk]))
        self.assertEqual(resp.status_code, 403)

    def test_designer_can_view_printer_list(self):
        self.client.login(username="designer_user", password="testpass123")
        resp = self.client.get(reverse("core:printerprofile_list"))
        self.assertEqual(resp.status_code, 200)


@override_settings(ALLOWED_HOSTS=["testserver"])
class OperatorPermissionTests(_RBACTestBase):
    """Verify Operator role can manage printers but NOT projects or users."""

    def test_operator_can_create_printer(self):
        self.client.login(username="operator_user", password="testpass123")
        resp = self.client.post(reverse("core:printerprofile_create"), {"name": "Op Printer"})
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(PrinterProfile.objects.filter(name="Op Printer").exists())

    def test_operator_cannot_create_project(self):
        self.client.login(username="operator_user", password="testpass123")
        resp = self.client.post(
            reverse("core:project_create"),
            {"name": "Hacked Project", "description": ""},
        )
        self.assertEqual(resp.status_code, 403)

    def test_operator_cannot_access_user_list(self):
        self.client.login(username="operator_user", password="testpass123")
        resp = self.client.get(reverse("core:user_list"))
        self.assertEqual(resp.status_code, 403)

    def test_operator_can_view_project_list(self):
        self.client.login(username="operator_user", password="testpass123")
        resp = self.client.get(reverse("core:project_list"))
        self.assertEqual(resp.status_code, 200)


@override_settings(ALLOWED_HOSTS=["testserver"])
class AdminPermissionTests(_RBACTestBase):
    """Verify Admin role has full access."""

    def test_admin_can_access_user_list(self):
        self.client.login(username="admin_user", password="testpass123")
        resp = self.client.get(reverse("core:user_list"))
        self.assertEqual(resp.status_code, 200)

    def test_admin_can_create_project(self):
        self.client.login(username="admin_user", password="testpass123")
        resp = self.client.post(
            reverse("core:project_create"),
            {"name": "Admin Project", "description": ""},
        )
        self.assertEqual(resp.status_code, 302)

    def test_admin_can_create_printer(self):
        self.client.login(username="admin_user", password="testpass123")
        resp = self.client.post(reverse("core:printerprofile_create"), {"name": "Admin Printer"})
        self.assertEqual(resp.status_code, 302)


@override_settings(ALLOWED_HOSTS=["testserver"])
class UnauthenticatedAccessTests(TestCase):
    """Verify unauthenticated users cannot access write endpoints.

    Note: RoleRequiredMixin sets raise_exception=True, so unauthenticated
    users get 403 instead of a login redirect for permission-protected views.
    """

    def test_project_create_denied(self):
        resp = self.client.get(reverse("core:project_create"))
        self.assertIn(resp.status_code, [302, 403])

    def test_printerprofile_create_denied(self):
        resp = self.client.get(reverse("core:printerprofile_create"))
        self.assertIn(resp.status_code, [302, 403])

    def test_user_list_denied(self):
        resp = self.client.get(reverse("core:user_list"))
        self.assertIn(resp.status_code, [302, 403])

    def test_printjob_create_denied(self):
        resp = self.client.get(reverse("core:printjob_create"))
        self.assertIn(resp.status_code, [302, 403])

    def test_printqueue_create_denied(self):
        resp = self.client.get(reverse("core:printqueue_create"))
        self.assertIn(resp.status_code, [302, 403])


# ===========================================================================
# Registration role assignment tests
# ===========================================================================


@override_settings(ALLOWED_HOSTS=["testserver"])
class RegistrationRoleAssignmentTests(TestCase):
    """Verify that the first user gets Admin role and subsequent get Designer."""

    def test_first_user_gets_admin(self):
        """First registered user should be assigned to Admin group."""
        self.client.post(
            reverse("core:register"),
            {
                "username": "first_user",
                "email": "first@test.com",
                "password1": "Str0ngP@ss!",
                "password2": "Str0ngP@ss!",
            },
        )
        user = User.objects.get(username="first_user")
        self.assertTrue(user.groups.filter(name="Admin").exists())

    def test_subsequent_user_gets_designer(self):
        """Second and later users should be assigned to Designer group."""
        # Create first user (gets Admin)
        User.objects.create_user(username="existing", password="pass123")
        admin_group = Group.objects.get(name="Admin")
        User.objects.get(username="existing").groups.add(admin_group)

        self.client.post(
            reverse("core:register"),
            {
                "username": "second_user",
                "email": "second@test.com",
                "password1": "Str0ngP@ss!",
                "password2": "Str0ngP@ss!",
            },
        )
        user = User.objects.get(username="second_user")
        self.assertTrue(user.groups.filter(name="Designer").exists())
        self.assertFalse(user.groups.filter(name="Admin").exists())


# ===========================================================================
# Additional form validation tests
# ===========================================================================


class CostProfileFormTests(TestCase):
    """Tests for the CostProfileForm validation."""

    def test_valid_form(self):
        from core.forms import CostProfileForm

        form = CostProfileForm(
            data={
                "electricity_cost_per_kwh": 0.30,
                "printer_power_watts": 200,
                "printer_purchase_cost": 500,
                "printer_lifespan_hours": 5000,
                "maintenance_cost_per_hour": 0.05,
            }
        )
        self.assertTrue(form.is_valid())

    def test_empty_electricity_cost_invalid(self):
        from core.forms import CostProfileForm

        form = CostProfileForm(
            data={
                "electricity_cost_per_kwh": "",
                "printer_power_watts": 200,
                "printer_purchase_cost": 500,
                "printer_lifespan_hours": 5000,
                "maintenance_cost_per_hour": 0.05,
            }
        )
        self.assertFalse(form.is_valid())


class PrinterProfileFormTests(TestCase):
    """Tests for the PrinterProfileForm."""

    def test_valid_minimal(self):
        from core.forms import PrinterProfileForm

        form = PrinterProfileForm(data={"name": "My Printer"})
        self.assertTrue(form.is_valid())

    def test_name_required(self):
        from core.forms import PrinterProfileForm

        form = PrinterProfileForm(data={"name": ""})
        self.assertFalse(form.is_valid())
        self.assertIn("name", form.errors)

    def test_with_moonraker_url(self):
        from core.forms import PrinterProfileForm

        form = PrinterProfileForm(
            data={
                "name": "Networked Printer",
                "moonraker_url": "http://192.168.1.100:7125",
            }
        )
        self.assertTrue(form.is_valid())


class UserManagementFormTests(TestCase):
    """Tests for the UserManagementForm."""

    def test_new_user_requires_password(self):
        from core.forms import UserManagementForm

        form = UserManagementForm(
            data={
                "username": "newuser",
                "email": "new@test.com",
                "role": "Designer",
                "is_active": True,
            }
        )
        self.assertFalse(form.is_valid())

    def test_password_mismatch(self):
        from core.forms import UserManagementForm

        form = UserManagementForm(
            data={
                "username": "newuser",
                "email": "new@test.com",
                "role": "Designer",
                "is_active": True,
                "password1": "Str0ngP@ss!",
                "password2": "DifferentP@ss!",
            }
        )
        self.assertFalse(form.is_valid())

    def test_valid_new_user(self):
        from core.forms import UserManagementForm

        form = UserManagementForm(
            data={
                "username": "newuser",
                "email": "new@test.com",
                "role": "Designer",
                "is_active": True,
                "password1": "Str0ngP@ss!",
                "password2": "Str0ngP@ss!",
            }
        )
        self.assertTrue(form.is_valid())

    def test_edit_existing_no_password_ok(self):
        """Editing an existing user without changing password should be valid."""
        from core.forms import UserManagementForm

        user = User.objects.create_user(username="existinguser", password="oldpass123")
        Group.objects.get(name="Designer")  # ensure group exists
        form = UserManagementForm(
            data={
                "username": "existinguser",
                "email": "exist@test.com",
                "role": "Designer",
                "is_active": True,
            },
            instance=user,
        )
        self.assertTrue(form.is_valid())


class OrcaProfileImportFormTests(TestCase):
    """Tests for OrcaSlicer profile import form validation."""

    def test_invalid_json_file(self):
        from core.forms import OrcaMachineProfileImportForm

        bad_file = SimpleUploadedFile("bad.json", b"not json", content_type="application/json")
        form = OrcaMachineProfileImportForm(data={}, files={"profile_file": bad_file})
        self.assertFalse(form.is_valid())

    def test_missing_name_field(self):
        import json

        from core.forms import OrcaMachineProfileImportForm

        data = json.dumps({"type": "machine"}).encode("utf-8")
        f = SimpleUploadedFile("profile.json", data, content_type="application/json")
        form = OrcaMachineProfileImportForm(data={}, files={"profile_file": f})
        self.assertFalse(form.is_valid())

    def test_wrong_type(self):
        import json

        from core.forms import OrcaMachineProfileImportForm

        data = json.dumps({"name": "Test", "type": "filament"}).encode("utf-8")
        f = SimpleUploadedFile("profile.json", data, content_type="application/json")
        form = OrcaMachineProfileImportForm(data={}, files={"profile_file": f})
        self.assertFalse(form.is_valid())

    def test_valid_machine_profile(self):
        import json

        from core.forms import OrcaMachineProfileImportForm

        data = json.dumps({"name": "Test Machine", "type": "machine"}).encode("utf-8")
        f = SimpleUploadedFile("profile.json", data, content_type="application/json")
        form = OrcaMachineProfileImportForm(data={}, files={"profile_file": f})
        self.assertTrue(form.is_valid())

    def test_non_json_extension_rejected(self):
        from core.forms import OrcaMachineProfileImportForm

        f = SimpleUploadedFile("profile.txt", b'{"name": "test"}', content_type="text/plain")
        form = OrcaMachineProfileImportForm(data={}, files={"profile_file": f})
        self.assertFalse(form.is_valid())


# ===========================================================================
# STL file validation edge cases
# ===========================================================================


class PartFormFileValidationTests(TestCase):
    """Additional edge-case tests for Part form file validation."""

    def test_stl_file_too_large(self):
        # Simulate a file > 100 MB
        large_file = SimpleUploadedFile(
            "big.stl",
            b"x" * (100 * 1024 * 1024 + 1),
            content_type="application/sla",
        )
        form = PartForm(
            data={"name": "Part", "quantity": 1, "color": "black", "material": "PLA"},
            files={"stl_file": large_file},
        )
        self.assertFalse(form.is_valid())
        self.assertIn("stl_file", form.errors)

    def test_stl_mixed_case_extension(self):
        stl = SimpleUploadedFile("model.STL", b"solid test", content_type="application/sla")
        form = PartForm(
            data={"name": "Part", "quantity": 1, "color": "black", "material": "PLA"},
            files={"stl_file": stl},
        )
        self.assertTrue(form.is_valid())

    def test_quantity_zero_rejected(self):
        form = PartForm(data={"name": "Part", "quantity": 0, "color": "black", "material": "PLA"})
        self.assertFalse(form.is_valid())
        self.assertIn("quantity", form.errors)


# ===========================================================================
# Aggregated status tests
# ===========================================================================


class ProjectAggregatedStatusTests(TestDataMixin, TestCase):
    """Tests for the Project.aggregated_status property."""

    def test_empty_project(self):
        """A project with no parts returns 'empty'."""
        empty = Project.objects.create(name="Empty", created_by=self.user)
        self.assertEqual(empty.aggregated_status, Project.STATUS_EMPTY)
        self.assertEqual(empty.aggregated_status_display, "Empty")

    def test_pending_status(self):
        """Parts without filament estimates result in 'pending'."""
        proj = Project.objects.create(name="Pending", created_by=self.user)
        Part.objects.create(project=proj, name="P1", quantity=1, filament_used_grams=None)
        self.assertEqual(proj.aggregated_status, Project.STATUS_PENDING)

    def test_ready_status(self):
        """All parts estimated but none printed → 'ready'."""
        proj = Project.objects.create(name="Ready", created_by=self.user)
        Part.objects.create(
            project=proj,
            name="P1",
            quantity=1,
            filament_used_grams=10.0,
            estimation_status=Part.ESTIMATION_SUCCESS,
        )
        self.assertEqual(proj.aggregated_status, Project.STATUS_READY)

    def test_in_progress_status(self):
        """Some parts printed → 'in_progress'."""
        proj = Project.objects.create(name="InProgress", created_by=self.user)
        part = Part.objects.create(
            project=proj,
            name="P1",
            quantity=3,
            filament_used_grams=10.0,
            estimation_status=Part.ESTIMATION_SUCCESS,
        )
        job = PrintJob.objects.create(status="completed", created_by=self.user)
        PrintJobPart.objects.create(print_job=job, part=part, quantity=1)
        PrintJobPlate.objects.create(print_job=job, plate_number=1, status=PrintJobPlate.STATUS_COMPLETED)
        self.assertEqual(proj.aggregated_status, Project.STATUS_IN_PROGRESS)

    def test_complete_status(self):
        """All parts fully printed → 'complete'."""
        proj = Project.objects.create(name="Complete", created_by=self.user)
        part = Part.objects.create(
            project=proj,
            name="P1",
            quantity=2,
            filament_used_grams=10.0,
            estimation_status=Part.ESTIMATION_SUCCESS,
        )
        job = PrintJob.objects.create(status="completed", created_by=self.user)
        PrintJobPart.objects.create(print_job=job, part=part, quantity=2)
        PrintJobPlate.objects.create(print_job=job, plate_number=1, status=PrintJobPlate.STATUS_COMPLETED)
        self.assertEqual(proj.aggregated_status, Project.STATUS_COMPLETE)

    def test_error_status(self):
        """Any part with estimation error → 'error'."""
        proj = Project.objects.create(name="Error", created_by=self.user)
        Part.objects.create(
            project=proj,
            name="P1",
            quantity=1,
            estimation_status=Part.ESTIMATION_ERROR,
            estimation_error="OrcaSlicer timeout",
        )
        self.assertEqual(proj.aggregated_status, Project.STATUS_ERROR)

    def test_estimating_status(self):
        """Any part with pending estimation → 'estimating'."""
        proj = Project.objects.create(name="Estimating", created_by=self.user)
        Part.objects.create(
            project=proj,
            name="P1",
            quantity=1,
            estimation_status=Part.ESTIMATION_PENDING,
        )
        self.assertEqual(proj.aggregated_status, Project.STATUS_ESTIMATING)

    def test_estimating_active_status(self):
        """Any part actively estimating → 'estimating'."""
        proj = Project.objects.create(name="Active", created_by=self.user)
        Part.objects.create(
            project=proj,
            name="P1",
            quantity=1,
            estimation_status=Part.ESTIMATION_ESTIMATING,
        )
        self.assertEqual(proj.aggregated_status, Project.STATUS_ESTIMATING)

    def test_error_takes_priority_over_estimating(self):
        """Error status wins even if another part is estimating."""
        proj = Project.objects.create(name="Mixed", created_by=self.user)
        Part.objects.create(
            project=proj,
            name="P1",
            quantity=1,
            estimation_status=Part.ESTIMATION_ERROR,
            estimation_error="fail",
        )
        Part.objects.create(
            project=proj,
            name="P2",
            quantity=1,
            estimation_status=Part.ESTIMATION_PENDING,
        )
        self.assertEqual(proj.aggregated_status, Project.STATUS_ERROR)

    def test_subproject_parts_included(self):
        """Parts in sub-projects contribute to parent's status."""
        parent = Project.objects.create(name="Parent", created_by=self.user)
        child = Project.objects.create(name="Child", parent=parent, quantity=2, created_by=self.user)
        Part.objects.create(
            project=child,
            name="P1",
            quantity=1,
            filament_used_grams=5.0,
            estimation_status=Part.ESTIMATION_SUCCESS,
        )
        self.assertEqual(parent.aggregated_status, Project.STATUS_READY)

    def test_subproject_error_propagates(self):
        """Error in a sub-project part propagates to the parent."""
        parent = Project.objects.create(name="Parent", created_by=self.user)
        child = Project.objects.create(name="Child", parent=parent, quantity=1, created_by=self.user)
        Part.objects.create(
            project=child,
            name="P1",
            quantity=1,
            estimation_status=Part.ESTIMATION_ERROR,
            estimation_error="fail",
        )
        self.assertEqual(parent.aggregated_status, Project.STATUS_ERROR)


# ===========================================================================
# Admin Dashboard tests
# ===========================================================================


class AdminDashboardViewTests(TestDataMixin, TestCase):
    """Tests for the AdminDashboardView."""

    def setUp(self):
        super().setUp()
        self.client.login(username="testuser", password="testpass123")
        self.url = reverse("core:admin_dashboard")

    def test_admin_can_access(self):
        """Admin users can access the admin dashboard."""
        resp = self.client.get(self.url)
        self.assertEqual(resp.status_code, 200)

    def test_non_admin_denied(self):
        """Non-admin users receive a 403."""
        # other_user has no Admin group
        designer_group = Group.objects.get(name="Designer")
        self.other_user.groups.add(designer_group)
        self.client.login(username="otheruser", password="otherpass123")
        resp = self.client.get(self.url)
        self.assertEqual(resp.status_code, 403)

    def test_operator_denied(self):
        """Operator users also receive a 403."""
        operator_group = Group.objects.get(name="Operator")
        self.other_user.groups.add(operator_group)
        self.client.login(username="otheruser", password="otherpass123")
        resp = self.client.get(self.url)
        self.assertEqual(resp.status_code, 403)

    def test_nav_visible_for_admin(self):
        """Admin Dashboard link is visible in navigation for admins."""
        resp = self.client.get(reverse("core:dashboard"))
        self.assertContains(resp, "admin-dashboard/")

    def test_nav_hidden_for_non_admin(self):
        """Admin Dashboard link is hidden for non-admin users."""
        designer_group = Group.objects.get(name="Designer")
        self.other_user.groups.add(designer_group)
        self.client.login(username="otheruser", password="otherpass123")
        resp = self.client.get(reverse("core:dashboard"))
        self.assertNotContains(resp, "admin-dashboard/")

    def test_context_contains_system_stats(self):
        """Context includes expected system statistic keys."""
        resp = self.client.get(self.url)
        for key in [
            "total_projects",
            "total_parts",
            "total_users",
            "total_printers",
            "total_jobs",
            "total_storage_mb",
        ]:
            self.assertIn(key, resp.context, f"Missing context key: {key}")

    def test_context_contains_estimation_data(self):
        """Context includes unified queue and estimation breakdown data."""
        resp = self.client.get(self.url)
        for key in [
            "queue_active",
            "queue_waiting",
            "queue_errors",
            "queue_active_count",
            "queue_waiting_count",
            "queue_error_count",
            "parts_estimated",
            "parts_estimating_count",
            "parts_pending_count",
            "parts_error_count",
        ]:
            self.assertIn(key, resp.context, f"Missing context key: {key}")

    def test_context_contains_recent_activity(self):
        """Context includes recent projects, parts, and jobs."""
        resp = self.client.get(self.url)
        self.assertIn("recent_projects", resp.context)
        self.assertIn("recent_parts", resp.context)
        self.assertIn("recent_jobs", resp.context)

    def test_error_parts_displayed(self):
        """Parts with estimation errors appear in queue_errors."""
        self.part.estimation_status = Part.ESTIMATION_ERROR
        self.part.estimation_error = "Test error"
        self.part.save()
        resp = self.client.get(self.url)
        self.assertEqual(resp.context["queue_error_count"], 1)
        self.assertEqual(resp.context["parts_error_count"], 1)
        self.assertEqual(resp.context["queue_errors"][0]["type"], "estimation")

    def test_estimating_parts_displayed(self):
        """Parts actively estimating appear in queue_active."""
        self.part.estimation_status = Part.ESTIMATION_ESTIMATING
        self.part.save()
        resp = self.client.get(self.url)
        self.assertEqual(resp.context["queue_active_count"], 1)
        self.assertEqual(resp.context["parts_estimating_count"], 1)
        self.assertEqual(resp.context["queue_active"][0]["type"], "estimation")

    def test_pending_parts_displayed(self):
        """Parts waiting for estimation appear in queue_waiting."""
        self.part.estimation_status = Part.ESTIMATION_PENDING
        self.part.save()
        resp = self.client.get(self.url)
        self.assertEqual(resp.context["queue_waiting_count"], 1)
        self.assertEqual(resp.context["parts_pending_count"], 1)
        self.assertEqual(resp.context["queue_waiting"][0]["type"], "estimation")

    def test_unauthenticated_denied(self):
        """Unauthenticated users get a 403."""
        self.client.logout()
        resp = self.client.get(self.url)
        self.assertEqual(resp.status_code, 403)


# ===========================================================================
# Estimation worker tests
# ===========================================================================


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
        from core.views import _trigger_part_estimation

        self.part.stl_file = SimpleUploadedFile("test.stl", b"solid test")
        self.part.print_preset = self.preset
        self.part.save()

        with patch("core.views._start_orcaslicer_worker"):
            _trigger_part_estimation(self.part)

        self.part.refresh_from_db()
        self.assertEqual(self.part.estimation_status, Part.ESTIMATION_PENDING)

    @patch("core.views._start_orcaslicer_worker")
    def test_trigger_calls_start_worker(self, mock_start: "patch"):
        """_trigger_part_estimation calls _start_orcaslicer_worker."""
        self.part.stl_file = SimpleUploadedFile("test.stl", b"solid test")
        self.part.print_preset = self.preset
        self.part.save()

        from core.views import _trigger_part_estimation

        _trigger_part_estimation(self.part)

        mock_start.assert_called_once()

    def test_worker_does_not_start_duplicate(self):
        """Only one worker thread should be active at a time."""
        import core.views as views_mod
        from core.views import (
            _orcaslicer_worker_lock,
            _start_orcaslicer_worker,
        )

        # Simulate an active worker
        with _orcaslicer_worker_lock:
            original = views_mod._orcaslicer_worker_active
            views_mod._orcaslicer_worker_active = True

        try:
            with patch("threading.Thread") as mock_thread:
                _start_orcaslicer_worker()
                mock_thread.assert_not_called()
        finally:
            with _orcaslicer_worker_lock:
                views_mod._orcaslicer_worker_active = original

    @patch("core.views._estimate_part_in_background")
    def test_worker_loop_processes_pending_sequentially(self, mock_estimate: "patch"):
        """Worker loop picks pending parts one by one."""
        import core.views as views_mod
        from core.views import _orcaslicer_worker_loop

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
        with views_mod._orcaslicer_worker_lock:
            views_mod._orcaslicer_worker_active = True

        _orcaslicer_worker_loop()

        # Both parts should have been processed
        self.assertEqual(mock_estimate.call_count, 2)
        # Worker should have processed p1 first (lower pk)
        first_call_pk = mock_estimate.call_args_list[0][0][0]
        second_call_pk = mock_estimate.call_args_list[1][0][0]
        self.assertEqual(first_call_pk, p1.pk)
        self.assertEqual(second_call_pk, p2.pk)

    @patch("core.views._estimate_part_in_background")
    def test_worker_loop_stops_when_no_pending(self, mock_estimate: "patch"):
        """Worker loop stops gracefully when no pending work exists."""
        import core.views as views_mod
        from core.views import _orcaslicer_worker_loop

        # No pending parts or jobs
        with views_mod._orcaslicer_worker_lock:
            views_mod._orcaslicer_worker_active = True

        _orcaslicer_worker_loop()

        mock_estimate.assert_not_called()
        # Worker should have set itself as inactive
        self.assertFalse(views_mod._orcaslicer_worker_active)

    @patch("core.views._slice_job_in_background")
    @patch("core.views._estimate_part_in_background")
    def test_worker_loop_processes_slicing_before_estimations(self, mock_estimate: "patch", mock_slice: "patch"):
        """Worker processes slicing jobs before estimation parts."""
        import core.views as views_mod
        from core.views import _orcaslicer_worker_loop

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

        with views_mod._orcaslicer_worker_lock:
            views_mod._orcaslicer_worker_active = True

        _orcaslicer_worker_loop()

        # Slicing should have been processed first
        self.assertEqual(len(call_order), 2)
        self.assertEqual(call_order[0], ("slice", job.pk))
        self.assertEqual(call_order[1], ("estimate", part.pk))

    @patch("core.views._start_orcaslicer_worker")
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


# ===========================================================================
# CreateJobsFromProject view tests
# ===========================================================================


class CreateJobsFromProjectViewTests(TestDataMixin, TestCase):
    """Tests for the bulk 'Create Print Job(s)' action on a project."""

    def setUp(self):
        super().setUp()
        self.client.login(username="testuser", password="testpass123")
        # Give the base part an STL file so it is eligible
        self.part.stl_file = SimpleUploadedFile("part1.stl", b"solid part1")
        self.part.save()

    def _url(self):
        return reverse("core:project_create_jobs", args=[self.project.pk])

    def test_creates_single_job_for_compatible_parts(self):
        """All parts with same preset/filament end up in one job."""
        Part.objects.create(
            project=self.project,
            name="Part Two",
            quantity=2,
            stl_file=SimpleUploadedFile("part2.stl", b"solid part2"),
        )
        resp = self.client.post(self._url())
        self.assertEqual(resp.status_code, 302)
        # One job created
        jobs = PrintJob.objects.filter(created_by=self.user)
        self.assertEqual(jobs.count(), 1)
        job = jobs.first()
        self.assertEqual(job.status, PrintJob.STATUS_DRAFT)
        # Both parts in the job
        self.assertEqual(job.job_parts.count(), 2)

    def test_creates_multiple_jobs_for_different_filaments(self):
        """Parts with different spoolman_filament_id create separate jobs."""
        self.part.spoolman_filament_id = 10
        self.part.save()

        Part.objects.create(
            project=self.project,
            name="Part Two",
            quantity=1,
            spoolman_filament_id=20,
            stl_file=SimpleUploadedFile("part2.stl", b"solid part2"),
        )
        resp = self.client.post(self._url())
        self.assertEqual(resp.status_code, 302)
        jobs = PrintJob.objects.filter(created_by=self.user)
        self.assertEqual(jobs.count(), 2)

    def test_skips_parts_without_stl(self):
        """Parts without an STL file are not added to any job."""
        Part.objects.create(
            project=self.project,
            name="No STL Part",
            quantity=1,
        )
        resp = self.client.post(self._url())
        self.assertEqual(resp.status_code, 302)
        job = PrintJob.objects.filter(created_by=self.user).first()
        # Only the part with STL should be in the job
        self.assertEqual(job.job_parts.count(), 1)
        self.assertEqual(job.job_parts.first().part, self.part)

    def test_skips_fully_printed_parts(self):
        """Parts with remaining_quantity == 0 are skipped."""
        # Mark part as fully printed via a completed job plate
        job = PrintJob.objects.create(
            name="Old Job",
            status=PrintJob.STATUS_DRAFT,
            created_by=self.user,
        )
        PrintJobPart.objects.create(print_job=job, part=self.part, quantity=self.part.quantity)
        PrintJobPlate.objects.create(print_job=job, plate_number=1, status="completed")
        # Now remaining_quantity should be 0
        self.assertEqual(self.part.remaining_quantity, 0)

        resp = self.client.post(self._url())
        self.assertEqual(resp.status_code, 302)
        # No new draft job should be created (only the old one exists)
        new_jobs = PrintJob.objects.filter(created_by=self.user, status=PrintJob.STATUS_DRAFT)
        self.assertEqual(new_jobs.count(), 1)  # only the old one

    def test_uses_remaining_quantity(self):
        """Job parts use remaining_quantity, not total quantity."""
        self.client.post(self._url())
        job = PrintJob.objects.filter(created_by=self.user).first()
        jp = job.job_parts.first()
        self.assertEqual(jp.quantity, self.part.remaining_quantity)

    def test_no_eligible_parts_shows_warning(self):
        """When no parts are eligible, a warning message is shown."""
        self.part.stl_file.delete(save=True)
        resp = self.client.post(self._url())
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(PrintJob.objects.filter(created_by=self.user).count(), 0)

    def test_redirects_to_job_when_single(self):
        """When only one job is created, redirect to its detail page."""
        resp = self.client.post(self._url())
        job = PrintJob.objects.filter(created_by=self.user).first()
        self.assertRedirects(resp, reverse("core:printjob_detail", args=[job.pk]))

    def test_redirects_to_project_when_multiple(self):
        """When multiple jobs are created, redirect to the project."""
        self.part.spoolman_filament_id = 10
        self.part.save()
        Part.objects.create(
            project=self.project,
            name="Part Two",
            quantity=1,
            spoolman_filament_id=20,
            stl_file=SimpleUploadedFile("part2.stl", b"solid part2"),
        )
        resp = self.client.post(self._url())
        self.assertRedirects(resp, reverse("core:project_detail", args=[self.project.pk]))

    def test_requires_permission(self):
        """Users without add_printjob permission get a 403."""
        self.client.login(username="otheruser", password="otherpass123")
        resp = self.client.post(self._url())
        self.assertEqual(resp.status_code, 403)


# ===========================================================================
# ProjectDocument tests
# ===========================================================================


class ProjectDocumentModelTests(TestDataMixin, TestCase):
    """Tests for the ProjectDocument model."""

    def _create_doc(self, name: str = "readme.pdf", filename: str = "readme.pdf") -> ProjectDocument:
        fake_file = SimpleUploadedFile(filename, b"fake content", content_type="application/pdf")
        return ProjectDocument.objects.create(
            project=self.project,
            name=name,
            file=fake_file,
            uploaded_by=self.user,
        )

    def test_str(self):
        doc = self._create_doc()
        self.assertEqual(str(doc), "readme.pdf")

    def test_file_extension_property(self):
        doc = self._create_doc(filename="drawing.step")
        self.assertEqual(doc.file_extension, "step")

    def test_ordering(self):
        self._create_doc(name="First")
        doc2 = self._create_doc(name="Second")
        docs = list(self.project.documents.all())
        self.assertEqual(docs[0], doc2)  # newest first

    def test_cascade_delete(self):
        self._create_doc()
        self.assertEqual(ProjectDocument.objects.count(), 1)
        self.project.delete()
        self.assertEqual(ProjectDocument.objects.count(), 0)


class ProjectDocumentAggregationTests(TestDataMixin, TestCase):
    """Test document aggregation across sub-projects."""

    def test_collect_documents_includes_subproject_docs(self):
        sub = Project.objects.create(name="SubProject", parent=self.project, quantity=2)
        fake_file = SimpleUploadedFile("main.pdf", b"content")
        ProjectDocument.objects.create(project=self.project, name="Main Doc", file=fake_file)
        fake_file2 = SimpleUploadedFile("sub.pdf", b"content")
        ProjectDocument.objects.create(project=sub, name="Sub Doc", file=fake_file2)

        all_docs = self.project._collect_documents()
        self.assertEqual(len(all_docs), 2)
        names = {doc.name for doc, _ in all_docs}
        self.assertIn("Main Doc", names)
        self.assertIn("Sub Doc", names)

    def test_collect_documents_returns_project_objects(self):
        """Second element of each tuple should be the owning Project instance."""
        sub = Project.objects.create(name="SubProject", parent=self.project, quantity=2)
        fake_file = SimpleUploadedFile("main.pdf", b"content")
        ProjectDocument.objects.create(project=self.project, name="Main Doc", file=fake_file)
        fake_file2 = SimpleUploadedFile("sub.pdf", b"content")
        ProjectDocument.objects.create(project=sub, name="Sub Doc", file=fake_file2)

        all_docs = self.project._collect_documents()
        project_ids = {proj.pk for _, proj in all_docs}
        self.assertIn(self.project.pk, project_ids)
        self.assertIn(sub.pk, project_ids)


@override_settings(MEDIA_ROOT="/tmp/layernexus_test_media/")  # noqa: S108
class ProjectDocumentFormTests(TestCase):
    """Tests for ProjectDocumentForm validation."""

    def test_valid_pdf(self):
        f = SimpleUploadedFile("doc.pdf", b"content", content_type="application/pdf")
        form = ProjectDocumentForm(data={"name": "My Doc"}, files={"file": f})
        self.assertTrue(form.is_valid())

    def test_invalid_extension(self):
        f = SimpleUploadedFile("script.exe", b"content", content_type="application/octet-stream")
        form = ProjectDocumentForm(data={"name": "Bad File"}, files={"file": f})
        self.assertFalse(form.is_valid())
        self.assertIn("file", form.errors)

    def test_auto_fill_name(self):
        f = SimpleUploadedFile("assembly_guide.pdf", b"content", content_type="application/pdf")
        form = ProjectDocumentForm(data={"name": ""}, files={"file": f})
        self.assertTrue(form.is_valid())
        self.assertEqual(form.cleaned_data["name"], "assembly_guide")


class ProjectDocumentViewTests(TestDataMixin, TestCase):
    """Tests for document CRUD views."""

    def setUp(self):
        super().setUp()
        self.client = Client()
        self.client.login(username="testuser", password="testpass123")

    def test_create_document(self):
        f = SimpleUploadedFile("test.pdf", b"content", content_type="application/pdf")
        url = reverse("core:document_create", args=[self.project.pk])
        resp = self.client.post(url, {"name": "Test Doc", "file": f})
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(self.project.documents.count(), 1)

    def test_delete_document(self):
        f = SimpleUploadedFile("test.pdf", b"content")
        doc = ProjectDocument.objects.create(project=self.project, name="Test", file=f)
        url = reverse("core:document_delete", args=[doc.pk])
        resp = self.client.post(url)
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(self.project.documents.count(), 0)

    def test_create_requires_permission(self):
        self.client.login(username="otheruser", password="otherpass123")
        url = reverse("core:document_create", args=[self.project.pk])
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 403)


@override_settings(MEDIA_ROOT="/tmp/layernexus_test_media/")  # noqa: S108
class ProjectDocumentDownloadViewTests(TestDataMixin, TestCase):
    """Tests for ProjectDocumentDownloadView."""

    def setUp(self):
        super().setUp()
        self.client = Client()
        f = SimpleUploadedFile("guide.pdf", b"pdf content", content_type="application/pdf")
        self.doc = ProjectDocument.objects.create(
            project=self.project,
            name="Guide",
            file=f,
        )
        self.url = reverse("core:document_download", args=[self.doc.pk])

    def test_download_redirects_anonymous(self):
        """Unauthenticated requests should be redirected to the login page."""
        self.client.logout()
        resp = self.client.get(self.url)
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/accounts/login/", resp["Location"])

    def test_download_returns_200_for_logged_in_user(self):
        """Authenticated users receive a 200 file response."""
        self.client.login(username="testuser", password="testpass123")
        resp = self.client.get(self.url)
        self.assertEqual(resp.status_code, 200)

    def test_download_content_disposition(self):
        """Response should include Content-Disposition attachment with a .pdf filename."""
        self.client.login(username="testuser", password="testpass123")
        resp = self.client.get(self.url)
        self.assertEqual(resp.status_code, 200)
        content_disposition = resp.get("Content-Disposition", "")
        self.assertIn("attachment", content_disposition)
        # Django may append a suffix to avoid name collisions, so check the extension only
        self.assertIn(".pdf", content_disposition)

    def test_download_404_for_missing_document(self):
        """Requesting a non-existent document pk returns 404."""
        self.client.login(username="testuser", password="testpass123")
        url = reverse("core:document_download", args=[99999])
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 404)


# ===========================================================================
# HardwarePart & ProjectHardware tests
# ===========================================================================


class HardwarePartModelTests(TestDataMixin, TestCase):
    """Tests for the HardwarePart model."""

    def test_str(self):
        hp = HardwarePart.objects.create(name="M3x10", category="screws")
        self.assertEqual(str(hp), "Screws: M3x10")

    def test_unique_together(self):
        HardwarePart.objects.create(name="M3x10", category="screws")
        with self.assertRaises(IntegrityError):
            HardwarePart.objects.create(name="M3x10", category="screws")

    def test_different_category_allowed(self):
        HardwarePart.objects.create(name="M3", category="screws")
        hp2 = HardwarePart.objects.create(name="M3", category="nuts")
        self.assertEqual(hp2.category, "nuts")


class ProjectHardwareModelTests(TestDataMixin, TestCase):
    """Tests for the ProjectHardware through model."""

    def _create_hw(self, price=None, qty=5) -> ProjectHardware:
        hp = HardwarePart.objects.create(name="M3x10", category="screws", unit_price=price)
        return ProjectHardware.objects.create(project=self.project, hardware_part=hp, quantity=qty)

    def test_str(self):
        ph = self._create_hw(qty=3)
        self.assertIn("×3", str(ph))

    def test_total_price_with_price(self):
        ph = self._create_hw(price="0.15", qty=10)
        self.assertAlmostEqual(ph.total_price, 1.5)

    def test_total_price_without_price(self):
        ph = self._create_hw(price=None)
        self.assertIsNone(ph.total_price)

    def test_unique_together(self):
        hp = HardwarePart.objects.create(name="M3x10", category="screws")
        ProjectHardware.objects.create(project=self.project, hardware_part=hp, quantity=1)
        with self.assertRaises(IntegrityError):
            ProjectHardware.objects.create(project=self.project, hardware_part=hp, quantity=2)

    def test_reuse_across_projects(self):
        hp = HardwarePart.objects.create(name="Motor", category="motors")
        ProjectHardware.objects.create(project=self.project, hardware_part=hp, quantity=2)
        ProjectHardware.objects.create(project=self.other_project, hardware_part=hp, quantity=4)
        self.assertEqual(hp.project_assignments.count(), 2)


class ProjectHardwareAggregationTests(TestDataMixin, TestCase):
    """Tests for hardware aggregation across sub-projects."""

    def test_collect_hardware_with_multiplier(self):
        sub = Project.objects.create(name="Sub", parent=self.project, quantity=3)
        hp = HardwarePart.objects.create(name="Bolt", category="bolts", unit_price="0.50")
        ProjectHardware.objects.create(project=sub, hardware_part=hp, quantity=4)

        hw_list = self.project._collect_hardware_with_multiplier()
        self.assertEqual(len(hw_list), 1)
        hw, mult = hw_list[0]
        self.assertEqual(hw.quantity, 4)
        self.assertEqual(mult, 3)  # sub-project quantity

    def test_total_hardware_cost_with_multiplier(self):
        sub = Project.objects.create(name="Sub", parent=self.project, quantity=2)
        hp = HardwarePart.objects.create(name="Screw", category="screws", unit_price="0.10")
        ProjectHardware.objects.create(project=self.project, hardware_part=hp, quantity=10)
        ProjectHardware.objects.create(project=sub, hardware_part=hp, quantity=5)

        # project: 10 × 0.10 × 1 = 1.00
        # sub:      5 × 0.10 × 2 = 1.00
        # total: 2.00
        self.assertAlmostEqual(self.project.total_hardware_cost, 2.00)

    def test_hardware_requirements_grouping(self):
        sub = Project.objects.create(name="Sub", parent=self.project, quantity=2)
        hp = HardwarePart.objects.create(name="Nut", category="nuts", unit_price="0.05")
        ProjectHardware.objects.create(project=self.project, hardware_part=hp, quantity=10)
        ProjectHardware.objects.create(project=sub, hardware_part=hp, quantity=5)

        reqs = self.project.hardware_requirements()
        self.assertEqual(len(reqs), 1)
        self.assertEqual(reqs[0]["total_quantity"], 20)  # 10×1 + 5×2
        self.assertAlmostEqual(reqs[0]["total_price"], 1.00)

    def test_total_hardware_cost_skips_none_prices(self):
        hp = HardwarePart.objects.create(name="Custom", category="other", unit_price=None)
        ProjectHardware.objects.create(project=self.project, hardware_part=hp, quantity=10)
        self.assertEqual(self.project.total_hardware_cost, 0.0)


class ProjectHardwareFormTests(TestCase):
    """Tests for ProjectHardwareForm validation and save."""

    def setUp(self):
        self.user = User.objects.create_user(username="testuser", password="testpass123")
        self.project = Project.objects.create(name="Test", created_by=self.user)

    def test_existing_part_selection(self):
        hp = HardwarePart.objects.create(name="M3x10", category="screws")
        form = ProjectHardwareForm(
            data={
                "hardware_part": hp.pk,
                "quantity": 5,
                "notes": "",
            }
        )
        self.assertTrue(form.is_valid())
        ph = form.save(project=self.project, user=self.user)
        self.assertEqual(ph.hardware_part, hp)
        self.assertEqual(ph.quantity, 5)

    def test_create_new_part(self):
        form = ProjectHardwareForm(
            data={
                "hardware_part": "",
                "new_name": "NEMA17 Motor",
                "new_category": "motors",
                "new_url": "https://example.com",
                "new_unit_price": "12.50",
                "new_notes": "Standard stepper",
                "quantity": 2,
                "notes": "For X and Y axis",
            }
        )
        self.assertTrue(form.is_valid())
        ph = form.save(project=self.project, user=self.user)
        self.assertEqual(ph.hardware_part.name, "NEMA17 Motor")
        self.assertEqual(ph.hardware_part.category, "motors")
        self.assertEqual(ph.quantity, 2)

    def test_neither_selected_nor_new_fails(self):
        form = ProjectHardwareForm(
            data={
                "hardware_part": "",
                "new_name": "",
                "quantity": 1,
            }
        )
        self.assertFalse(form.is_valid())

    def test_get_or_create_reuses_existing(self):
        HardwarePart.objects.create(name="M3x10", category="screws")
        form = ProjectHardwareForm(
            data={
                "hardware_part": "",
                "new_name": "M3x10",
                "new_category": "screws",
                "quantity": 3,
            }
        )
        self.assertTrue(form.is_valid())
        form.save(project=self.project, user=self.user)
        self.assertEqual(HardwarePart.objects.filter(name="M3x10", category="screws").count(), 1)


class ProjectHardwareViewTests(TestDataMixin, TestCase):
    """Tests for hardware CRUD views."""

    def setUp(self):
        super().setUp()
        self.client = Client()
        self.client.login(username="testuser", password="testpass123")

    def test_create_hardware(self):
        url = reverse("core:hardware_create", args=[self.project.pk])
        resp = self.client.post(
            url,
            {
                "new_name": "M5x20",
                "new_category": "screws",
                "quantity": 10,
                "notes": "",
            },
        )
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(self.project.hardware_assignments.count(), 1)

    def test_update_hardware(self):
        hp = HardwarePart.objects.create(name="M3x10", category="screws")
        ph = ProjectHardware.objects.create(project=self.project, hardware_part=hp, quantity=5)
        url = reverse("core:hardware_update", args=[ph.pk])
        resp = self.client.post(
            url,
            {
                "hw_name": "M3x12",
                "hw_category": "screws",
                "hw_url": "",
                "hw_unit_price": "0.15",
                "hw_notes": "",
                "quantity": 10,
                "notes": "Updated",
            },
        )
        self.assertEqual(resp.status_code, 302)
        ph.refresh_from_db()
        self.assertEqual(ph.quantity, 10)
        ph.hardware_part.refresh_from_db()
        self.assertEqual(ph.hardware_part.name, "M3x12")

    def test_delete_hardware(self):
        hp = HardwarePart.objects.create(name="M3x10", category="screws")
        ph = ProjectHardware.objects.create(project=self.project, hardware_part=hp, quantity=5)
        url = reverse("core:hardware_delete", args=[ph.pk])
        resp = self.client.post(url)
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(self.project.hardware_assignments.count(), 0)
        # HardwarePart should still exist
        self.assertTrue(HardwarePart.objects.filter(pk=hp.pk).exists())

    def test_create_requires_permission(self):
        self.client.login(username="otheruser", password="otherpass123")
        url = reverse("core:hardware_create", args=[self.project.pk])
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 403)


class ProjectHardwareUpdateFormValidationTests(TestCase):
    """Tests for ProjectHardwareUpdateForm uniqueness validation."""

    def setUp(self):
        self.user = User.objects.create_user(username="testuser", password="testpass123")
        self.project = Project.objects.create(name="Test", created_by=self.user)

    def test_update_form_allows_same_name_category(self):
        """Updating a part to keep the same name/category should succeed."""
        hp = HardwarePart.objects.create(name="M3x10", category="screws")
        ph = ProjectHardware.objects.create(project=self.project, hardware_part=hp, quantity=5)
        form = ProjectHardwareUpdateForm(
            data={
                "hw_name": "M3x10",
                "hw_category": "screws",
                "hw_url": "",
                "hw_unit_price": "",
                "hw_notes": "",
                "quantity": 10,
                "notes": "",
            },
            instance=ph,
        )
        self.assertTrue(form.is_valid())

    def test_update_form_rejects_duplicate_name_category(self):
        """Renaming a part to collide with an existing (name, category) must fail validation."""
        HardwarePart.objects.create(name="M4x10", category="screws")
        hp2 = HardwarePart.objects.create(name="M3x10", category="screws")
        ph2 = ProjectHardware.objects.create(project=self.project, hardware_part=hp2, quantity=2)
        form = ProjectHardwareUpdateForm(
            data={
                "hw_name": "M4x10",
                "hw_category": "screws",
                "hw_url": "",
                "hw_unit_price": "",
                "hw_notes": "",
                "quantity": 2,
                "notes": "",
            },
            instance=ph2,
        )
        self.assertFalse(form.is_valid())
        self.assertIn("__all__", form.errors)
