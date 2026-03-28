"""Bambu Lab Cloud authentication wizard views.

Three-step wizard for connecting a Bambu Lab Cloud account:
1. **BambuAccountStep1View** — enter email + password → triggers 2FA email
2. **BambuAccountStep2View** — enter 6-digit verification code → obtains JWT
3. **BambuAccountStep3View** — select device → creates PrinterProfile

Also provides list/delete views for managing connected accounts.
"""

import logging
from datetime import timedelta
from typing import Any

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse_lazy
from django.utils import timezone
from django.views import View
from django.views.generic import DeleteView, FormView, ListView

from core.forms import (
    BambuAuthStep1Form,
    BambuAuthStep2Form,
    BambuAuthStep3Form,
)
from core.mixins import PrinterManageMixin
from core.models import BambuCloudAccount, PrinterProfile
from core.services.bambulab import encrypt_token

logger = logging.getLogger(__name__)

__all__ = [
    "BambuAccountStep1View",
    "BambuAccountStep2View",
    "BambuAccountStep3View",
    "BambuAccountListView",
    "BambuAccountDeleteView",
    "BambuAccountRefreshView",
]

# Session keys for wizard state
_SESSION_BAMBU_EMAIL = "bambu_auth_email"
_SESSION_BAMBU_PASSWORD = "bambu_auth_password"
_SESSION_BAMBU_REGION = "bambu_auth_region"
_SESSION_BAMBU_TOKEN = "bambu_auth_token"
_SESSION_BAMBU_UID = "bambu_auth_uid"

# Default token validity (Bambu Lab tokens last ~24h)
TOKEN_VALIDITY_HOURS = 23


class BambuAccountStep1View(PrinterManageMixin, FormView):
    """Step 1: Enter Bambu Lab Cloud credentials.

    Sends credentials to the Bambu Lab API which triggers a 2FA
    verification email.  Credentials are stored temporarily in the
    session (password is cleared after step 2).
    """

    template_name = "core/bambuaccount_wizard_step1.html"
    form_class = BambuAuthStep1Form
    success_url = reverse_lazy("core:bambuaccount_step2")

    def form_valid(self, form: BambuAuthStep1Form) -> HttpResponse:
        """Initiate Bambu Lab login and store credentials in session."""
        email = form.cleaned_data["email"]
        password = form.cleaned_data["password"]
        region = form.cleaned_data["region"]

        try:
            from bambulab import BambuAuthenticator
        except ImportError:
            messages.error(
                self.request,
                "Bambu Lab Cloud API library is not installed. Please contact your administrator.",
            )
            return self.form_invalid(form)

        try:
            auth = BambuAuthenticator(region=region)
            # Initiate login — this sends the 2FA email
            # The library's login() blocks for code input, so we need
            # to use the lower-level API to just trigger the email
            auth._login_request(email, password)

            # Store in session for step 2
            self.request.session[_SESSION_BAMBU_EMAIL] = email
            self.request.session[_SESSION_BAMBU_PASSWORD] = password
            self.request.session[_SESSION_BAMBU_REGION] = region

            messages.info(
                self.request,
                f"A verification code has been sent to {email}. Please check your inbox.",
            )
            return super().form_valid(form)

        except AttributeError:
            # Library doesn't have _login_request — use alternate approach
            # Store credentials for step 2 to complete the full login
            self.request.session[_SESSION_BAMBU_EMAIL] = email
            self.request.session[_SESSION_BAMBU_PASSWORD] = password
            self.request.session[_SESSION_BAMBU_REGION] = region

            messages.info(
                self.request,
                f"Please check {email} for a verification code from Bambu Lab.",
            )
            return super().form_valid(form)

        except Exception as exc:
            logger.exception("Bambu Lab login initiation failed for %s", email)
            messages.error(
                self.request,
                f"Login failed: {exc}",
            )
            return self.form_invalid(form)


class BambuAccountStep2View(PrinterManageMixin, FormView):
    """Step 2: Enter the 2FA verification code.

    Completes authentication using the code sent to the user's email.
    On success, stores the encrypted JWT token and proceeds to device
    selection.
    """

    template_name = "core/bambuaccount_wizard_step2.html"
    form_class = BambuAuthStep2Form
    success_url = reverse_lazy("core:bambuaccount_step3")

    def dispatch(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        """Ensure step 1 has been completed."""
        if _SESSION_BAMBU_EMAIL not in request.session:
            messages.warning(request, "Please start from step 1.")
            return redirect("core:bambuaccount_step1")
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        """Add the email address to the template context."""
        context = super().get_context_data(**kwargs)
        context["bambu_email"] = self.request.session.get(_SESSION_BAMBU_EMAIL, "")
        return context

    def form_valid(self, form: BambuAuthStep2Form) -> HttpResponse:
        """Verify the 2FA code and obtain the JWT token."""
        code = form.cleaned_data["code"]
        email = self.request.session.get(_SESSION_BAMBU_EMAIL, "")
        password = self.request.session.get(_SESSION_BAMBU_PASSWORD, "")
        region = self.request.session.get(_SESSION_BAMBU_REGION, "global")

        try:
            from bambulab import BambuAuthenticator, BambuClient
        except ImportError:
            messages.error(self.request, "Bambu Lab library is not installed.")
            return self.form_invalid(form)

        try:
            auth = BambuAuthenticator(region=region)

            # Complete login with verification code
            # Use the login method with a callback that returns our code
            token = auth.login(email, password, lambda: code)

            if not token:
                messages.error(self.request, "Authentication failed — no token received.")
                return self.form_invalid(form)

            # Get user ID (needed for MQTT)
            client = BambuClient(token=token, region=region)
            user_info = client.get_user_info()
            bambu_uid = ""
            if isinstance(user_info, dict):
                bambu_uid = str(user_info.get("uid", user_info.get("user_id", "")))

            # Store token and UID in session for step 3
            self.request.session[_SESSION_BAMBU_TOKEN] = token
            self.request.session[_SESSION_BAMBU_UID] = bambu_uid

            # Clear password from session immediately
            self.request.session.pop(_SESSION_BAMBU_PASSWORD, None)

            messages.success(self.request, "Authentication successful! Select your printer.")
            return super().form_valid(form)

        except Exception as exc:
            logger.exception("Bambu Lab 2FA verification failed for %s", email)
            messages.error(self.request, f"Verification failed: {exc}")
            return self.form_invalid(form)


class BambuAccountStep3View(PrinterManageMixin, FormView):
    """Step 3: Select a printer from the authenticated account.

    Lists all devices bound to the Bambu Lab account and creates
    a :class:`BambuCloudAccount` and :class:`PrinterProfile` for the
    selected device.
    """

    template_name = "core/bambuaccount_wizard_step3.html"
    form_class = BambuAuthStep3Form
    success_url = reverse_lazy("core:printerprofile_list")

    def dispatch(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        """Ensure step 2 has been completed."""
        if _SESSION_BAMBU_TOKEN not in request.session:
            messages.warning(request, "Please complete authentication first.")
            return redirect("core:bambuaccount_step1")
        return super().dispatch(request, *args, **kwargs)

    def _get_devices(self) -> list[dict]:
        """Fetch device list from Bambu Lab Cloud API (cached per request).

        Returns:
            List of device dictionaries.
        """
        if hasattr(self, "_cached_devices"):
            return self._cached_devices

        token = self.request.session.get(_SESSION_BAMBU_TOKEN, "")
        region = self.request.session.get(_SESSION_BAMBU_REGION, "global")

        try:
            from bambulab import BambuClient

            client = BambuClient(token=token, region=region)
            result = client.get_devices()
            if isinstance(result, list):
                self._cached_devices = result
            else:
                self._cached_devices = result.get("devices", [])
        except Exception as exc:
            logger.exception("Failed to fetch Bambu Lab devices")
            messages.error(self.request, f"Failed to load devices: {exc}")
            self._cached_devices = []

        return self._cached_devices

    def get_form_kwargs(self) -> dict[str, Any]:
        """Pass device list to the form."""
        kwargs = super().get_form_kwargs()
        kwargs["devices"] = self._get_devices()
        return kwargs

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        """Add device details to the template context."""
        context = super().get_context_data(**kwargs)
        context["bambu_email"] = self.request.session.get(_SESSION_BAMBU_EMAIL, "")
        context["devices"] = self._get_devices()
        return context

    def form_valid(self, form: BambuAuthStep3Form) -> HttpResponse:
        """Create BambuCloudAccount and PrinterProfile for the selected device."""
        device_id = form.cleaned_data["device_id"]
        lan_ip = form.cleaned_data.get("lan_ip") or None

        email = self.request.session.get(_SESSION_BAMBU_EMAIL, "")
        region = self.request.session.get(_SESSION_BAMBU_REGION, "global")
        token = self.request.session.get(_SESSION_BAMBU_TOKEN, "")
        bambu_uid = self.request.session.get(_SESSION_BAMBU_UID, "")

        # Get device name for the printer profile
        devices = self._get_devices()
        device_name = "Bambu Lab Printer"
        for dev in devices:
            if dev.get("dev_id") == device_id:
                device_name = dev.get("name", device_name)
                break

        try:
            # Create or update BambuCloudAccount
            account, created = BambuCloudAccount.objects.update_or_create(
                user=self.request.user,
                email=email,
                defaults={
                    "region": region,
                    "token": encrypt_token(token),
                    "bambu_uid": bambu_uid,
                    "token_expires_at": timezone.now() + timedelta(hours=TOKEN_VALIDITY_HOURS),
                    "is_active": True,
                },
            )

            # Create PrinterProfile for the selected device
            printer, p_created = PrinterProfile.objects.get_or_create(
                printer_type=PrinterProfile.TYPE_BAMBULAB,
                bambu_account=account,
                bambu_device_id=device_id,
                defaults={
                    "name": device_name,
                    "bambu_ip_address": lan_ip,
                    "created_by": self.request.user,
                },
            )

            if not p_created:
                # Update existing printer with new IP if provided
                if lan_ip:
                    printer.bambu_ip_address = lan_ip
                    printer.save(update_fields=["bambu_ip_address"])
                messages.info(
                    self.request,
                    f"Printer '{printer.name}' was already connected. Updated settings.",
                )
            else:
                messages.success(
                    self.request,
                    f"Bambu Lab printer '{device_name}' connected successfully!",
                )

            # Clean up session
            self._clear_session()

            return super().form_valid(form)

        except Exception as exc:
            logger.exception("Failed to create Bambu Lab printer profile")
            messages.error(self.request, f"Failed to connect printer: {exc}")
            return self.form_invalid(form)

    def _clear_session(self) -> None:
        """Remove all wizard-related session data."""
        for key in (
            _SESSION_BAMBU_EMAIL,
            _SESSION_BAMBU_PASSWORD,
            _SESSION_BAMBU_REGION,
            _SESSION_BAMBU_TOKEN,
            _SESSION_BAMBU_UID,
        ):
            self.request.session.pop(key, None)


class BambuAccountListView(LoginRequiredMixin, ListView):
    """List the current user's Bambu Lab Cloud accounts."""

    model = BambuCloudAccount
    template_name = "core/bambuaccount_list.html"
    context_object_name = "accounts"

    def get_queryset(self):
        """Filter to only the current user's accounts."""
        return BambuCloudAccount.objects.filter(user=self.request.user)

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        """Add current time for token expiry comparison."""
        context = super().get_context_data(**kwargs)
        context["now"] = timezone.now()
        return context


class BambuAccountDeleteView(PrinterManageMixin, DeleteView):
    """Disconnect (delete) a Bambu Lab Cloud account.

    Also removes any printer profiles linked to this account.
    """

    model = BambuCloudAccount
    template_name = "core/bambuaccount_confirm_delete.html"
    success_url = reverse_lazy("core:bambuaccount_list")

    def get_queryset(self):
        """Restrict to accounts owned by the current user."""
        return BambuCloudAccount.objects.filter(user=self.request.user)

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        """Add linked printers to the context."""
        context = super().get_context_data(**kwargs)
        context["linked_printers"] = PrinterProfile.objects.filter(bambu_account=self.object)
        return context

    def form_valid(self, form: Any) -> HttpResponse:
        """Delete account and show success message."""
        email = self.object.email
        messages.success(
            self.request,
            f"Bambu Lab account '{email}' disconnected.",
        )
        return super().form_valid(form)


class BambuAccountRefreshView(PrinterManageMixin, View):
    """Re-authenticate an existing Bambu Lab account.

    Pre-fills the wizard with the account's email and region, then
    redirects to step 1.
    """

    def post(self, request: HttpRequest, pk: int) -> HttpResponse:
        """Start re-authentication for the given account."""
        account = get_object_or_404(
            BambuCloudAccount,
            pk=pk,
            user=request.user,
        )

        # Pre-fill session with account details
        request.session[_SESSION_BAMBU_EMAIL] = account.email
        request.session[_SESSION_BAMBU_REGION] = account.region

        messages.info(
            request,
            f"Please re-authenticate your Bambu Lab account '{account.email}'.",
        )
        return redirect("core:bambuaccount_step1")
