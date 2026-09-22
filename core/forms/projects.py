"""Project-related forms."""

from django import forms

from core.models import Project, ProjectComponent
from core.models.composition import component_would_create_cycle

__all__ = [
    "ProjectForm",
    "SubProjectForm",
    "ProjectEditForm",
    "AddComponentForm",
    "ProjectComponentQuantityForm",
]


class ProjectForm(forms.ModelForm):
    """Form for creating and updating projects."""

    class Meta:
        model = Project
        fields = ["name", "description", "image", "default_print_preset"]
        widgets = {
            "description": forms.Textarea(attrs={"rows": 4}),
        }

    def __init__(self, *args, **kwargs) -> None:
        """Make ``default_print_preset`` mandatory (Variant B: preset resolution needs it)."""
        super().__init__(*args, **kwargs)
        self.fields["default_print_preset"].required = True


class SubProjectForm(forms.ModelForm):
    """Form for creating and updating sub-projects (includes quantity field)."""

    class Meta:
        model = Project
        fields = ["name", "description", "image", "quantity", "default_print_preset"]
        widgets = {
            "description": forms.Textarea(attrs={"rows": 4}),
        }

    def __init__(self, *args, **kwargs) -> None:
        """Make ``default_print_preset`` mandatory (Variant B: preset resolution needs it)."""
        super().__init__(*args, **kwargs)
        self.fields["default_print_preset"].required = True


class ProjectEditForm(forms.ModelForm):
    """Form for editing a project's own attributes.

    Composition (which assemblies contain this project, and in what quantity)
    is edited exclusively through the ``ProjectComponent`` edge UI on the project
    detail page (add/remove module, edit edge quantity), never here. The legacy
    ``parent``/``quantity`` model fields are therefore deliberately **not**
    exposed: a single ``parent`` field cannot represent a module shared by
    several assemblies, and editing an edge-based module through it used to wipe
    that module's composition edges.
    """

    class Meta:
        model = Project
        fields = ["name", "description", "image", "default_print_preset"]
        widgets = {
            "description": forms.Textarea(attrs={"rows": 4}),
        }

    def __init__(self, *args, **kwargs) -> None:
        """Make ``default_print_preset`` mandatory (Variant B: preset resolution needs it)."""
        super().__init__(*args, **kwargs)
        self.fields["default_print_preset"].required = True


class AddComponentForm(forms.ModelForm):
    """Add an existing project/module as a child of ``parent_project`` (composition edge).

    The ``child_project`` choices exclude the parent itself, its existing children, and
    all its descendants so a user cannot introduce a cycle or a duplicate edge. ``clean``
    re-checks against :func:`component_would_create_cycle` as a backstop, and the model
    ``save()`` guard is the final line of defence.
    """

    class Meta:
        model = ProjectComponent
        fields = ["child_project", "quantity"]
        widgets = {
            "child_project": forms.Select(attrs={"class": "form-select"}),
            "quantity": forms.NumberInput(attrs={"class": "form-control", "min": 1}),
        }

    def __init__(self, *args, parent_project: Project, **kwargs) -> None:
        """Restrict the child queryset to acyclic, not-yet-linked projects.

        Args:
            parent_project: The assembly the new child module is being added to.
        """
        super().__init__(*args, **kwargs)
        self.parent_project = parent_project
        excluded = {parent_project.pk} | parent_project.get_descendant_ids()
        existing = set(parent_project.child_links.values_list("child_project_id", flat=True))
        self.fields["child_project"].queryset = Project.objects.exclude(pk__in=excluded | existing).order_by("name")
        self.fields["child_project"].label = "Module"

    def clean(self) -> dict:
        """Reject a child selection that would make the assembly contain itself."""
        cleaned = super().clean()
        child = cleaned.get("child_project")
        if child and component_would_create_cycle(self.parent_project.pk, child.pk):
            raise forms.ValidationError("Adding this module would create a cycle.")
        return cleaned

    def save(self, commit: bool = True) -> ProjectComponent:
        """Attach the edge to ``parent_project`` before saving."""
        self.instance.parent_project = self.parent_project
        return super().save(commit=commit)


class ProjectComponentQuantityForm(forms.ModelForm):
    """Edit only the ``quantity`` of an existing ``ProjectComponent`` edge."""

    class Meta:
        model = ProjectComponent
        fields = ["quantity"]
        widgets = {
            "quantity": forms.NumberInput(attrs={"class": "form-control", "min": 1}),
        }
