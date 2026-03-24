"""Printer profile and cost profile forms."""

from django import forms

from core.models import CostProfile, PrinterProfile

__all__ = [
    "PrinterProfileForm",
    "CostProfileForm",
]


class PrinterProfileForm(forms.ModelForm):
    """Form for creating and updating printer profiles."""

    class Meta:
        model = PrinterProfile
        fields = [
            "name",
            "description",
            "orca_machine_profile",
            "moonraker_url",
            "moonraker_api_key",
        ]
        widgets = {
            "description": forms.Textarea(attrs={"rows": 3}),
            "moonraker_api_key": forms.PasswordInput(render_value=True),
        }


class CostProfileForm(forms.ModelForm):
    """Form for creating and updating cost profiles."""

    class Meta:
        model = CostProfile
        fields = [
            "electricity_cost_per_kwh",
            "printer_power_watts",
            "printer_purchase_cost",
            "printer_lifespan_hours",
            "maintenance_cost_per_hour",
        ]
