"""OrcaSlicer profile import forms."""

import json

from django import forms

__all__ = [
    "OrcaProfileImportFormBase",
    "OrcaFilamentProfileImportForm",
    "OrcaPrintPresetImportForm",
    "OrcaMachineProfileImportForm",
]


class OrcaProfileImportFormBase(forms.Form):
    """Base form for importing OrcaSlicer profile JSON files.

    Subclasses must set ``expected_type`` to the profile type string
    (e.g. ``"filament"``, ``"process"``, ``"machine"``).  The
    ``type_label`` attribute is used in validation error messages.
    """

    expected_type: str | None = None
    type_label: str = "profile"

    profile_file = forms.FileField(
        label="Profile JSON file",
        help_text="Upload an OrcaSlicer profile JSON file.",
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
                not the expected profile type.
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

        if self.expected_type:
            profile_type = data.get("type", "")
            if profile_type and profile_type != self.expected_type:
                raise forms.ValidationError(
                    f"Expected profile type '{self.expected_type}', got '{profile_type}'. "
                    f"This does not appear to be a {self.type_label}."
                )

        return data


class OrcaFilamentProfileImportForm(OrcaProfileImportFormBase):
    """Form for importing an OrcaSlicer filament profile JSON file."""

    expected_type = "filament"
    type_label = "filament profile"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["profile_file"].help_text = (
            "Upload an OrcaSlicer filament profile JSON file."
        )


class OrcaPrintPresetImportForm(OrcaProfileImportFormBase):
    """Form for importing an OrcaSlicer process (print preset) profile JSON file."""

    expected_type = "process"
    type_label = "process/print preset profile"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["profile_file"].help_text = (
            "Upload an OrcaSlicer process profile JSON file."
        )


class OrcaMachineProfileImportForm(OrcaProfileImportFormBase):
    """Form for importing an OrcaSlicer machine profile JSON file."""

    expected_type = "machine"
    type_label = "machine/printer profile"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["profile_file"].help_text = (
            "Upload an OrcaSlicer machine profile JSON file."
        )
