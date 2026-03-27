"""Bambu Lab Cloud authentication wizard forms.

Provides step-specific forms for the three-step auth wizard:
1. Account credentials (email, password, region)
2. Two-factor verification (6-digit code)
3. Device selection (choose printer from account)
"""

from django import forms

from core.models import BambuCloudAccount

__all__ = [
    "BambuAuthStep1Form",
    "BambuAuthStep2Form",
    "BambuAuthStep3Form",
]


class BambuAuthStep1Form(forms.Form):
    """Step 1: Bambu Lab Cloud login credentials.

    The password is NOT stored — it is only used to initiate the
    authentication flow and trigger the 2FA email.
    """

    email = forms.EmailField(
        label="Bambu Lab Email",
        help_text="The email address associated with your Bambu Lab account.",
        widget=forms.EmailInput(attrs={"autofocus": True}),
    )
    password = forms.CharField(
        label="Password",
        widget=forms.PasswordInput,
        help_text="Your password is only used to authenticate and is never stored.",
    )
    region = forms.ChoiceField(
        label="Region",
        choices=BambuCloudAccount.REGION_CHOICES,
        initial=BambuCloudAccount.REGION_GLOBAL,
        help_text="Select the region matching your Bambu Lab account.",
    )


class BambuAuthStep2Form(forms.Form):
    """Step 2: Two-factor authentication code verification.

    After step 1, Bambu Lab sends a 6-digit verification code to the
    user's email.  This form collects that code.
    """

    code = forms.CharField(
        label="Verification Code",
        max_length=6,
        min_length=6,
        help_text="Enter the 6-digit code sent to your email.",
        widget=forms.TextInput(attrs={
            "autofocus": True,
            "inputmode": "numeric",
            "pattern": "[0-9]{6}",
            "autocomplete": "one-time-code",
            "class": "form-control form-control-lg text-center",
            "style": "letter-spacing: 0.5em; max-width: 200px;",
        }),
    )

    def clean_code(self) -> str:
        """Validate that the code is exactly 6 digits."""
        code = self.cleaned_data["code"].strip()
        if not code.isdigit():
            raise forms.ValidationError("The code must contain only digits.")
        return code


class BambuAuthStep3Form(forms.Form):
    """Step 3: Device selection from the authenticated account.

    Displays a list of printers bound to the Bambu Lab account.
    The user selects which device(s) to add as printer profiles.
    """

    device_id = forms.ChoiceField(
        label="Select Printer",
        widget=forms.RadioSelect,
        help_text="Choose the printer you want to connect to LayerNexus.",
    )
    lan_ip = forms.GenericIPAddressField(
        label="Local IP Address (optional)",
        required=False,
        help_text=(
            "If the printer is on your local network, enter its IP address "
            "for faster file uploads via LAN."
        ),
    )

    def __init__(self, *args, devices: list[dict] | None = None, **kwargs) -> None:
        """Initialize with available devices.

        Args:
            devices: List of device dicts from the Bambu Lab Cloud API.
                Each dict should have at least ``dev_id`` and ``name`` keys.
        """
        super().__init__(*args, **kwargs)
        if devices:
            self.fields["device_id"].choices = [
                (
                    dev.get("dev_id", ""),
                    f"{dev.get('name', 'Unknown')} ({dev.get('dev_product_name', 'Unknown Model')})"
                    f" — {'Online' if dev.get('online') else 'Offline'}",
                )
                for dev in devices
            ]
        else:
            self.fields["device_id"].choices = []

