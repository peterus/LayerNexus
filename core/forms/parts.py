"""Part-related forms."""

from django import forms
from django.core.files.uploadedfile import UploadedFile

from core.forms.projects import path_sorted_choices
from core.models import Part, PrintedPartRecord, Project

__all__ = [
    "PartForm",
    "PartCopyForm",
    "AddExistingPartForm",
    "PrintedPartRecordForm",
]


class PartForm(forms.ModelForm):
    """Form for creating and updating parts with STL file upload.

    On update the form also offers ``project`` to move the part; on create the
    project comes from the URL.
    """

    class Meta:
        model = Part
        fields = [
            "name",
            "project",
            "quantity",
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

    def __init__(self, *args, labels: dict[int, str] | None = None, **kwargs) -> None:
        """Offer every project (labelled by tree path) on update; drop the field on create.

        Args:
            labels: Precomputed :meth:`Project.path_label_map`, to avoid a second scan.
        """
        super().__init__(*args, **kwargs)
        if self.instance.pk is None:
            del self.fields["project"]
            return
        field = self.fields["project"]
        # Required unless omitted from the POST entirely (see clean_project).
        field.required = False
        field.label = "Project"
        field.help_text = "Move the part into another project. Its print history and files move with it."
        field.queryset = Project.objects.order_by("name")
        # Path labels are also needed when an invalid POST re-renders the form.
        labels = Project.path_label_map() if labels is None else labels
        field.choices = path_sorted_choices(
            field.queryset.only("pk", "name"), lambda p: labels.get(p.pk, p.name), field.empty_label
        )

    def clean_project(self) -> Project:
        """Keep the current project when the POST has no ``project`` key; an empty value is an error.

        Returns:
            The selected project, or the part's current project if the field was not submitted.

        Raises:
            forms.ValidationError: If ``project`` was submitted empty.
        """
        project = self.cleaned_data.get("project")
        if project is not None:
            return project
        if self.add_prefix("project") not in self.data:
            return self.instance.project
        raise forms.ValidationError(self.fields["project"].error_messages["required"], code="required")

    def clean_stl_file(self) -> UploadedFile | None:
        """Validate uploaded model file (STL or 3MF) for size and extension.

        Returns:
            The validated file or None.

        Raises:
            forms.ValidationError: If file extension is invalid or size exceeds 100 MB.
        """
        stl_file = self.cleaned_data.get("stl_file")
        # Only a new upload is checked; the already stored file is kept as it is.
        if isinstance(stl_file, UploadedFile):
            if not stl_file.name.lower().endswith((".stl", ".3mf")):
                raise forms.ValidationError("Only STL and 3MF files are allowed.")
            if stl_file.size > 100 * 1024 * 1024:  # 100 MB limit
                raise forms.ValidationError("File size must be under 100 MB.")
        return stl_file

    def clean(self) -> dict:
        """Auto-fill name from the uploaded model filename if left empty."""
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
                self.add_error("name", "Name is required when no model file is uploaded.")
        return cleaned_data


class PartCopyForm(forms.Form):
    """Copy a part into a project (its own project by default)."""

    project = forms.ModelChoiceField(queryset=Project.objects.none(), label="Copy into project")

    def __init__(self, *args, source: Part, **kwargs) -> None:
        """Offer every project, preselecting the source part's project.

        Args:
            source: The part being copied.
        """
        super().__init__(*args, **kwargs)
        field = self.fields["project"]
        field.queryset = Project.objects.order_by("name")
        field.initial = source.project_id
        labels = Project.path_label_map()
        field.choices = path_sorted_choices(
            field.queryset.only("pk", "name"), lambda p: labels.get(p.pk, p.name), field.empty_label
        )


class AddExistingPartForm(forms.Form):
    """Pick any existing part to deep-copy into the current project."""

    part = forms.ModelChoiceField(queryset=Part.objects.none(), label="Part to copy")

    def __init__(self, *args, labels: dict[int, str] | None = None, **kwargs) -> None:
        """Offer every part, labelled ``"<project path> › <part name>"``.

        Args:
            labels: Precomputed :meth:`Project.path_label_map`, to avoid a second scan.
        """
        super().__init__(*args, **kwargs)
        field = self.fields["part"]
        field.queryset = Part.objects.order_by("name")
        if self.is_bound:
            # Only POSTed to validate one PK, never re-rendered: skip building the picker.
            return
        labels = Project.path_label_map() if labels is None else labels
        field.choices = path_sorted_choices(
            field.queryset.only("pk", "name", "project_id"),
            lambda part: " › ".join(filter(None, [labels.get(part.project_id), part.name])),
            field.empty_label,
        )


class PrintedPartRecordForm(forms.ModelForm):
    """Book printed copies of a part manually (quantity ≥ 1, date, optional note)."""

    class Meta:
        model = PrintedPartRecord
        fields = ["quantity", "printed_on", "note"]
        widgets = {
            "quantity": forms.NumberInput(attrs={"class": "form-control form-control-sm", "min": 1}),
            "printed_on": forms.DateInput(
                attrs={"class": "form-control form-control-sm", "type": "date"}, format="%Y-%m-%d"
            ),
            "note": forms.TextInput(attrs={"class": "form-control form-control-sm", "placeholder": "Note (optional)"}),
        }
