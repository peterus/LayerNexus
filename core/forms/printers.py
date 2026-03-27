"""Printer profile and cost profile forms."""

from django import forms

from core.models import CostProfile, PrinterProfile

__all__ = [
    "PrinterProfileForm",
    "CostProfileForm",
]


class PrinterProfileForm(forms.ModelForm):
    """Form for creating and updating printer profiles.

    Dynamically shows/hides connection fields based on ``printer_type``:
    - **Klipper**: ``moonraker_url``, ``moonraker_api_key``
    - **Bambu Lab**: ``bambu_account``, ``bambu_device_id``, ``bambu_ip_address``
    """

    class Meta:
        model = PrinterProfile
        fields = [
            "name",
            "description",
            "printer_type",
            "orca_machine_profile",
            # Klipper fields
            "moonraker_url",
            "moonraker_api_key",
            # Bambu Lab fields
            "bambu_account",
            "bambu_device_id",
            "bambu_ip_address",
        ]
        widgets = {
            "description": forms.Textarea(attrs={"rows": 3}),
            "moonraker_api_key": forms.PasswordInput(render_value=True),
            "printer_type": forms.RadioSelect,
        }

    def __init__(self, *args, **kwargs) -> None:
        """Make backend-specific fields optional regardless of printer type."""
        super().__init__(*args, **kwargs)
        # All connection fields are optional at form level;
        # clean() validates based on printer_type
        for field_name in (
            "moonraker_url",
            "moonraker_api_key",
            "bambu_account",
            "bambu_device_id",
            "bambu_ip_address",
        ):
            if field_name in self.fields:
                self.fields[field_name].required = False

        # Limit bambu_account to current user's accounts if we have a request
        if "bambu_account" in self.fields:
            self.fields["bambu_account"].required = False

    def clean(self) -> dict:
        """Validate connection fields based on the selected printer type."""
        cleaned = super().clean()
        printer_type = cleaned.get("printer_type")

        if printer_type == PrinterProfile.TYPE_KLIPPER:
            if not cleaned.get("moonraker_url"):
                self.add_error("moonraker_url", "Moonraker URL is required for Klipper printers.")

        elif printer_type == PrinterProfile.TYPE_BAMBULAB:
            if not cleaned.get("bambu_account"):
                self.add_error(
                    "bambu_account",
                    "A Bambu Lab account is required. Connect one first.",
                )
            if not cleaned.get("bambu_device_id"):
                self.add_error("bambu_device_id", "Device ID is required for Bambu Lab printers.")

        return cleaned


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
