"""OrcaSlicer profile views for the LayerNexus application."""

import logging
from collections.abc import Callable
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


# ── Base import view ─────────────────────────────────────────────────────


class OrcaProfileImportViewBase(OrcaProfileManageMixin, View):
    """Base view for importing OrcaSlicer profiles from JSON files.

    Subclasses configure the following class attributes:
    - ``model_class``: The Django model for this profile type.
    - ``form_class``: The import form class.
    - ``import_function_path``: Attribute name on
      ``core.services.profile_import`` for the import function.
    - ``template_name``: Template to render.
    - ``success_url_name``: URL name to redirect to on success
      (detail view, receives ``pk``).
    - ``pending_url_name``: URL name to redirect to when profile is
      pending (import view).
    - ``profile_type_label``: Human-readable label for messages
      (e.g. ``"Profile"``, ``"Filament profile"``).
    """

    model_class = None
    form_class = None
    import_function_path: str = ""
    template_name: str = ""
    success_url_name: str = ""
    pending_url_name: str = ""
    profile_type_label: str = "Profile"

    def _get_import_function(self) -> Callable:
        """Lazily import the profile import function."""
        from core.services import profile_import

        return getattr(profile_import, self.import_function_path)

    def _get_pending_queryset(self):
        """Return pending profiles for context."""
        return self.model_class.objects.filter(
            state=self.model_class.STATE_PENDING,
        )

    def get(self, request: HttpRequest) -> HttpResponse:
        """Show the import form."""
        from django.template.response import TemplateResponse

        form = self.form_class()
        return TemplateResponse(
            request,
            self.template_name,
            {"form": form, "pending_profiles": self._get_pending_queryset()},
        )

    def post(self, request: HttpRequest) -> HttpResponse:
        """Process an uploaded profile JSON file."""
        from django.template.response import TemplateResponse

        import_function = self._get_import_function()

        form = self.form_class(request.POST, request.FILES)
        if not form.is_valid():
            return TemplateResponse(
                request,
                self.template_name,
                {"form": form, "pending_profiles": self._get_pending_queryset()},
            )

        json_data = form.cleaned_data["profile_file"]
        display_name = form.cleaned_data.get("display_name") or None

        try:
            result = import_function(json_data, request.user, display_name)
        except ValueError as exc:
            form.add_error(None, str(exc))
            return TemplateResponse(
                request,
                self.template_name,
                {"form": form, "pending_profiles": self._get_pending_queryset()},
            )

        if result.is_resolved:
            msg = f"{self.profile_type_label} '{result.profile.name}' imported and resolved."
            if result.auto_resolved_children:
                names = ", ".join(result.auto_resolved_children)
                msg += f" Additionally auto-resolved: {names}."
            messages.success(request, msg)
            return redirect(self.success_url_name, pk=result.profile.pk)
        else:
            messages.warning(
                request,
                f"{self.profile_type_label} '{result.profile.name}' saved as pending. "
                f"Please upload the parent profile '{result.missing_parent}' next.",
            )
            return redirect(self.pending_url_name)


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


class OrcaMachineProfileImportView(OrcaProfileImportViewBase):
    """Import an OrcaSlicer machine profile from a JSON file."""

    model_class = OrcaMachineProfile
    form_class = OrcaMachineProfileImportForm
    import_function_path = "import_machine_profile_json"
    template_name = "core/orcamachineprofile_import.html"
    success_url_name = "core:orcamachineprofile_detail"
    pending_url_name = "core:orcamachineprofile_import"
    profile_type_label = "Profile"


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


class OrcaFilamentProfileImportView(OrcaProfileImportViewBase):
    """Import an OrcaSlicer filament profile from a JSON file."""

    model_class = OrcaFilamentProfile
    form_class = OrcaFilamentProfileImportForm
    import_function_path = "import_filament_profile_json"
    template_name = "core/orcafilamentprofile_import.html"
    success_url_name = "core:orcafilamentprofile_detail"
    pending_url_name = "core:orcafilamentprofile_import"
    profile_type_label = "Filament profile"


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


class OrcaPrintPresetImportView(OrcaProfileImportViewBase):
    """Import an OrcaSlicer process profile from a JSON file."""

    model_class = OrcaPrintPreset
    form_class = OrcaPrintPresetImportForm
    import_function_path = "import_process_profile_json"
    template_name = "core/orcaprintpreset_import.html"
    success_url_name = "core:orcaprintpreset_detail"
    pending_url_name = "core:orcaprintpreset_import"
    profile_type_label = "Process profile"


class OrcaPrintPresetDeleteView(OrcaProfileManageMixin, DeleteView):
    """Delete an OrcaSlicer process profile."""

    model = OrcaPrintPreset
    template_name = "core/orcaprintpreset_confirm_delete.html"
    context_object_name = "profile"
    success_url = reverse_lazy("core:orcaprintpreset_list")

    def form_valid(self, form):
        messages.success(self.request, "Process profile deleted.")
        return super().form_valid(form)
