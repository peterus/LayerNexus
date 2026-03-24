"""Part-related forms."""

from django import forms
from django.core.files.uploadedfile import UploadedFile

from core.models import Part

__all__ = [
    "PartForm",
]


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
                from pathlib import PurePosixPath

                cleaned_data["name"] = PurePosixPath(self.instance.stl_file.name).stem
            else:
                self.add_error("name", "Name is required when no STL file is uploaded.")
        return cleaned_data
