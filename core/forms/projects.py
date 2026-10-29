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
    """Form for editing a project, including moving it within the tree.

    ``parent`` moves the project: any project outside its own subtree, or empty for
    a top-level project. Sub-projects additionally get their ``quantity`` within the
    parent. The preset is optional (inherited from the parent) but required when the
    project is, or becomes, a top-level project.
    """

    #: Error shown when a project would be moved into its own subtree.
    CYCLE_ERROR = "A project cannot be a sub-project of itself or one of its descendants."

    class Meta:
        model = Project
        fields = ["name", "description", "image", "parent", "quantity", "default_print_preset"]
        widgets = {
            "description": forms.Textarea(attrs={"rows": 4}),
        }

    def __init__(self, *args, labels: dict[int, str] | None = None, **kwargs) -> None:
        """Offer every project outside the own subtree as parent; keep ``quantity`` for sub-projects.

        Args:
            labels: Precomputed :meth:`Project.path_label_map`, to avoid a second scan.
        """
        super().__init__(*args, **kwargs)
        parent = self.fields["parent"]
        parent.required = False
        parent.empty_label = "(top-level project)"
        parent.label = "Parent project"
        parent.help_text = "Move this project into another project, or leave empty for a top-level project."
        parent.queryset = Project.objects.exclude(pk__in=self.instance.subtree_ids()).order_by("name")
        # Path labels are also needed when an invalid POST re-renders the form.
        labels = Project.path_label_map() if labels is None else labels
        parent.choices = path_sorted_choices(
            parent.queryset.only("pk", "name"), lambda p: labels.get(p.pk, p.name), parent.empty_label
        )
        # Enforced in clean(): only required when the project ends up top-level.
        self.fields["default_print_preset"].required = False
        if self.instance.parent_id is not None:
            self.fields["default_print_preset"].help_text = INHERITED_PRESET_HELP
        else:
            del self.fields["quantity"]

    def clean(self) -> dict:
        """Require a preset on a top-level project; a top-level project has quantity 1.

        Only an explicitly submitted empty ``parent`` makes a project top-level: a POST
        without the field (an edit page opened before moving existed, a script) keeps
        the current parent instead of silently turning a sub-project into a root.
        """
        cleaned_data = super().clean()
        if self.is_bound and self.add_prefix("parent") not in self.data and "parent" in cleaned_data:
            cleaned_data["parent"] = self.instance.parent
        if "parent" in cleaned_data and cleaned_data["parent"] is None:
            if "default_print_preset" in cleaned_data and cleaned_data["default_print_preset"] is None:
                self.add_error("default_print_preset", "Top-level projects need a default print preset.")
            # Quantity only means something below a parent (same rule as the API).
            self.instance.quantity = 1
            if "quantity" in self.fields:
                cleaned_data["quantity"] = 1
        return cleaned_data


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

    def __init__(
        self,
        *args,
        project: Project,
        labels: dict[int, str] | None = None,
        subtree_ids: set[int] | None = None,
        **kwargs,
    ) -> None:
        """Offer every project outside ``project``'s own subtree and ancestor chain.

        Args:
            project: The project the copy is added to.
            labels: Precomputed :meth:`Project.path_label_map`, to avoid a second scan.
            subtree_ids: Precomputed :meth:`Project.subtree_ids` of ``project``.
        """
        super().__init__(*args, **kwargs)
        field = self.fields["source"]
        if subtree_ids is None:
            subtree_ids = project.subtree_ids()
        # Ancestors are excluded too: copying one into this project would copy it into its own subtree.
        excluded = subtree_ids | {ancestor.pk for ancestor in project.ancestors()}
        field.queryset = Project.objects.exclude(pk__in=excluded).order_by("name")
        if self.is_bound:
            # Only POSTed to validate one PK, never re-rendered: skip building the picker.
            return
        labels = Project.path_label_map() if labels is None else labels
        field.choices = path_sorted_choices(
            field.queryset.only("pk", "name"), lambda p: labels.get(p.pk, p.name), field.empty_label
        )
