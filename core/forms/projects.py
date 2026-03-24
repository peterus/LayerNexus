"""Project-related forms."""

from django import forms

from core.models import Project

__all__ = [
    "ProjectForm",
    "SubProjectForm",
    "ProjectEditForm",
]


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
