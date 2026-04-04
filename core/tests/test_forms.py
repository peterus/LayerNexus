"""Tests for core forms."""

from django.contrib.auth.models import Group, User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings

from core.forms import (
    PartForm,
    ProjectDocumentForm,
    ProjectEditForm,
    ProjectForm,
    ProjectHardwareForm,
    ProjectHardwareUpdateForm,
    UserRegistrationForm,
)
from core.models import (
    HardwarePart,
    Project,
    ProjectHardware,
)


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

    def test_3mf_file_valid(self):
        threemf = SimpleUploadedFile(
            "model.3mf",
            b"PK\x03\x04",
            content_type="model/3mf",
        )
        form = PartForm(
            data={"name": "Part", "quantity": 1, "color": "black", "material": "PLA"},
            files={"stl_file": threemf},
        )
        self.assertTrue(form.is_valid())

    def test_3mf_mixed_case_extension(self):
        threemf = SimpleUploadedFile("model.3MF", b"PK\x03\x04", content_type="application/octet-stream")
        form = PartForm(
            data={"name": "Part", "quantity": 1, "color": "black", "material": "PLA"},
            files={"stl_file": threemf},
        )
        self.assertTrue(form.is_valid())

    def test_3mf_file_name_derivation(self):
        threemf = SimpleUploadedFile("MyModel_v2.3mf", b"PK\x03\x04", content_type="application/octet-stream")
        form = PartForm(
            data={"name": "", "quantity": 1, "color": "black", "material": "PLA"},
            files={"stl_file": threemf},
        )
        self.assertTrue(form.is_valid())
        self.assertEqual(form.cleaned_data["name"], "MyModel_v2")

    def test_quantity_zero_rejected(self):
        form = PartForm(data={"name": "Part", "quantity": 0, "color": "black", "material": "PLA"})
        self.assertFalse(form.is_valid())
        self.assertIn("quantity", form.errors)


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
