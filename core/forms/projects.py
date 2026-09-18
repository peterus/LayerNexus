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
        """Normalise quantity and reject cyclic re-parenting.

        A project may not be re-parented under itself or any of its
        descendants — doing so would create a cycle that makes the recursive
        aggregate properties recurse endlessly. This mirrors the model-level
        :meth:`Project.clean` guard so the error surfaces as a field error on
        the form even though ``ModelForm`` does not run the cycle check for the
        ``parent`` field automatically.
        """
        cleaned_data = super().clean()
        parent = cleaned_data.get("parent")
        if not parent:
            cleaned_data["quantity"] = 1
        elif self.instance.pk and (parent.pk == self.instance.pk or parent.pk in self.instance.get_descendant_ids()):
            self.add_error(
                "parent",
                "A project cannot be a sub-project of itself or one of its descendants.",
            )
        return cleaned_data
