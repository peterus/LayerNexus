"""Part-related forms."""

from django import forms
from django.core.files.uploadedfile import UploadedFile

from core.models import Part, Project, ProjectPart

__all__ = [
    "PartForm",
    "AddPartToProjectForm",
    "ProjectPartQuantityForm",
]


class PartForm(forms.ModelForm):
    """Form for creating and updating parts with STL file upload."""

    class Meta:
        model = Part
        fields = [
            "name",
            "stl_file",
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


class AddPartToProjectForm(forms.ModelForm):
    """Add an existing reusable part into ``project`` (composition edge).

    The ``part`` choices exclude parts already linked to this project so the same
    building block is not added twice; the model's unique ``(project, part)``
    constraint is the backstop.
    """

    class Meta:
        model = ProjectPart
        fields = ["part", "quantity"]
        widgets = {
            "part": forms.Select(attrs={"class": "form-select"}),
            "quantity": forms.NumberInput(attrs={"class": "form-control", "min": 1}),
        }

    def __init__(self, *args, project: Project, **kwargs) -> None:
        """Restrict the part queryset to parts not yet linked to ``project``.

        Args:
            project: The module/assembly the part is being added to.
        """
        super().__init__(*args, **kwargs)
        self.project = project
        existing = set(project.part_links.values_list("part_id", flat=True))
        self.fields["part"].queryset = Part.objects.exclude(pk__in=existing).order_by("name")

    def save(self, commit: bool = True) -> ProjectPart:
        """Attach the edge to ``project`` before saving."""
        self.instance.project = self.project
        return super().save(commit=commit)


class ProjectPartQuantityForm(forms.ModelForm):
    """Edit only the ``quantity`` of an existing ``ProjectPart`` edge."""

    class Meta:
        model = ProjectPart
        fields = ["quantity"]
        widgets = {
            "quantity": forms.NumberInput(attrs={"class": "form-control", "min": 1}),
        }
