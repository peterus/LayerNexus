"""Print job forms."""

from typing import Any

from django import forms

from core.models import PrintJob

__all__ = [
    "PrintJobForm",
    "PrintJobUpdateForm",
    "AddPartToJobForm",
]


class PrintJobForm(forms.ModelForm):
    """Form for creating a new print job (draft).

    The user provides a name, machine profile, and optional notes.
    Parts are added separately after the job is created.
    """

    class Meta:
        model = PrintJob
        fields = [
            "name",
            "machine_profile",
            "notes",
        ]
        widgets = {
            "notes": forms.Textarea(attrs={"rows": 3}),
        }


class PrintJobUpdateForm(forms.ModelForm):
    """Form for editing existing print jobs."""

    class Meta:
        model = PrintJob
        fields = [
            "name",
            "machine_profile",
            "notes",
        ]
        widgets = {
            "notes": forms.Textarea(attrs={"rows": 3}),
        }


class AddPartToJobForm(forms.Form):
    """Form for adding a part to a print job.

    Used on the Part detail page — the user selects an existing draft
    job or chooses to create a new one.
    """

    job = forms.ModelChoiceField(
        queryset=PrintJob.objects.none(),
        required=False,
        empty_label="— Create new job —",
        label="Print Job",
        help_text="Select an existing draft job or create a new one.",
    )
    quantity = forms.IntegerField(
        min_value=1,
        initial=1,
        label="Quantity",
        help_text="How many copies of this part to add.",
    )

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        """Initialise with current user's draft jobs."""
        user = kwargs.pop("user", None)
        super().__init__(*args, **kwargs)
        if user:
            self.fields["job"].queryset = PrintJob.objects.filter(
                status=PrintJob.STATUS_DRAFT,
                created_by=user,
            ).order_by("-created_at")
