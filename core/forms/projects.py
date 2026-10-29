"""Project-related forms."""

from collections.abc import Callable, Iterable

from django import forms
from django.db.models import Model

from core.models import Project

__all__ = [
    "ProjectForm",
    "SubProjectForm",
    "ProjectEditForm",
    "ProjectCopyForm",
    "AddExistingSubprojectForm",
    "path_sorted_choices",
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


def path_sorted_choices(
    objects: Iterable[Model],
    label: Callable[[Model], str],
    empty_label: str | None = None,
) -> list[tuple[int | str, str]]:
    """Build select choices labelled by tree path and sorted by that label.

    Args:
        objects: Model instances to offer (usually the field's queryset).
        label: Returns the display label of one instance.
        empty_label: Label of a leading empty choice, or ``None`` for none.

    Returns:
        ``(pk, label)`` choices, sorted case-insensitively by label.
    """
    choices: list[tuple[int | str, str]] = sorted(
        ((obj.pk, label(obj)) for obj in objects), key=lambda choice: choice[1].casefold()
    )
    if empty_label is not None:
        choices.insert(0, ("", empty_label))
    return choices


class ProjectCopyForm(forms.Form):
    """Copy a project (with its whole subtree) as a new root or into another project."""

    name = forms.CharField(max_length=255)
    target = forms.ModelChoiceField(
        queryset=Project.objects.none(),
        required=False,
        empty_label="As new top-level project",
        label="Copy to",
        help_text="Leave empty to create a new top-level project, or pick a project to copy into.",
    )

    def __init__(self, *args, source: Project, labels: dict[int, str] | None = None, **kwargs) -> None:
        """Prefill the name and offer every project outside the copied subtree.

        Args:
            source: The project being copied.
            labels: Precomputed :meth:`Project.path_label_map`, to avoid a second scan.
        """
        super().__init__(*args, **kwargs)
        self.fields["name"].initial = source.default_copy_name()
        field = self.fields["target"]
        field.queryset = Project.objects.exclude(pk__in=source.subtree_ids()).order_by("name")
        labels = Project.path_label_map() if labels is None else labels
        field.choices = path_sorted_choices(
            field.queryset.only("pk", "name"), lambda p: labels.get(p.pk, p.name), field.empty_label
        )


class AddExistingSubprojectForm(forms.Form):
    """Pick a project to deep-copy into the current project as a sub-project."""

    source = forms.ModelChoiceField(queryset=Project.objects.none(), label="Project to copy")

    def __init__(self, *args, project: Project, labels: dict[int, str] | None = None, **kwargs) -> None:
        """Offer every project outside ``project``'s own subtree and ancestor chain.

        Args:
            project: The project the copy is added to.
            labels: Precomputed :meth:`Project.path_label_map`, to avoid a second scan.
        """
        super().__init__(*args, **kwargs)
        field = self.fields["source"]
        # Ancestors are excluded too: copying one into this project would copy it into its own subtree.
        excluded = project.subtree_ids() | {ancestor.pk for ancestor in project.ancestors()}
        field.queryset = Project.objects.exclude(pk__in=excluded).order_by("name")
        if self.is_bound:
            # Only POSTed to validate one PK, never re-rendered: skip building the picker.
            return
        labels = Project.path_label_map() if labels is None else labels
        field.choices = path_sorted_choices(
            field.queryset.only("pk", "name"), lambda p: labels.get(p.pk, p.name), field.empty_label
        )
