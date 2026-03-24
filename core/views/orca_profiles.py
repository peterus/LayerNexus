"""OrcaSlicer profile views for the LayerNexus application."""

import logging
from typing import Any

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect
from django.urls import reverse_lazy
from django.views import View
from django.views.generic import DeleteView, DetailView, ListView

from core.forms import (
    OrcaFilamentProfileImportForm,
    OrcaMachineProfileImportForm,
    OrcaPrintPresetImportForm,
)
from core.mixins import OrcaProfileManageMixin
from core.models import (
    OrcaFilamentProfile,
    OrcaMachineProfile,
    OrcaPrintPreset,
)

logger = logging.getLogger(__name__)

__all__ = [
    "OrcaMachineProfileListView",
    "OrcaMachineProfileDetailView",
    "OrcaMachineProfileImportView",
    "OrcaMachineProfileDeleteView",
    "OrcaFilamentProfileListView",
    "OrcaFilamentProfileDetailView",
    "OrcaFilamentProfileImportView",
    "OrcaFilamentProfileDeleteView",
    "OrcaPrintPresetListView",
    "OrcaPrintPresetDetailView",
    "OrcaPrintPresetImportView",
    "OrcaPrintPresetDeleteView",
]


# ── OrcaSlicer Machine Profiles (structured import with inheritance) ─────


class OrcaMachineProfileListView(LoginRequiredMixin, ListView):
    """List all OrcaSlicer machine profiles."""

    model = OrcaMachineProfile
    template_name = "core/orcamachineprofile_list.html"
    context_object_name = "profiles"


class OrcaMachineProfileDetailView(LoginRequiredMixin, DetailView):
    """Show details of a resolved OrcaSlicer machine profile."""

    model = OrcaMachineProfile
    template_name = "core/orcamachineprofile_detail.html"
    context_object_name = "profile"

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        """Add resolved settings to context for display."""
        context = super().get_context_data(**kwargs)
        profile = self.object
        if profile.is_resolved:
            from core.services.profile_import import MACHINE_FIELD_MAP

            key_settings = []
            for field_name, (orca_key, _type_tag) in MACHINE_FIELD_MAP.items():
                value = profile.settings.get(field_name)
                if value is not None and value != "" and value != []:
                    key_settings.append(
                        {
                            "key": orca_key,
                            "value": value,
                            "field_name": field_name,
                        }
                    )
            context["key_settings"] = key_settings
            known_fields = set(MACHINE_FIELD_MAP.keys())
            context["extra_settings_count"] = len([k for k in profile.settings if k not in known_fields])
            context["extra_settings"] = {k: v for k, v in profile.settings.items() if k not in known_fields}
        return context


class OrcaMachineProfileImportView(OrcaProfileManageMixin, View):
    """Import an OrcaSlicer machine profile from a JSON file.

    Handles the inheritance chain: if the profile's parent is missing,
    the user is informed which file to upload next.
    """

    template_name = "core/orcamachineprofile_import.html"

    def get(self, request: HttpRequest) -> HttpResponse:
        """Show the import form."""
        from django.template.response import TemplateResponse

        form = OrcaMachineProfileImportForm()
        pending = OrcaMachineProfile.objects.filter(
            state=OrcaMachineProfile.STATE_PENDING,
        )
        return TemplateResponse(
            request,
            self.template_name,
            {"form": form, "pending_profiles": pending},
        )

    def post(self, request: HttpRequest) -> HttpResponse:
        """Process an uploaded profile JSON file."""
        from django.template.response import TemplateResponse

        from core.services.profile_import import import_machine_profile_json

        form = OrcaMachineProfileImportForm(request.POST, request.FILES)
        if not form.is_valid():
            pending = OrcaMachineProfile.objects.filter(
                state=OrcaMachineProfile.STATE_PENDING,
            )
            return TemplateResponse(
                request,
                self.template_name,
                {"form": form, "pending_profiles": pending},
            )

        json_data = form.cleaned_data["profile_file"]
        display_name = form.cleaned_data.get("display_name") or None

        try:
            result = import_machine_profile_json(json_data, request.user, display_name)
        except ValueError as exc:
            form.add_error(None, str(exc))
            pending = OrcaMachineProfile.objects.filter(
                state=OrcaMachineProfile.STATE_PENDING,
            )
            return TemplateResponse(
                request,
                self.template_name,
                {"form": form, "pending_profiles": pending},
            )

        if result.is_resolved:
            msg = f"Profile '{result.profile.name}' imported and resolved."
            if result.auto_resolved_children:
                names = ", ".join(result.auto_resolved_children)
                msg += f" Additionally auto-resolved: {names}."
            messages.success(request, msg)
            return redirect("core:orcamachineprofile_detail", pk=result.profile.pk)
        else:
            messages.warning(
                request,
                f"Profile '{result.profile.name}' saved as pending. "
                f"Please upload the parent profile '{result.missing_parent}' next.",
            )
            return redirect("core:orcamachineprofile_import")


class OrcaMachineProfileDeleteView(OrcaProfileManageMixin, DeleteView):
    """Delete an OrcaSlicer machine profile."""

    model = OrcaMachineProfile
    template_name = "core/orcamachineprofile_confirm_delete.html"
    success_url = reverse_lazy("core:orcamachineprofile_list")

    def form_valid(self, form):
        messages.success(self.request, "Machine profile deleted.")
        return super().form_valid(form)


# ── Filament Profiles ────────────────────────────────────────────────────


class OrcaFilamentProfileListView(LoginRequiredMixin, ListView):
    """List all filament profiles."""

    model = OrcaFilamentProfile
    template_name = "core/orcafilamentprofile_list.html"
    context_object_name = "filament_profiles"


class OrcaFilamentProfileDetailView(LoginRequiredMixin, DetailView):
    """Show details of a resolved OrcaSlicer filament profile."""

    model = OrcaFilamentProfile
    template_name = "core/orcafilamentprofile_detail.html"
    context_object_name = "profile"

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        """Add resolved settings to context for display."""
        context = super().get_context_data(**kwargs)
        profile = self.object
        if profile.is_resolved:
            from core.services.profile_import import FILAMENT_FIELD_MAP

            key_settings = []
            for field_name, (orca_key, _type_tag) in FILAMENT_FIELD_MAP.items():
                value = profile.settings.get(field_name)
                if value is not None and value != "" and value != []:
                    key_settings.append(
                        {
                            "key": orca_key,
                            "value": value,
                            "field_name": field_name,
                        }
                    )
            context["key_settings"] = key_settings
            known_fields = set(FILAMENT_FIELD_MAP.keys())
            context["extra_settings_count"] = len([k for k in profile.settings if k not in known_fields])
            context["extra_settings"] = {k: v for k, v in profile.settings.items() if k not in known_fields}
        return context


class OrcaFilamentProfileImportView(OrcaProfileManageMixin, View):
    """Import an OrcaSlicer filament profile from a JSON file.

    Handles the inheritance chain: if the profile's parent is missing,
    the user is informed which file to upload next.
    """

    template_name = "core/orcafilamentprofile_import.html"

    def get(self, request: HttpRequest) -> HttpResponse:
        """Show the import form."""
        from django.template.response import TemplateResponse

        form = OrcaFilamentProfileImportForm()
        pending = OrcaFilamentProfile.objects.filter(
            state=OrcaFilamentProfile.STATE_PENDING,
        )
        return TemplateResponse(
            request,
            self.template_name,
            {"form": form, "pending_profiles": pending},
        )

    def post(self, request: HttpRequest) -> HttpResponse:
        """Process an uploaded filament profile JSON file."""
        from django.template.response import TemplateResponse

        from core.services.profile_import import import_filament_profile_json

        form = OrcaFilamentProfileImportForm(request.POST, request.FILES)
        if not form.is_valid():
            pending = OrcaFilamentProfile.objects.filter(
                state=OrcaFilamentProfile.STATE_PENDING,
            )
            return TemplateResponse(
                request,
                self.template_name,
                {"form": form, "pending_profiles": pending},
            )

        json_data = form.cleaned_data["profile_file"]
        display_name = form.cleaned_data.get("display_name") or None

        try:
            result = import_filament_profile_json(json_data, request.user, display_name)
        except ValueError as exc:
            form.add_error(None, str(exc))
            pending = OrcaFilamentProfile.objects.filter(
                state=OrcaFilamentProfile.STATE_PENDING,
            )
            return TemplateResponse(
                request,
                self.template_name,
                {"form": form, "pending_profiles": pending},
            )

        if result.is_resolved:
            msg = f"Filament profile '{result.profile.name}' imported and resolved."
            if result.auto_resolved_children:
                names = ", ".join(result.auto_resolved_children)
                msg += f" Additionally auto-resolved: {names}."
            messages.success(request, msg)
            return redirect("core:orcafilamentprofile_detail", pk=result.profile.pk)
        else:
            messages.warning(
                request,
                f"Filament profile '{result.profile.name}' saved as pending. "
                f"Please upload the parent profile '{result.missing_parent}' next.",
            )
            return redirect("core:orcafilamentprofile_import")


class OrcaFilamentProfileDeleteView(OrcaProfileManageMixin, DeleteView):
    """Delete an OrcaSlicer filament profile."""

    model = OrcaFilamentProfile
    template_name = "core/orcafilamentprofile_confirm_delete.html"
    success_url = reverse_lazy("core:orcafilamentprofile_list")

    def form_valid(self, form):
        messages.success(self.request, "Filament profile deleted.")
        return super().form_valid(form)


# ── Print Presets ─────────────────────────────────────────────────────────


class OrcaPrintPresetListView(LoginRequiredMixin, ListView):
    """List all print presets."""

    model = OrcaPrintPreset
    template_name = "core/orcaprintpreset_list.html"
    context_object_name = "print_presets"


class OrcaPrintPresetDetailView(LoginRequiredMixin, DetailView):
    """Show details of a resolved OrcaSlicer process profile."""

    model = OrcaPrintPreset
    template_name = "core/orcaprintpreset_detail.html"
    context_object_name = "profile"

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        """Add resolved settings to context for display."""
        context = super().get_context_data(**kwargs)
        profile = self.object
        if profile.is_resolved:
            from core.services.profile_import import PROCESS_FIELD_MAP

            key_settings = []
            for field_name, (orca_key, _type_tag) in PROCESS_FIELD_MAP.items():
                value = profile.settings.get(field_name)
                if value is not None and value != "" and value != []:
                    key_settings.append(
                        {
                            "key": orca_key,
                            "value": value,
                            "field_name": field_name,
                        }
                    )
            context["key_settings"] = key_settings
            known_fields = set(PROCESS_FIELD_MAP.keys())
            context["extra_settings_count"] = len([k for k in profile.settings if k not in known_fields])
            context["extra_settings"] = {k: v for k, v in profile.settings.items() if k not in known_fields}
        return context


class OrcaPrintPresetImportView(OrcaProfileManageMixin, View):
    """Import an OrcaSlicer process profile from a JSON file.

    Handles the inheritance chain: if the profile's parent is missing,
    the user is informed which file to upload next.
    """

    template_name = "core/orcaprintpreset_import.html"

    def get(self, request: HttpRequest) -> HttpResponse:
        """Show the import form."""
        from django.template.response import TemplateResponse

        form = OrcaPrintPresetImportForm()
        pending = OrcaPrintPreset.objects.filter(
            state=OrcaPrintPreset.STATE_PENDING,
        )
        return TemplateResponse(
            request,
            self.template_name,
            {"form": form, "pending_profiles": pending},
        )

    def post(self, request: HttpRequest) -> HttpResponse:
        """Process an uploaded process profile JSON file."""
        from django.template.response import TemplateResponse

        from core.services.profile_import import import_process_profile_json

        form = OrcaPrintPresetImportForm(request.POST, request.FILES)
        if not form.is_valid():
            pending = OrcaPrintPreset.objects.filter(
                state=OrcaPrintPreset.STATE_PENDING,
            )
            return TemplateResponse(
                request,
                self.template_name,
                {"form": form, "pending_profiles": pending},
            )

        json_data = form.cleaned_data["profile_file"]
        display_name = form.cleaned_data.get("display_name") or None

        try:
            result = import_process_profile_json(json_data, request.user, display_name)
        except ValueError as exc:
            form.add_error(None, str(exc))
            pending = OrcaPrintPreset.objects.filter(
                state=OrcaPrintPreset.STATE_PENDING,
            )
            return TemplateResponse(
                request,
                self.template_name,
                {"form": form, "pending_profiles": pending},
            )

        if result.is_resolved:
            msg = f"Process profile '{result.profile.name}' imported and resolved."
            if result.auto_resolved_children:
                names = ", ".join(result.auto_resolved_children)
                msg += f" Additionally auto-resolved: {names}."
            messages.success(request, msg)
            return redirect("core:orcaprintpreset_detail", pk=result.profile.pk)
        else:
            messages.warning(
                request,
                f"Process profile '{result.profile.name}' saved as pending. "
                f"Please upload the parent profile '{result.missing_parent}' next.",
            )
            return redirect("core:orcaprintpreset_import")


class OrcaPrintPresetDeleteView(OrcaProfileManageMixin, DeleteView):
    """Delete an OrcaSlicer process profile."""

    model = OrcaPrintPreset
    template_name = "core/orcaprintpreset_confirm_delete.html"
    context_object_name = "profile"
    success_url = reverse_lazy("core:orcaprintpreset_list")

    def form_valid(self, form):
        messages.success(self.request, "Process profile deleted.")
        return super().form_valid(form)
