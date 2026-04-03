"""Part views for the LayerNexus application."""

import json
import logging

from django import forms as django_forms
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse, reverse_lazy
from django.views import View
from django.views.generic import CreateView, DeleteView, DetailView, UpdateView

from core.forms import PartForm
from core.mixins import ProjectManageMixin
from core.models import (
    OrcaPrintPreset,
    Part,
    PrintJob,
    Project,
    SpoolmanFilamentMapping,
)
from core.services.spoolman import SpoolmanClient, SpoolmanError
from core.views.helpers import _trigger_part_estimation

logger = logging.getLogger(__name__)

__all__ = [
    "PartDetailView",
    "PartCreateView",
    "PartUpdateView",
    "PartDeleteView",
    "PartReEstimateView",
]


class _SpoolmanFilamentMixin:
    """Shared logic for populating Spoolman filament dropdown on Part forms."""

    _spoolman_configured = False
    _spoolman_filaments: dict = {}
    _spoolman_colors: dict = {}

    def populate_spoolman_filaments(self, form):
        """Populate the spoolman_filament_id field with Spoolman filament types."""
        spoolman_url = settings.SPOOLMAN_URL
        if not spoolman_url:
            self._spoolman_configured = False
            self._spoolman_filaments = {}
            self._spoolman_colors = {}
            return
        try:
            client = SpoolmanClient(spoolman_url)
            filaments = client.get_filaments()
            choices = [("", "— Select Filament Type —")]
            colors: dict[str, str] = {}
            for f in filaments:
                vendor = f.get("vendor", {}) or {}
                vendor_name = vendor.get("name", "")
                name = f.get("name", f"Filament #{f['id']}")
                material = f.get("material", "")
                color_hex = (f.get("color_hex") or "")[:7]
                label = f"{vendor_name} - {name}" if vendor_name else name
                if material:
                    label += f" ({material})"
                choices.append((f["id"], label))
                if color_hex:
                    colors[str(f["id"])] = color_hex
            form.fields["spoolman_filament_id"].widget = django_forms.Select(
                choices=choices,
                attrs={"class": "form-select"},
            )
            self._spoolman_configured = True
            self._spoolman_filaments = {f["id"]: f for f in filaments}
            self._spoolman_colors = colors
        except SpoolmanError:
            self._spoolman_configured = False
            self._spoolman_filaments = {}
            self._spoolman_colors = {}

    def apply_spoolman_filament(self, form):
        """Auto-fill color/material from the selected Spoolman filament.

        Also updates the cached color on the SpoolmanFilamentMapping so that
        the filament requirements display always reflects the current Spoolman
        color, even for parts created before a color change.
        """
        filament_id = form.cleaned_data.get("spoolman_filament_id")
        if filament_id and self._spoolman_filaments:
            fil = self._spoolman_filaments.get(filament_id, {})
            if fil:
                color_hex = (fil.get("color_hex") or "")[:7]
                form.instance.color = color_hex
                form.instance.material = fil.get("material", "")
                # Keep the mapping's cached color in sync
                SpoolmanFilamentMapping.objects.filter(
                    spoolman_filament_id=filament_id,
                ).update(spoolman_color_hex=color_hex)


class PartDetailView(LoginRequiredMixin, DetailView):
    """Show details of a single part."""

    model = Part
    template_name = "core/part_detail.html"
    context_object_name = "part"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        part = self.object
        context["ancestors"] = part.project.get_ancestors()

        context["print_presets"] = OrcaPrintPreset.objects.filter(
            state=OrcaPrintPreset.STATE_RESOLVED,
            instantiation=True,
        )

        # Draft jobs the user can add this part to — only those whose
        # existing parts share the same effective preset and filament.
        effective_preset_id = part.effective_print_preset_id
        effective_filament_id = part.spoolman_filament_id

        draft_jobs = PrintJob.objects.filter(
            status=PrintJob.STATUS_DRAFT,
        ).prefetch_related("job_parts__part__project")

        compatible_jobs = []
        for job in draft_jobs:
            job_parts = job.job_parts.all()
            if not job_parts:
                # Empty job is always compatible
                compatible_jobs.append(job)
                continue
            compatible = all(
                jp.part.effective_print_preset_id == effective_preset_id
                and jp.part.spoolman_filament_id == effective_filament_id
                for jp in job_parts
            )
            if compatible:
                compatible_jobs.append(job)

        context["draft_jobs"] = compatible_jobs

        # Spoolman filament name lookup
        spoolman_filament_name = ""
        if part.spoolman_filament_id:
            mapping = SpoolmanFilamentMapping.objects.filter(
                spoolman_filament_id=part.spoolman_filament_id,
            ).first()
            if mapping and mapping.spoolman_filament_name:
                spoolman_filament_name = mapping.spoolman_filament_name
        context["spoolman_filament_name"] = spoolman_filament_name

        return context


class PartCreateView(_SpoolmanFilamentMixin, ProjectManageMixin, CreateView):
    """Create a new part within a project."""

    model = Part
    form_class = PartForm
    template_name = "core/part_form.html"

    def dispatch(self, request, *args, **kwargs):
        self.project = get_object_or_404(Project, pk=kwargs["project_pk"])
        return super().dispatch(request, *args, **kwargs)

    def get_form(self, form_class=None):
        form = super().get_form(form_class)
        # Print presets
        form.fields["print_preset"].queryset = OrcaPrintPreset.objects.filter(
            state=OrcaPrintPreset.STATE_RESOLVED,
            instantiation=True,
        )
        # Spoolman filament choices
        self.populate_spoolman_filaments(form)
        return form

    def form_valid(self, form):
        form.instance.project = self.project
        self.apply_spoolman_filament(form)

        response = super().form_valid(form)
        messages.success(self.request, "Part added successfully.")

        # Trigger background estimation slicing
        _trigger_part_estimation(self.object)

        if "_save_and_add_another" in self.request.POST:
            return redirect(reverse("core:part_create", kwargs={"project_pk": self.project.pk}))
        return response

    def get_success_url(self) -> str:
        """Redirect to the newly created part's detail page."""
        return reverse("core:part_detail", kwargs={"pk": self.object.pk})

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["project"] = self.project
        context["ancestors"] = self.project.get_ancestors()
        context["spoolman_configured"] = self._spoolman_configured
        context["spoolman_colors"] = self._spoolman_colors
        context["spoolman_colors_json"] = json.dumps(self._spoolman_colors)
        return context


class PartUpdateView(_SpoolmanFilamentMixin, ProjectManageMixin, UpdateView):
    """Update an existing part."""

    model = Part
    form_class = PartForm
    template_name = "core/part_form.html"

    def get_form(self, form_class=None):
        form = super().get_form(form_class)
        form.fields["print_preset"].queryset = OrcaPrintPreset.objects.filter(
            state=OrcaPrintPreset.STATE_RESOLVED,
            instantiation=True,
        )
        # Spoolman filament choices
        self.populate_spoolman_filaments(form)
        return form

    def get_success_url(self):
        return reverse_lazy("core:part_detail", kwargs={"pk": self.object.pk})

    def form_valid(self, form):
        self.apply_spoolman_filament(form)

        # Check if STL or preset changed — re-estimate if so
        stl_changed = "stl_file" in form.changed_data
        preset_changed = "print_preset" in form.changed_data
        filament_changed = "spoolman_filament_id" in form.changed_data
        needs_re_estimate = stl_changed or preset_changed or filament_changed

        messages.success(self.request, "Part updated successfully.")
        response = super().form_valid(form)

        if needs_re_estimate:
            # Clear old estimates so new ones are written
            Part.objects.filter(pk=self.object.pk).update(
                filament_used_grams=None,
                filament_used_meters=None,
                estimated_print_time=None,
                estimation_status=Part.ESTIMATION_NONE,
                estimation_error="",
            )
            _trigger_part_estimation(self.object)

        return response

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["ancestors"] = self.object.project.get_ancestors()
        context["spoolman_configured"] = self._spoolman_configured
        context["spoolman_colors"] = self._spoolman_colors
        context["spoolman_colors_json"] = json.dumps(self._spoolman_colors)
        return context


class PartDeleteView(ProjectManageMixin, DeleteView):
    """Delete a part."""

    model = Part
    template_name = "core/part_confirm_delete.html"

    def get_context_data(self, **kwargs):
        """Add breadcrumb ancestors to template context."""
        context = super().get_context_data(**kwargs)
        context["ancestors"] = self.object.project.get_ancestors()
        return context

    def get_success_url(self):
        return reverse_lazy("core:project_detail", kwargs={"pk": self.object.project.pk})

    def form_valid(self, form):
        messages.success(self.request, "Part deleted.")
        return super().form_valid(form)


class PartReEstimateView(ProjectManageMixin, View):
    """Re-trigger estimation for a single part.

    Clears existing estimates and starts a background estimation thread.
    Redirects back to the part detail page.
    """

    http_method_names = ["post"]

    def post(self, request: HttpRequest, pk: int) -> HttpResponse:
        """Handle POST request to re-estimate a single part.

        Args:
            request: The incoming HTTP request.
            pk: Primary key of the Part to re-estimate.

        Returns:
            Redirect to the part detail page.
        """
        part = get_object_or_404(Part, pk=pk)

        if not part.stl_file:
            messages.warning(request, "No model file — cannot estimate.")
            return redirect("core:part_detail", pk=part.pk)

        preset = part.effective_print_preset
        if not preset:
            messages.warning(request, "No print preset configured — cannot estimate.")
            return redirect("core:part_detail", pk=part.pk)

        Part.objects.filter(pk=part.pk).update(
            filament_used_grams=None,
            filament_used_meters=None,
            estimated_print_time=None,
            estimation_status=Part.ESTIMATION_NONE,
            estimation_error="",
        )
        _trigger_part_estimation(part)
        messages.info(request, f"Re-estimation started for '{part.name}'.")
        return redirect("core:part_detail", pk=part.pk)
