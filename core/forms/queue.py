"""Print queue forms."""

from typing import Any

from django import forms
from django.utils import timezone

from core.models import PrintQueue
from core.services.scheduled_start import MAX_SCHEDULE_AHEAD

__all__ = [
    "PrintQueueForm",
    "ScheduledStartForm",
]


class PrintQueueForm(forms.ModelForm):
    """Form for adding a plate to the queue.

    Only plates from sliced jobs can be queued.  The printer must have
    a machine profile matching the job's machine profile.
    """

    class Meta:
        model = PrintQueue
        fields = ["plate", "printer", "priority"]

    def clean(self) -> dict[str, Any]:
        """Validate that the selected printer is compatible with the job.

        Returns:
            Cleaned form data.

        Raises:
            forms.ValidationError: If the printer's machine profile does not
                match the job's machine profile.
        """
        cleaned = super().clean()
        plate = cleaned.get("plate")
        printer = cleaned.get("printer")
        if plate and printer:
            job_mp = plate.print_job.machine_profile
            printer_mp = printer.orca_machine_profile
            if job_mp and printer_mp and job_mp.pk != printer_mp.pk:
                raise forms.ValidationError(
                    f"Printer '{printer.name}' uses machine profile "
                    f"'{printer_mp.name}', but this job was sliced for "
                    f"'{job_mp.name}'. Please select a compatible printer."
                )
        return cleaned


class ScheduledStartForm(forms.Form):
    """Form to schedule a one-shot queue start (``datetime-local`` input in the current time zone)."""

    start_at = forms.DateTimeField(
        input_formats=["%Y-%m-%dT%H:%M", "%Y-%m-%dT%H:%M:%S"],
        widget=forms.DateTimeInput(attrs={"type": "datetime-local"}, format="%Y-%m-%dT%H:%M"),
    )

    def clean_start_at(self) -> Any:
        """Require a start time in the future, at most 7 days ahead."""
        start_at = self.cleaned_data["start_at"]
        now = timezone.now()
        if start_at <= now:
            raise forms.ValidationError("The start time must be in the future.")
        if start_at > now + MAX_SCHEDULE_AHEAD:
            raise forms.ValidationError("The start time must be within the next 7 days.")
        return start_at
