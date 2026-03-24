"""OrcaSlicer profile import forms."""

import json

from django import forms

__all__ = [
    "OrcaFilamentProfileImportForm",
    "OrcaPrintPresetImportForm",
    "OrcaMachineProfileImportForm",
]


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
