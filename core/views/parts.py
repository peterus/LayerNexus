"""Part views for the LayerNexus application."""

import json
import logging

from django import forms as django_forms
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.db import transaction
from django.db.models import Count, ProtectedError
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse, reverse_lazy
from django.views import View
from django.views.generic import CreateView, DeleteView, DetailView, FormView, UpdateView

from core.forms import PartCopyForm, PartForm
from core.mixins import ProjectManageMixin
from core.models import (
    OrcaPrintPreset,
    Part,
    PrintJob,
    Project,
    SpoolmanFilamentMapping,
)
from core.services.spoolman import SpoolmanClient, SpoolmanError
from core.views.helpers import _requeue_stale_estimates, _trigger_part_estimation

logger = logging.getLogger(__name__)

__all__ = [
    "PartDetailView",
    "PartCreateView",
    "PartUpdateView",
    "PartDeleteView",
    "PartReEstimateView",
    "PartCopyView",
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

        context["print_presets"] = OrcaPrintPreset.objects.filter(
            state=OrcaPrintPreset.STATE_RESOLVED,
            instantiation=True,
        )

        # Jobs the user can add this part to (not printing yet, not slicing) — those whose
        # pinned preset is the part's effective (bottom-up resolved) preset, and whose
        # filament matches.  Adding to a sliced job puts it back to draft.
        allowed_preset_ids = {part.effective_print_preset_id} - {None}
        effective_filament_id = part.spoolman_filament_id

        draft_jobs = PrintJob.objects.contents_editable().prefetch_related("job_parts__part")

        compatible_jobs = []
        for job in draft_jobs:
            job_parts = job.job_parts.all()
            if not job_parts:
                # Empty job is always compatible (it will be pinned when the part is added).
                compatible_jobs.append(job)
                continue
            filament_ok = all(jp.part.spoolman_filament_id == effective_filament_id for jp in job_parts)
            if job.print_preset_id is not None:
                preset_ok = job.print_preset_id in allowed_preset_ids
            else:
                # Legacy unpinned job: fall back to the existing parts' own presets.
                preset_ok = all(jp.part.effective_print_preset_id in allowed_preset_ids for jp in job_parts)
            if filament_ok and preset_ok:
                compatible_jobs.append(job)

        context["draft_jobs"] = compatible_jobs
        # One row per job (PrintJobPart is unique per job+part); plate count as an annotation, no N+1.
        context["part_jobs"] = part.print_jobs.with_display_status().annotate(n_plates=Count("plates"))

        # Spoolman filament name lookup
        spoolman_filament_name = ""
        if part.spoolman_filament_id:
            mapping = SpoolmanFilamentMapping.objects.filter(
                spoolman_filament_id=part.spoolman_filament_id,
            ).first()
            if mapping and mapping.spoolman_filament_name:
                spoolman_filament_name = mapping.spoolman_filament_name
        context["spoolman_filament_name"] = spoolman_filament_name

        context["ancestors"] = part.project.ancestors()

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
        self.apply_spoolman_filament(form)
        form.instance.project = self.project

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
        # Compare stored values: changed_data also flags a project key that was simply omitted.
        moved = form.initial.get("project") != self.object.project_id

        messages.success(self.request, "Part updated successfully.")
        response = super().form_valid(form)

        if needs_re_estimate:
            # Clear old estimates so new ones are written; not estimable -> just clear.
            if not _trigger_part_estimation(self.object):
                self.object.clear_estimate()
        elif moved:
            # Moved part: re-estimate only if the inherited preset differs in the new project.
            _requeue_stale_estimates([self.object])

        return response

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["project"] = self.object.project
        context["spoolman_configured"] = self._spoolman_configured
        context["spoolman_colors"] = self._spoolman_colors
        context["spoolman_colors_json"] = json.dumps(self._spoolman_colors)
        return context


class PartDeleteView(ProjectManageMixin, DeleteView):
    """Delete a part.

    Refused while an unfinished print job contains the part, and while the part
    has print history (it is in a job with print attempts) — the history must be
    kept.  The checks run again inside the delete transaction.
    """

    model = Part
    template_name = "core/part_confirm_delete.html"

    def get_context_data(self, **kwargs) -> dict:
        """Add the jobs with the part's print history and the unfinished jobs that block the delete."""
        context = super().get_context_data(**kwargs)
        context["history_jobs"] = list(self.object.history_jobs().order_by("-created_at"))
        context["blocking_jobs"] = list(self.object.blocking_jobs().order_by("-created_at"))
        return context

    def form_valid(self, form: django_forms.Form) -> HttpResponse:
        """Delete the part unless print history or an unfinished print job still needs it.

        Returns:
            Redirect to the part's project, or back to the confirmation page when
            the delete is blocked.
        """
        with transaction.atomic():
            if self.object.has_print_history():
                messages.error(self.request, "This part cannot be deleted: it has print history, which must be kept.")
                return redirect("core:part_delete", pk=self.object.pk)
            if self.object.blocking_jobs().exists():
                messages.error(
                    self.request,
                    "This part cannot be deleted while unfinished print jobs contain it. "
                    "Finish or remove those jobs first.",
                )
                return redirect("core:part_delete", pk=self.object.pk)
            project_id = self.object.project_id
            try:
                self.object.delete()
            except ProtectedError:
                messages.error(self.request, "This part cannot be deleted: print jobs still contain it.")
                return redirect("core:part_delete", pk=self.object.pk)
        messages.success(self.request, "Part deleted.")
        return redirect("core:project_detail", pk=project_id)


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

        if not part.is_estimable():
            messages.warning(request, "No print preset configured — cannot estimate.")
            return redirect("core:part_detail", pk=part.pk)

        _trigger_part_estimation(part)
        messages.info(request, f"Re-estimation started for '{part.name}'.")
        return redirect("core:part_detail", pk=part.pk)


class PartCopyView(ProjectManageMixin, FormView):
    """Deep-copy a part into a project (content, never history)."""

    form_class = PartCopyForm
    template_name = "core/part_copy.html"

    def get_source(self) -> Part:
        """Return the part being copied (cached per request)."""
        if not hasattr(self, "_source"):
            self._source = get_object_or_404(Part.objects.select_related("project"), pk=self.kwargs["pk"])
        return self._source

    def get_form_kwargs(self) -> dict:
        """Pass the source part to the form."""
        kwargs = super().get_form_kwargs()
        kwargs["source"] = self.get_source()
        return kwargs

    def get_context_data(self, **kwargs) -> dict:
        """Add the source part and its project's ancestors for the breadcrumb."""
        context = super().get_context_data(**kwargs)
        part = self.get_source()
        context["part"] = part
        context["ancestors"] = part.project.ancestors()
        return context

    def form_valid(self, form: PartCopyForm) -> HttpResponse:
        """Copy the part and re-queue its estimate if the effective preset changed."""
        source = self.get_source()
        new = source.deep_copy(project=form.cleaned_data["project"])
        count = _requeue_stale_estimates([new])
        suffix = " Re-estimation queued." if count else ""
        messages.success(self.request, f'Copied part "{source.name}" to "{new.project.name}".{suffix}')
        return redirect("core:part_detail", pk=new.pk)
