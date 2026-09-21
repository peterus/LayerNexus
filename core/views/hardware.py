"""Project hardware views for the LayerNexus application."""

import logging

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.db import IntegrityError
from django.db.models import Count, Q, QuerySet
from django.db.models.deletion import ProtectedError
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse, reverse_lazy
from django.views.generic import CreateView, DeleteView, FormView, ListView, UpdateView

from core.forms import ProjectHardwareForm, ProjectHardwareUpdateForm
from core.forms.hardware import HardwarePartForm
from core.mixins import ProjectManageMixin
from core.models import HardwarePart, Project, ProjectHardware

logger = logging.getLogger(__name__)

__all__ = [
    "HardwarePartListView",
    "HardwarePartCreateView",
    "HardwarePartUpdateView",
    "HardwarePartDeleteView",
    "ProjectHardwareCreateView",
    "ProjectHardwareUpdateView",
    "ProjectHardwareDeleteView",
]


class HardwarePartListView(LoginRequiredMixin, ListView):
    """Read-only library of all hardware catalogue entries."""

    model = HardwarePart
    template_name = "core/hardware_library.html"
    context_object_name = "hardware_parts"
    paginate_by = 50

    def get_queryset(self) -> QuerySet:
        """Return hardware parts annotated with project usage count, filtered by search/category."""
        queryset = HardwarePart.objects.annotate(used_in_count=Count("project_assignments", distinct=True)).order_by(
            "category", "name"
        )
        query = self.request.GET.get("q", "").strip()
        if query:
            queryset = queryset.filter(Q(name__icontains=query))
        category = self.request.GET.get("category", "").strip()
        if category:
            queryset = queryset.filter(category=category)
        return queryset

    def get_context_data(self, **kwargs) -> dict:
        """Add search term, active category filter, and all categories to context."""
        context = super().get_context_data(**kwargs)
        context["q"] = self.request.GET.get("q", "").strip()
        context["selected_category"] = self.request.GET.get("category", "").strip()
        context["categories"] = HardwarePart.CATEGORY_CHOICES
        return context


class HardwarePartCreateView(ProjectManageMixin, CreateView):
    """Create a new hardware catalogue entry."""

    model = HardwarePart
    form_class = HardwarePartForm
    template_name = "core/hardwarepart_form.html"

    def form_valid(self, form: HardwarePartForm) -> HttpResponse:
        """Set created_by and save."""
        form.instance.created_by = self.request.user
        response = super().form_valid(form)
        messages.success(self.request, f"Hardware part '{self.object.name}' created.")
        return response

    def get_success_url(self) -> str:
        """Redirect to the hardware library after creation."""
        return reverse_lazy("core:hardware_library")

    def get_context_data(self, **kwargs) -> dict:
        """Add mode flag for template."""
        context = super().get_context_data(**kwargs)
        context["is_edit"] = False
        return context


class HardwarePartUpdateView(ProjectManageMixin, UpdateView):
    """Edit an existing hardware catalogue entry."""

    model = HardwarePart
    form_class = HardwarePartForm
    template_name = "core/hardwarepart_form.html"

    def form_valid(self, form: HardwarePartForm) -> HttpResponse:
        """Save and show success message."""
        response = super().form_valid(form)
        messages.success(self.request, f"Hardware part '{self.object.name}' updated.")
        return response

    def get_success_url(self) -> str:
        """Redirect to the hardware library after update."""
        return reverse_lazy("core:hardware_library")

    def get_context_data(self, **kwargs) -> dict:
        """Add mode flag for template."""
        context = super().get_context_data(**kwargs)
        context["is_edit"] = True
        return context


class HardwarePartDeleteView(ProjectManageMixin, DeleteView):
    """Delete a hardware catalogue entry.

    Deletion is blocked if any project still references the part.
    """

    model = HardwarePart
    template_name = "core/hardwarepart_confirm_delete.html"
    context_object_name = "hardware_part"
    success_url = reverse_lazy("core:hardware_library")

    def get_context_data(self, **kwargs) -> dict:
        """Add project usage count to context."""
        context = super().get_context_data(**kwargs)
        context["used_in_count"] = self.object.project_assignments.count()
        context["used_in_projects"] = self.object.project_assignments.select_related("project").values_list(
            "project__name", flat=True
        )
        return context

    def form_valid(self, form) -> HttpResponse:
        """Delete the part, or show an error if it is still referenced by projects.

        The PROTECT constraint on ProjectHardware.hardware_part means the database
        itself enforces the guard, making it race-condition-safe on all backends.
        """
        name = self.object.name
        try:
            response = super().form_valid(form)
            messages.success(self.request, f"Hardware part '{name}' deleted.")
            return response
        except ProtectedError:
            used_count = self.object.project_assignments.count()
            messages.error(
                self.request,
                f"Cannot delete '{name}': it is used in {used_count} "
                f"project{'s' if used_count != 1 else ''}. Remove it from all projects first.",
            )
            return redirect(reverse("core:hardware_part_delete", kwargs={"pk": self.object.pk}))


class ProjectHardwareCreateView(ProjectManageMixin, FormView):
    """Add a hardware part to a project (select existing or create new)."""

    form_class = ProjectHardwareForm
    template_name = "core/projecthardware_form.html"

    def get_project(self) -> Project:
        """Return the project from the URL."""
        return get_object_or_404(Project, pk=self.kwargs["project_pk"])

    def get_context_data(self, **kwargs) -> dict:
        """Add project and edge-based "used in" assemblies to context."""
        context = super().get_context_data(**kwargs)
        project = self.get_project()
        context["project"] = project
        context["used_in"] = project.parent_assemblies()
        return context

    def form_valid(self, form: ProjectHardwareForm) -> HttpResponse:
        """Create or link hardware part and redirect."""
        project = self.get_project()
        try:
            ph = form.save(project=project, user=self.request.user)
            messages.success(
                self.request,
                f"Hardware '{ph.hardware_part.name}' (×{ph.quantity}) added to project.",
            )
        except IntegrityError:
            messages.error(
                self.request,
                "This hardware part is already assigned to this project.",
            )
        except Exception:
            logger.exception("Unexpected error while adding hardware to project %s", project.pk)
            messages.error(
                self.request,
                "An unexpected error occurred while adding the hardware part. "
                "Please try again or contact an administrator.",
            )
            raise
        return redirect(self.get_success_url())

    def get_success_url(self) -> str:
        """Redirect to the project detail page."""
        return reverse("core:project_detail", kwargs={"pk": self.kwargs["project_pk"]})


class ProjectHardwareUpdateView(ProjectManageMixin, UpdateView):
    """Edit a hardware assignment (quantity, notes, and hardware part details)."""

    model = ProjectHardware
    form_class = ProjectHardwareUpdateForm
    template_name = "core/projecthardware_form.html"
    context_object_name = "assignment"

    def get_context_data(self, **kwargs) -> dict:
        """Add project and edge-based "used in" assemblies to context."""
        context = super().get_context_data(**kwargs)
        project = self.object.project
        context["project"] = project
        context["used_in"] = project.parent_assemblies()
        context["is_edit"] = True
        return context

    def form_valid(self, form: ProjectHardwareUpdateForm) -> HttpResponse:
        """Save and show success message."""
        response = super().form_valid(form)
        messages.success(self.request, f"Hardware '{self.object.hardware_part.name}' updated.")
        return response

    def get_success_url(self) -> str:
        """Redirect to the project detail page."""
        return reverse("core:project_detail", kwargs={"pk": self.object.project_id})


class ProjectHardwareDeleteView(ProjectManageMixin, DeleteView):
    """Remove a hardware assignment from a project (does not delete the HardwarePart)."""

    model = ProjectHardware
    template_name = "core/projecthardware_confirm_delete.html"
    context_object_name = "assignment"

    def get_context_data(self, **kwargs) -> dict:
        """Add project and edge-based "used in" assemblies to context."""
        context = super().get_context_data(**kwargs)
        project = self.object.project
        context["project"] = project
        context["used_in"] = project.parent_assemblies()
        return context

    def form_valid(self, form) -> HttpResponse:
        """Show success message and remove the assignment."""
        name = self.object.hardware_part.name
        messages.success(self.request, f"Hardware '{name}' removed from project.")
        return super().form_valid(form)

    def get_success_url(self) -> str:
        """Redirect to the project detail page."""
        return reverse("core:project_detail", kwargs={"pk": self.object.project_id})
