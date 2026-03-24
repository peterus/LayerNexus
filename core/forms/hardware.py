"""Hardware-related forms."""

from typing import Any

from django import forms

from core.models import HardwarePart, Project, ProjectHardware

__all__ = [
    "ProjectHardwareForm",
    "ProjectHardwareUpdateForm",
]


class ProjectHardwareForm(forms.Form):
    """Form for adding a hardware part to a project.

    The user can either select an existing :class:`HardwarePart` from
    the dropdown or fill in the ``new_*`` fields to create a new one.
    """

    hardware_part = forms.ModelChoiceField(
        queryset=HardwarePart.objects.all(),
        required=False,
        label="Existing hardware part",
        widget=forms.Select(attrs={"class": "form-select"}),
    )

    new_name = forms.CharField(
        max_length=255,
        required=False,
        label="Name",
    )
    new_category = forms.ChoiceField(
        choices=[("", "---------")] + list(HardwarePart.CATEGORY_CHOICES),
        required=False,
        label="Category",
        widget=forms.Select(attrs={"class": "form-select"}),
    )
    new_url = forms.URLField(
        required=False,
        label="URL",
        widget=forms.URLInput(attrs={"placeholder": "https://..."}),
    )
    new_unit_price = forms.DecimalField(
        max_digits=10,
        decimal_places=2,
        required=False,
        label="Unit price",
        widget=forms.NumberInput(attrs={"step": "0.01", "min": "0"}),
    )
    new_notes = forms.CharField(
        required=False,
        label="Notes (hardware part)",
        widget=forms.Textarea(attrs={"rows": 2}),
    )

    quantity = forms.IntegerField(
        min_value=1,
        initial=1,
        label="Quantity",
    )
    notes = forms.CharField(
        required=False,
        label="Notes (project-specific)",
        widget=forms.Textarea(attrs={"rows": 2}),
    )

    def clean(self) -> dict:
        """Ensure either an existing part is selected or new_name + new_category are provided."""
        cleaned_data = super().clean()
        hw_part = cleaned_data.get("hardware_part")
        new_name = cleaned_data.get("new_name", "").strip()
        new_category = cleaned_data.get("new_category", "").strip()

        if not hw_part and not new_name:
            raise forms.ValidationError("Select an existing hardware part or provide a name for a new one.")
        if not hw_part and not new_category:
            self.add_error("new_category", "Category is required for new hardware parts.")

        return cleaned_data

    def save(self, project: Project, user: Any = None) -> ProjectHardware:
        """Create or retrieve the HardwarePart and link it to the project.

        Args:
            project: The project to assign the hardware to.
            user: The current user (set as created_by on new HardwareParts).

        Returns:
            The created ProjectHardware instance.
        """
        hw_part = self.cleaned_data.get("hardware_part")

        if not hw_part:
            hw_part, _created = HardwarePart.objects.get_or_create(
                name=self.cleaned_data["new_name"].strip(),
                category=self.cleaned_data["new_category"],
                defaults={
                    "url": self.cleaned_data.get("new_url") or "",
                    "unit_price": self.cleaned_data.get("new_unit_price"),
                    "notes": self.cleaned_data.get("new_notes", ""),
                    "created_by": user,
                },
            )

        return ProjectHardware.objects.create(
            project=project,
            hardware_part=hw_part,
            quantity=self.cleaned_data["quantity"],
            notes=self.cleaned_data.get("notes", ""),
        )


class ProjectHardwareUpdateForm(forms.ModelForm):
    """Form for editing a hardware assignment's quantity and notes.

    Also exposes the related :class:`HardwarePart` fields for inline
    editing (name, category, url, unit_price).
    """

    hw_name = forms.CharField(max_length=255, label="Name")
    hw_category = forms.ChoiceField(
        choices=HardwarePart.CATEGORY_CHOICES,
        label="Category",
        widget=forms.Select(attrs={"class": "form-select"}),
    )
    hw_url = forms.URLField(required=False, label="URL")
    hw_unit_price = forms.DecimalField(
        max_digits=10,
        decimal_places=2,
        required=False,
        label="Unit price",
        widget=forms.NumberInput(attrs={"step": "0.01", "min": "0"}),
    )
    hw_notes = forms.CharField(
        required=False,
        label="Hardware notes",
        widget=forms.Textarea(attrs={"rows": 2}),
    )

    class Meta:
        model = ProjectHardware
        fields = ["quantity", "notes"]
        widgets = {
            "notes": forms.Textarea(attrs={"rows": 2}),
        }

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        if self.instance and self.instance.pk:
            hp = self.instance.hardware_part
            self.fields["hw_name"].initial = hp.name
            self.fields["hw_category"].initial = hp.category
            self.fields["hw_url"].initial = hp.url
            self.fields["hw_unit_price"].initial = hp.unit_price
            self.fields["hw_notes"].initial = hp.notes

    field_order = [
        "hw_name",
        "hw_category",
        "hw_url",
        "hw_unit_price",
        "hw_notes",
        "quantity",
        "notes",
    ]

    def clean(self) -> dict:
        """Validate that the new (name, category) combination is unique for HardwarePart."""
        cleaned_data = super().clean()
        name = cleaned_data.get("hw_name")
        category = cleaned_data.get("hw_category")
        if name and category and self.instance and self.instance.pk:
            hp = self.instance.hardware_part
            if HardwarePart.objects.filter(name=name, category=category).exclude(pk=hp.pk).exists():
                raise forms.ValidationError(
                    f"A hardware part named '{name}' with category '{category}' already exists. "
                    "Please use a different name or category."
                )
        return cleaned_data

    def save(self, commit: bool = True) -> ProjectHardware:
        """Save the ProjectHardware and update the related HardwarePart."""
        instance = super().save(commit=False)
        hp = instance.hardware_part
        hp.name = self.cleaned_data["hw_name"]
        hp.category = self.cleaned_data["hw_category"]
        hp.url = self.cleaned_data.get("hw_url") or ""
        hp.unit_price = self.cleaned_data.get("hw_unit_price")
        hp.notes = self.cleaned_data.get("hw_notes") or ""
        if commit:
            hp.save()
            instance.save()
        return instance
