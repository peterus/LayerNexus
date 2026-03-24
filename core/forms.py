"""Forms for the LayerNexus application."""

import json
from typing import Any

from django import forms
from django.contrib.auth.forms import UserCreationForm
from django.contrib.auth.models import Group, User
from django.core.files.uploadedfile import UploadedFile

from .models import (
    CostProfile,
    HardwarePart,
    Part,
    PrinterProfile,
    PrintJob,
    PrintQueue,
    Project,
    ProjectDocument,
    ProjectHardware,
)


class ProjectForm(forms.ModelForm):
    """Form for creating and updating projects."""

    class Meta:
        model = Project
        fields = ["name", "description", "image", "default_print_preset"]
        widgets = {
            "description": forms.Textarea(attrs={"rows": 4}),
        }


class SubProjectForm(forms.ModelForm):
    """Form for creating and updating sub-projects (includes quantity field)."""

    class Meta:
        model = Project
        fields = ["name", "description", "image", "quantity", "default_print_preset"]
        widgets = {
            "description": forms.Textarea(attrs={"rows": 4}),
        }


class ProjectEditForm(forms.ModelForm):
    """Form for editing projects with optional parent (re-parenting support).

    Includes the ``parent`` field so that an existing top-level project can
    be turned into a sub-project and vice-versa.  The ``quantity`` field is
    shown so it can be adjusted when a parent is set.
    """

    class Meta:
        model = Project
        fields = ["name", "description", "image", "parent", "quantity", "default_print_preset"]
        widgets = {
            "description": forms.Textarea(attrs={"rows": 4}),
        }

    def clean(self) -> dict:
        """Ensure quantity is 1 when no parent is set."""
        cleaned_data = super().clean()
        parent = cleaned_data.get("parent")
        if not parent:
            cleaned_data["quantity"] = 1
        return cleaned_data


class PartForm(forms.ModelForm):
    """Form for creating and updating parts with STL file upload."""

    class Meta:
        model = Part
        fields = [
            "name",
            "stl_file",
            "quantity",
            "spoolman_filament_id",
            "color",
            "material",
            "print_preset",
            "notes",
        ]
        widgets = {
            "notes": forms.Textarea(attrs={"rows": 3}),
            "spoolman_filament_id": forms.Select(),
        }

    def clean_stl_file(self) -> UploadedFile | None:
        """Validate uploaded STL file for size and extension.

        Returns:
            The validated STL file or None.

        Raises:
            forms.ValidationError: If file extension is invalid or size exceeds 100 MB.
        """
        stl_file = self.cleaned_data.get("stl_file")
        if stl_file:
            if not stl_file.name.lower().endswith(".stl"):
                raise forms.ValidationError("Only STL files are allowed.")
            if stl_file.size > 100 * 1024 * 1024:  # 100 MB limit
                raise forms.ValidationError("File size must be under 100 MB.")
        return stl_file

    def clean(self) -> dict:
        """Auto-fill name from the uploaded STL filename if left empty."""
        cleaned_data = super().clean()
        name = cleaned_data.get("name", "").strip()
        if not name:
            stl_file = cleaned_data.get("stl_file")
            if stl_file and hasattr(stl_file, "name"):
                # Strip extension: "My_Part_v2.stl" → "My_Part_v2"
                from pathlib import PurePosixPath

                cleaned_data["name"] = PurePosixPath(stl_file.name).stem
            elif self.instance and self.instance.stl_file:
                cleaned_data["name"] = PurePosixPath(self.instance.stl_file.name).stem
            else:
                self.add_error("name", "Name is required when no STL file is uploaded.")
        return cleaned_data


class PrinterProfileForm(forms.ModelForm):
    """Form for creating and updating printer profiles."""

    class Meta:
        model = PrinterProfile
        fields = [
            "name",
            "description",
            "orca_machine_profile",
            "moonraker_url",
            "moonraker_api_key",
        ]
        widgets = {
            "description": forms.Textarea(attrs={"rows": 3}),
            "moonraker_api_key": forms.PasswordInput(render_value=True),
        }


class PrintJobForm(forms.ModelForm):
    """Form for creating a new print job (draft).

    The user provides a name, machine profile, and optional notes.
    Parts are added separately after the job is created.
    """

    class Meta:
        model = PrintJob
        fields = [
            "name",
            "machine_profile",
            "notes",
        ]
        widgets = {
            "notes": forms.Textarea(attrs={"rows": 3}),
        }


class PrintJobUpdateForm(forms.ModelForm):
    """Form for editing existing print jobs."""

    class Meta:
        model = PrintJob
        fields = [
            "name",
            "machine_profile",
            "notes",
        ]
        widgets = {
            "notes": forms.Textarea(attrs={"rows": 3}),
        }


class AddPartToJobForm(forms.Form):
    """Form for adding a part to a print job.

    Used on the Part detail page — the user selects an existing draft
    job or chooses to create a new one.
    """

    job = forms.ModelChoiceField(
        queryset=PrintJob.objects.none(),
        required=False,
        empty_label="— Create new job —",
        label="Print Job",
        help_text="Select an existing draft job or create a new one.",
    )
    quantity = forms.IntegerField(
        min_value=1,
        initial=1,
        label="Quantity",
        help_text="How many copies of this part to add.",
    )

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        """Initialise with current user's draft jobs."""
        user = kwargs.pop("user", None)
        super().__init__(*args, **kwargs)
        if user:
            self.fields["job"].queryset = PrintJob.objects.filter(
                status=PrintJob.STATUS_DRAFT,
                created_by=user,
            ).order_by("-created_at")


class OrcaFilamentProfileImportForm(forms.Form):
    """Form for importing an OrcaSlicer filament profile JSON file.

    Parses the uploaded JSON and validates it contains the required
    'name' field and is of type 'filament' (if type is specified).
    """

    profile_file = forms.FileField(
        label="Profile JSON file",
        help_text="Upload an OrcaSlicer filament profile JSON file.",
        widget=forms.ClearableFileInput(attrs={"accept": ".json"}),
    )
    display_name = forms.CharField(
        max_length=255,
        required=False,
        label="Display name (optional)",
        help_text="Override the display name. Leave empty to use the profile's 'name' field.",
    )

    def clean_profile_file(self) -> dict:
        """Validate and parse the uploaded JSON file.

        Returns:
            Parsed JSON data as a dictionary.

        Raises:
            forms.ValidationError: If the file is not valid JSON or
                not a filament profile.
        """
        upload = self.cleaned_data.get("profile_file")
        if not upload:
            return {}

        if not upload.name.lower().endswith(".json"):
            raise forms.ValidationError("Only JSON files are allowed.")

        if upload.size > 5 * 1024 * 1024:  # 5 MB limit
            raise forms.ValidationError("File size must be under 5 MB.")

        try:
            content = upload.read().decode("utf-8")
            data = json.loads(content)
        except UnicodeDecodeError as exc:
            raise forms.ValidationError("File is not valid UTF-8 text.") from exc
        except json.JSONDecodeError as exc:
            raise forms.ValidationError(f"Invalid JSON: {exc}") from exc

        if not isinstance(data, dict):
            raise forms.ValidationError("Profile JSON must be a JSON object (dict).")

        if not data.get("name"):
            raise forms.ValidationError("Profile JSON is missing the required 'name' field.")

        profile_type = data.get("type", "")
        if profile_type and profile_type != "filament":
            raise forms.ValidationError(
                f"Expected profile type 'filament', got '{profile_type}'. "
                f"This does not appear to be a filament profile."
            )

        return data


class OrcaPrintPresetImportForm(forms.Form):
    """Form for importing an OrcaSlicer process (print preset) profile JSON file.

    Parses the uploaded JSON and validates it contains the required
    'name' field and is of type 'process' (if type is specified).
    """

    profile_file = forms.FileField(
        label="Profile JSON file",
        help_text="Upload an OrcaSlicer process profile JSON file.",
        widget=forms.ClearableFileInput(attrs={"accept": ".json"}),
    )
    display_name = forms.CharField(
        max_length=255,
        required=False,
        label="Display name (optional)",
        help_text="Override the display name. Leave empty to use the profile's 'name' field.",
    )

    def clean_profile_file(self) -> dict:
        """Validate and parse the uploaded JSON file.

        Returns:
            Parsed JSON data as a dictionary.

        Raises:
            forms.ValidationError: If the file is not valid JSON or
                not a process profile.
        """
        upload = self.cleaned_data.get("profile_file")
        if not upload:
            return {}

        if not upload.name.lower().endswith(".json"):
            raise forms.ValidationError("Only JSON files are allowed.")

        if upload.size > 5 * 1024 * 1024:  # 5 MB limit
            raise forms.ValidationError("File size must be under 5 MB.")

        try:
            content = upload.read().decode("utf-8")
            data = json.loads(content)
        except UnicodeDecodeError as exc:
            raise forms.ValidationError("File is not valid UTF-8 text.") from exc
        except json.JSONDecodeError as exc:
            raise forms.ValidationError(f"Invalid JSON: {exc}") from exc

        if not isinstance(data, dict):
            raise forms.ValidationError("Profile JSON must be a JSON object (dict).")

        if not data.get("name"):
            raise forms.ValidationError("Profile JSON is missing the required 'name' field.")

        profile_type = data.get("type", "")
        if profile_type and profile_type != "process":
            raise forms.ValidationError(
                f"Expected profile type 'process', got '{profile_type}'. "
                f"This does not appear to be a process/print preset profile."
            )

        return data


class UserRegistrationForm(UserCreationForm):
    """Extended user registration form with email field."""

    email = forms.EmailField(required=True)

    class Meta:
        model = User
        fields = ["username", "email", "password1", "password2"]


class UserManagementForm(forms.ModelForm):
    """Admin form for creating/editing users with role assignment.

    Allows assigning users to one of the three role groups:
    Admin, Operator, or Designer.
    """

    ROLE_CHOICES = [
        ("", "— Select a role —"),
        ("Admin", "Admin"),
        ("Operator", "Operator"),
        ("Designer", "Designer"),
    ]

    role = forms.ChoiceField(
        choices=ROLE_CHOICES,
        required=True,
        label="Role",
        help_text="Determines what the user can access.",
    )
    password1 = forms.CharField(
        label="Password",
        widget=forms.PasswordInput,
        required=False,
        help_text="Leave empty to keep current password (edit only).",
    )
    password2 = forms.CharField(
        label="Confirm Password",
        widget=forms.PasswordInput,
        required=False,
    )

    class Meta:
        model = User
        fields = ["username", "email", "first_name", "last_name", "is_active"]

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        """Pre-fill role from existing group membership."""
        super().__init__(*args, **kwargs)
        if self.instance and self.instance.pk:
            groups = self.instance.groups.values_list("name", flat=True)
            for role_name in ("Admin", "Operator", "Designer"):
                if role_name in groups:
                    self.fields["role"].initial = role_name
                    break

    def clean(self) -> dict[str, Any]:
        """Validate password pair matches."""
        cleaned = super().clean()
        pw1 = cleaned.get("password1", "")
        pw2 = cleaned.get("password2", "")
        if pw1 or pw2:
            if pw1 != pw2:
                raise forms.ValidationError("Passwords do not match.")
        elif not self.instance.pk:
            raise forms.ValidationError("Password is required for new users.")
        return cleaned

    def save(self, commit: bool = True) -> User:
        """Save user and assign to the selected role group."""
        user = super().save(commit=False)
        pw = self.cleaned_data.get("password1")
        if pw:
            user.set_password(pw)
        role_name = self.cleaned_data["role"]
        if role_name == "Admin":
            user.is_staff = True
            user.is_superuser = True
        else:
            user.is_staff = False
            user.is_superuser = False
        if commit:
            user.save()
            # Clear existing role groups and assign selected
            role_groups = Group.objects.filter(name__in=["Admin", "Operator", "Designer"])
            user.groups.remove(*role_groups)
            group = Group.objects.get(name=role_name)
            user.groups.add(group)
        return user


class ProfileUpdateForm(forms.ModelForm):
    """Form for users to update their own profile."""

    class Meta:
        model = User
        fields = ["first_name", "last_name", "email"]


class CostProfileForm(forms.ModelForm):
    """Form for creating and updating cost profiles."""

    class Meta:
        model = CostProfile
        fields = [
            "electricity_cost_per_kwh",
            "printer_power_watts",
            "printer_purchase_cost",
            "printer_lifespan_hours",
            "maintenance_cost_per_hour",
        ]


class PrintQueueForm(forms.ModelForm):
    """Form for adding a plate to the queue.

    Only plates from sliced jobs can be queued.  The printer must have
    a machine profile matching the job's machine profile.
    """

    class Meta:
        model = PrintQueue
        fields = ["plate", "printer", "priority"]

    def clean(self) -> dict[str, Any]:
        """Validate that the selected printer is compatible with the job.

        Returns:
            Cleaned form data.

        Raises:
            forms.ValidationError: If the printer's machine profile does not
                match the job's machine profile.
        """
        cleaned = super().clean()
        plate = cleaned.get("plate")
        printer = cleaned.get("printer")
        if plate and printer:
            job_mp = plate.print_job.machine_profile
            printer_mp = printer.orca_machine_profile
            if job_mp and printer_mp and job_mp.pk != printer_mp.pk:
                raise forms.ValidationError(
                    f"Printer '{printer.name}' uses machine profile "
                    f"'{printer_mp.name}', but this job was sliced for "
                    f"'{job_mp.name}'. Please select a compatible printer."
                )
        return cleaned


class OrcaMachineProfileImportForm(forms.Form):
    """Form for importing an OrcaSlicer machine profile JSON file.

    Parses the uploaded JSON and validates it contains the required
    'name' field and is of type 'machine' (if type is specified).
    """

    profile_file = forms.FileField(
        label="Profile JSON file",
        help_text="Upload an OrcaSlicer machine profile JSON file.",
        widget=forms.ClearableFileInput(attrs={"accept": ".json"}),
    )
    display_name = forms.CharField(
        max_length=255,
        required=False,
        label="Display name (optional)",
        help_text="Override the display name. Leave empty to use the profile's 'name' field.",
    )

    def clean_profile_file(self) -> dict:
        """Validate and parse the uploaded JSON file.

        Returns:
            Parsed JSON data as a dictionary.

        Raises:
            forms.ValidationError: If the file is not valid JSON or
                not a machine profile.
        """
        upload = self.cleaned_data.get("profile_file")
        if not upload:
            return {}

        if not upload.name.lower().endswith(".json"):
            raise forms.ValidationError("Only JSON files are allowed.")

        if upload.size > 5 * 1024 * 1024:  # 5 MB limit
            raise forms.ValidationError("File size must be under 5 MB.")

        try:
            content = upload.read().decode("utf-8")
            data = json.loads(content)
        except UnicodeDecodeError as exc:
            raise forms.ValidationError("File is not valid UTF-8 text.") from exc
        except json.JSONDecodeError as exc:
            raise forms.ValidationError(f"Invalid JSON: {exc}") from exc

        if not isinstance(data, dict):
            raise forms.ValidationError("Profile JSON must be a JSON object (dict).")

        if not data.get("name"):
            raise forms.ValidationError("Profile JSON is missing the required 'name' field.")

        profile_type = data.get("type", "")
        if profile_type and profile_type != "machine":
            raise forms.ValidationError(
                f"Expected profile type 'machine', got '{profile_type}'. "
                f"This does not appear to be a machine/printer profile."
            )

        return data


# ------------------------------------------------------------------
# Project Document & Hardware forms
# ------------------------------------------------------------------

ALLOWED_DOCUMENT_EXTENSIONS = {
    ".pdf",
    ".txt",
    ".md",
    ".png",
    ".jpg",
    ".jpeg",
    ".svg",
    ".step",
    ".dxf",
}

MAX_DOCUMENT_SIZE = 75 * 1024 * 1024  # 75 MB


class ProjectDocumentForm(forms.ModelForm):
    """Form for uploading a document to a project."""

    class Meta:
        model = ProjectDocument
        fields = ["name", "file"]

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.fields["name"].required = False

    def clean_file(self) -> UploadedFile | None:
        """Validate uploaded document for extension and size.

        Returns:
            The validated file.

        Raises:
            forms.ValidationError: If extension is not allowed or size exceeds 75 MB.
        """
        uploaded = self.cleaned_data.get("file")
        if uploaded:
            import os

            _, ext = os.path.splitext(uploaded.name)
            if ext.lower() not in ALLOWED_DOCUMENT_EXTENSIONS:
                allowed = ", ".join(sorted(ALLOWED_DOCUMENT_EXTENSIONS))
                raise forms.ValidationError(f"File type '{ext}' is not allowed. Allowed types: {allowed}")
            if uploaded.size > MAX_DOCUMENT_SIZE:
                raise forms.ValidationError("File size must be under 75 MB.")
        return uploaded

    def clean(self) -> dict:
        """Auto-fill name from the uploaded filename if left empty."""
        cleaned_data = super().clean()
        name = cleaned_data.get("name", "").strip()
        if not name:
            uploaded = cleaned_data.get("file")
            if uploaded and hasattr(uploaded, "name"):
                from pathlib import PurePosixPath

                cleaned_data["name"] = PurePosixPath(uploaded.name).stem
            else:
                self.add_error("name", "Name is required when no file is uploaded.")
        return cleaned_data


class ProjectHardwareForm(forms.Form):
    """Form for adding a hardware part to a project.

    The user can either select an existing :class:`HardwarePart` from
    the dropdown or fill in the ``new_*`` fields to create a new one.
    """

    hardware_part = forms.ModelChoiceField(
        queryset=HardwarePart.objects.all(),
        required=False,
        label="Existing hardware part",
        widget=forms.Select(attrs={"class": "form-select"}),
    )

    new_name = forms.CharField(
        max_length=255,
        required=False,
        label="Name",
    )
    new_category = forms.ChoiceField(
        choices=[("", "---------")] + list(HardwarePart.CATEGORY_CHOICES),
        required=False,
        label="Category",
        widget=forms.Select(attrs={"class": "form-select"}),
    )
    new_url = forms.URLField(
        required=False,
        label="URL",
        widget=forms.URLInput(attrs={"placeholder": "https://..."}),
    )
    new_unit_price = forms.DecimalField(
        max_digits=10,
        decimal_places=2,
        required=False,
        label="Unit price",
        widget=forms.NumberInput(attrs={"step": "0.01", "min": "0"}),
    )
    new_notes = forms.CharField(
        required=False,
        label="Notes (hardware part)",
        widget=forms.Textarea(attrs={"rows": 2}),
    )

    quantity = forms.IntegerField(
        min_value=1,
        initial=1,
        label="Quantity",
    )
    notes = forms.CharField(
        required=False,
        label="Notes (project-specific)",
        widget=forms.Textarea(attrs={"rows": 2}),
    )

    def clean(self) -> dict:
        """Ensure either an existing part is selected or new_name + new_category are provided."""
        cleaned_data = super().clean()
        hw_part = cleaned_data.get("hardware_part")
        new_name = cleaned_data.get("new_name", "").strip()
        new_category = cleaned_data.get("new_category", "").strip()

        if not hw_part and not new_name:
            raise forms.ValidationError("Select an existing hardware part or provide a name for a new one.")
        if not hw_part and not new_category:
            self.add_error("new_category", "Category is required for new hardware parts.")

        return cleaned_data

    def save(self, project: Project, user: Any = None) -> ProjectHardware:
        """Create or retrieve the HardwarePart and link it to the project.

        Args:
            project: The project to assign the hardware to.
            user: The current user (set as created_by on new HardwareParts).

        Returns:
            The created ProjectHardware instance.
        """
        hw_part = self.cleaned_data.get("hardware_part")

        if not hw_part:
            hw_part, _created = HardwarePart.objects.get_or_create(
                name=self.cleaned_data["new_name"].strip(),
                category=self.cleaned_data["new_category"],
                defaults={
                    "url": self.cleaned_data.get("new_url") or "",
                    "unit_price": self.cleaned_data.get("new_unit_price"),
                    "notes": self.cleaned_data.get("new_notes", ""),
                    "created_by": user,
                },
            )

        return ProjectHardware.objects.create(
            project=project,
            hardware_part=hw_part,
            quantity=self.cleaned_data["quantity"],
            notes=self.cleaned_data.get("notes", ""),
        )


class ProjectHardwareUpdateForm(forms.ModelForm):
    """Form for editing a hardware assignment's quantity and notes.

    Also exposes the related :class:`HardwarePart` fields for inline
    editing (name, category, url, unit_price).
    """

    hw_name = forms.CharField(max_length=255, label="Name")
    hw_category = forms.ChoiceField(
        choices=HardwarePart.CATEGORY_CHOICES,
        label="Category",
        widget=forms.Select(attrs={"class": "form-select"}),
    )
    hw_url = forms.URLField(required=False, label="URL")
    hw_unit_price = forms.DecimalField(
        max_digits=10,
        decimal_places=2,
        required=False,
        label="Unit price",
        widget=forms.NumberInput(attrs={"step": "0.01", "min": "0"}),
    )
    hw_notes = forms.CharField(
        required=False,
        label="Hardware notes",
        widget=forms.Textarea(attrs={"rows": 2}),
    )

    class Meta:
        model = ProjectHardware
        fields = ["quantity", "notes"]
        widgets = {
            "notes": forms.Textarea(attrs={"rows": 2}),
        }

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        if self.instance and self.instance.pk:
            hp = self.instance.hardware_part
            self.fields["hw_name"].initial = hp.name
            self.fields["hw_category"].initial = hp.category
            self.fields["hw_url"].initial = hp.url
            self.fields["hw_unit_price"].initial = hp.unit_price
            self.fields["hw_notes"].initial = hp.notes

    field_order = [
        "hw_name",
        "hw_category",
        "hw_url",
        "hw_unit_price",
        "hw_notes",
        "quantity",
        "notes",
    ]

    def clean(self) -> dict:
        """Validate that the new (name, category) combination is unique for HardwarePart."""
        cleaned_data = super().clean()
        name = cleaned_data.get("hw_name")
        category = cleaned_data.get("hw_category")
        if name and category and self.instance and self.instance.pk:
            hp = self.instance.hardware_part
            if HardwarePart.objects.filter(name=name, category=category).exclude(pk=hp.pk).exists():
                raise forms.ValidationError(
                    f"A hardware part named '{name}' with category '{category}' already exists. "
                    "Please use a different name or category."
                )
        return cleaned_data

    def save(self, commit: bool = True) -> ProjectHardware:
        """Save the ProjectHardware and update the related HardwarePart."""
        instance = super().save(commit=False)
        hp = instance.hardware_part
        hp.name = self.cleaned_data["hw_name"]
        hp.category = self.cleaned_data["hw_category"]
        hp.url = self.cleaned_data.get("hw_url") or ""
        hp.unit_price = self.cleaned_data.get("hw_unit_price")
        hp.notes = self.cleaned_data.get("hw_notes") or ""
        if commit:
            hp.save()
            instance.save()
        return instance
