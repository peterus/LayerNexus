"""Project-related forms."""

from django import forms

from core.models import Project

__all__ = [
    "ProjectForm",
    "SubProjectForm",
    "ProjectEditForm",
]

#: Help text for the optional preset on sub-projects.
INHERITED_PRESET_HELP = "Inherited from parent if empty."


class ProjectForm(forms.ModelForm):
    """Form for creating top-level projects."""

    class Meta:
        model = Project
        fields = ["name", "description", "image", "default_print_preset"]
        widgets = {
            "description": forms.Textarea(attrs={"rows": 4}),
        }

    def __init__(self, *args, **kwargs) -> None:
        """Make ``default_print_preset`` mandatory: top-level projects anchor preset inheritance."""
        super().__init__(*args, **kwargs)
        self.fields["default_print_preset"].required = True


class SubProjectForm(forms.ModelForm):
    """Form for creating sub-projects (includes the quantity within the parent)."""

    class Meta:
        model = Project
        fields = ["name", "description", "image", "quantity", "default_print_preset"]
        widgets = {
            "description": forms.Textarea(attrs={"rows": 4}),
        }

    def __init__(self, *args, **kwargs) -> None:
        """Make ``default_print_preset`` optional: sub-projects inherit it from the parent."""
        super().__init__(*args, **kwargs)
        self.fields["default_print_preset"].required = False
        self.fields["default_print_preset"].help_text = INHERITED_PRESET_HELP


class ProjectEditForm(forms.ModelForm):
    """Form for editing a project.

    Sub-projects additionally get their ``quantity`` within the parent and an
    optional preset (inherited from the parent if empty); on top-level projects
    the quantity is hidden and the preset is required.
    """

    class Meta:
        model = Project
        fields = ["name", "description", "image", "quantity", "default_print_preset"]
        widgets = {
            "description": forms.Textarea(attrs={"rows": 4}),
        }

    def __init__(self, *args, **kwargs) -> None:
        """Keep ``quantity`` and an optional preset only for sub-projects."""
        super().__init__(*args, **kwargs)
        if self.instance.parent_id is not None:
            self.fields["default_print_preset"].required = False
            self.fields["default_print_preset"].help_text = INHERITED_PRESET_HELP
        else:
            del self.fields["quantity"]
            self.fields["default_print_preset"].required = True
