"""Project views for the LayerNexus application."""

import logging

from django import forms as django_forms
from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.db import transaction
from django.db.models import QuerySet
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.views import View
from django.views.generic import CreateView, DeleteView, DetailView, ListView, UpdateView

from core.forms import ProjectEditForm, ProjectForm, SubProjectForm
from core.mixins import ProjectManageMixin
from core.models import (
    HardwareShoppingItem,
    OrcaPrintPreset,
    Part,
    PrintJob,
    PrintJobPart,
    Project,
    SpoolmanFilamentMapping,
)
from core.views.helpers import _trigger_part_estimation, _user_projects_qs

logger = logging.getLogger(__name__)

__all__ = [
    "ProjectListView",
    "ProjectDetailView",
    "ProjectCreateView",
    "SubProjectCreateView",
    "ProjectUpdateView",
    "ProjectDeleteView",
    "ProjectCostView",
    "ProjectReEstimateView",
]


class ProjectListView(LoginRequiredMixin, ListView):
    """List all top-level projects (sub-projects are excluded)."""

    model = Project
    template_name = "core/project_list.html"
    context_object_name = "projects"

    def get_queryset(self) -> QuerySet:
        """Return only top-level projects (``parent`` is NULL).

        Prefetches the sub-project/part/job tree so the per-card status badges
        and progress bars are computed from cache instead of issuing a query
        per project (and per part) — see
        :meth:`Project.aggregate_prefetch_lookups`.
        """
        return Project.objects.filter(parent__isnull=True).prefetch_related(*Project.aggregate_prefetch_lookups())


class ProjectDetailView(LoginRequiredMixin, DetailView):
    """Show details of a single project."""

    model = Project
    template_name = "core/project_detail.html"
    context_object_name = "project"

    def get_queryset(self) -> QuerySet:
        """Prefetch the aggregate tree so status/progress for the project and its
        sub-projects render without an N+1 explosion."""
        return Project.objects.prefetch_related(*Project.aggregate_prefetch_lookups())

    def get_context_data(self, **kwargs) -> dict:
        """Add filament requirements, build progress, the ancestor chain and lookups."""
        context = super().get_context_data(**kwargs)
        project = self.object
        context["filament_requirements"] = project.filament_requirements()
        context["ancestors"] = project.ancestors()
        context["subprojects"] = list(project.subprojects.all())
        context["parts"] = list(project.parts.all())

        build_progress = project.build_progress()
        context["build_progress"] = build_progress
        # Per-part rows keyed by part pk, so the parts table can show printed/remaining.
        context["build_rows_by_pk"] = {row["part"].pk: row for row in build_progress["parts"]}

        # Expand the build-progress section by default when prints exist or an unfinished
        # job contains a part of this subtree.
        has_active_job_parts: bool = (
            PrintJobPart.objects.filter(part__project_id__in=project.subtree_ids())
            .exclude(print_job__status__in=PrintJob.TERMINAL_STATUSES)
            .exists()
        )
        context["build_progress_expanded"] = build_progress["printed"] > 0 or has_active_job_parts

        # Build filament name and color lookups for part display
        parts = context["parts"]
        filament_ids = {p.spoolman_filament_id for p in parts if p.spoolman_filament_id}
        filament_names: dict[int, str] = {}
        filament_colors: dict[int, str] = {}
        if filament_ids:
            for m in SpoolmanFilamentMapping.objects.filter(
                spoolman_filament_id__in=filament_ids,
            ):
                if m.spoolman_filament_name:
                    filament_names[m.spoolman_filament_id] = m.spoolman_filament_name
                if m.spoolman_color_hex:
                    filament_colors[m.spoolman_filament_id] = m.spoolman_color_hex
        context["filament_names"] = filament_names
        context["filament_colors"] = filament_colors

        # Effective default print preset (own or inherited from parent)
        effective_preset = project.effective_default_print_preset
        context["effective_default_print_preset"] = effective_preset
        context["preset_inherited"] = effective_preset is not None and project.default_print_preset_id is None

        # Documents (own + aggregated from sub-projects)
        context["documents"] = project.documents.all()
        context["all_documents"] = project._collect_documents()

        # Hardware (own + aggregated from sub-projects)
        context["hardware_assignments"] = project.hardware_assignments.select_related("hardware_part").all()
        shopping_rows = HardwareShoppingItem.build_list(project)
        context["shopping_counts"] = HardwareShoppingItem.counts(shopping_rows) if shopping_rows else None
        context["total_hardware_cost"] = project.total_hardware_cost

        return context


class ProjectCreateView(ProjectManageMixin, CreateView):
    """Create a new project."""

    model = Project
    form_class = ProjectForm
    template_name = "core/project_form.html"

    def get_form(self, form_class: type[ProjectForm] | None = None) -> ProjectForm:
        """Return form instance with queryset filtered to resolved slicer profiles."""
        form = super().get_form(form_class)
        form.fields["default_print_preset"].queryset = OrcaPrintPreset.objects.filter(
            state=OrcaPrintPreset.STATE_RESOLVED,
            instantiation=True,
        )
        return form

    def form_valid(self, form: ProjectForm) -> HttpResponse:
        """Set created_by to current user and save project."""
        form.instance.created_by = self.request.user
        response = super().form_valid(form)
        messages.success(self.request, "Project created successfully.")
        if "_save_and_add_another" in self.request.POST:
            return redirect(reverse("core:project_create"))
        return response

    def get_success_url(self) -> str:
        """Redirect to the newly created project's detail page."""
        return reverse("core:project_detail", kwargs={"pk": self.object.pk})


class SubProjectCreateView(ProjectManageMixin, CreateView):
    """Create a new sub-project under a parent project."""

    model = Project
    form_class = SubProjectForm
    template_name = "core/subproject_form.html"

    def get_parent(self) -> Project:
        """Return the parent project from the URL."""
        return get_object_or_404(Project, pk=self.kwargs["parent_pk"])

    def get_context_data(self, **kwargs) -> dict:
        """Add the parent project to template context."""
        context = super().get_context_data(**kwargs)
        context["parent"] = self.get_parent()
        return context

    def get_form(self, form_class=None) -> SubProjectForm:
        """Return form with resolved print preset choices."""
        form = super().get_form(form_class)
        form.fields["default_print_preset"].queryset = OrcaPrintPreset.objects.filter(
            state=OrcaPrintPreset.STATE_RESOLVED,
            instantiation=True,
        )
        return form

    def form_valid(self, form: SubProjectForm) -> HttpResponse:
        """Create the sub-project under the parent project (quantity comes from the form)."""
        form.instance.parent = self.get_parent()
        form.instance.created_by = self.request.user
        messages.success(self.request, "Sub-project created successfully.")
        return super().form_valid(form)

    def get_success_url(self) -> str:
        """Redirect to the parent project detail page after creation."""
        return reverse("core:project_detail", kwargs={"pk": self.kwargs["parent_pk"]})


class ProjectUpdateView(ProjectManageMixin, UpdateView):
    """Update an existing project."""

    model = Project
    form_class = ProjectEditForm
    template_name = "core/project_form.html"

    def get_form(self, form_class=None) -> ProjectEditForm:
        """Return the edit form with resolved print-preset choices."""
        form = super().get_form(form_class)
        form.fields["default_print_preset"].queryset = OrcaPrintPreset.objects.filter(
            state=OrcaPrintPreset.STATE_RESOLVED,
            instantiation=True,
        )
        return form

    def form_valid(self, form: ProjectEditForm) -> HttpResponse:
        """Save updated project with success message."""
        messages.success(self.request, "Project updated successfully.")
        return super().form_valid(form)

    def get_success_url(self) -> str:
        """Redirect to the edited project's detail page."""
        return reverse("core:project_detail", kwargs={"pk": self.object.pk})


class ProjectDeleteView(ProjectManageMixin, DeleteView):
    """Delete a project together with its whole subtree.

    Sub-projects, parts, documents and hardware assignments go with it (CASCADE).
    The delete is refused while any unfinished print job contains a part of the
    subtree; the check runs again inside the delete transaction.
    """

    model = Project
    template_name = "core/project_confirm_delete.html"

    def get_context_data(self, **kwargs) -> dict:
        """Add the deletion counts and the jobs that block the delete."""
        context = super().get_context_data(**kwargs)
        context["summary"] = self.object.deletion_summary()
        context["blocking_jobs"] = list(self.object.blocking_jobs().order_by("-created_at"))
        return context

    def form_valid(self, form: django_forms.Form) -> HttpResponse:
        """Delete the subtree unless an unfinished print job still uses it.

        Returns:
            Redirect to the parent project (or the project list for a root), or
            back to the confirmation page when the delete is blocked.
        """
        with transaction.atomic():
            if self.object.blocking_jobs().exists():
                messages.error(
                    self.request,
                    "This project cannot be deleted while unfinished print jobs contain its parts. "
                    "Finish, cancel or remove those jobs first.",
                )
                return redirect("core:project_delete", pk=self.object.pk)
            parent_id = self.object.parent_id
            self.object.delete()
        messages.success(self.request, "Project deleted.")
        if parent_id is not None:
            return redirect("core:project_detail", pk=parent_id)
        return redirect("core:project_list")


class ProjectCostView(LoginRequiredMixin, DetailView):
    """Show cost breakdown for a project."""

    model = Project
    template_name = "core/project_cost.html"
    context_object_name = "project"

    def get_queryset(self):
        return _user_projects_qs(self.request.user)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        project = self.object
        cost_breakdown = []
        total_cost = 0

        for part in project.parts.all():
            qty = part.quantity
            part_costs = {"part": part, "quantity": qty, "cost": None}
            if part.filament_used_grams and part.estimated_print_time:
                hours = part.estimated_print_time.total_seconds() / 3600
                # Try to find a cost profile from recent print jobs
                recent_job = part.print_jobs.filter(printer__isnull=False).first()
                if recent_job and hasattr(recent_job.printer, "cost_profile"):
                    cp = recent_job.printer.cost_profile
                    part_costs["cost"] = cp.calculate_print_cost(
                        hours * qty,
                        part.filament_used_grams * qty,
                    )
                    total_cost += part_costs["cost"]["total_cost"]
            cost_breakdown.append(part_costs)

        context["cost_breakdown"] = cost_breakdown
        context["total_cost"] = round(total_cost, 2)

        # Hardware costs
        context["hardware_requirements"] = project.hardware_requirements()
        context["total_hardware_cost"] = project.total_hardware_cost
        context["grand_total"] = round(total_cost + project.total_hardware_cost, 2)

        return context


class ProjectReEstimateView(ProjectManageMixin, View):
    """Re-trigger estimation for all parts in a project (including sub-projects).

    Clears existing estimates and starts background estimation threads
    for every part that has an STL file and a print preset.
    Redirects back to the project detail page.
    """

    http_method_names = ["post"]

    def post(self, request: HttpRequest, pk: int) -> HttpResponse:
        """Handle POST request to re-estimate all parts in a project.

        Args:
            request: The incoming HTTP request.
            pk: Primary key of the Project to re-estimate.

        Returns:
            Redirect to the project detail page.
        """
        project = get_object_or_404(Project, pk=pk)
        count = 0
        for part, _needed in project._collect_parts_with_multiplier():
            if not part.stl_file or part.effective_print_preset_id is None:
                continue
            Part.objects.filter(pk=part.pk).update(
                filament_used_grams=None,
                filament_used_meters=None,
                estimated_print_time=None,
                estimation_status=Part.ESTIMATION_NONE,
                estimation_error="",
                estimated_with_preset=None,
            )
            _trigger_part_estimation(part)
            count += 1

        if count:
            messages.info(request, f"Re-estimation started for {count} part(s).")
        else:
            messages.warning(request, "No parts eligible for estimation.")
        return redirect("core:project_detail", pk=project.pk)
