"""Project hardware views for the LayerNexus application."""

import logging

from django.contrib import messages
from django.db import IntegrityError
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.views.generic import DeleteView, FormView, UpdateView

from core.forms import ProjectHardwareForm, ProjectHardwareUpdateForm
from core.mixins import ProjectManageMixin
from core.models import Project, ProjectHardware

logger = logging.getLogger(__name__)

__all__ = [
    "ProjectHardwareCreateView",
    "ProjectHardwareUpdateView",
    "ProjectHardwareDeleteView",
]


class ProjectHardwareCreateView(ProjectManageMixin, FormView):
    """Add a hardware part to a project (select existing or create new)."""

    form_class = ProjectHardwareForm
    template_name = "core/projecthardware_form.html"

    def get_project(self) -> Project:
        """Return the project from the URL."""
        return get_object_or_404(Project, pk=self.kwargs["project_pk"])

    def get_context_data(self, **kwargs) -> dict:
        """Add project and breadcrumb ancestors to context."""
        context = super().get_context_data(**kwargs)
        project = self.get_project()
        context["project"] = project
        context["ancestors"] = project.get_ancestors()
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
        """Add project and breadcrumb ancestors to context."""
        context = super().get_context_data(**kwargs)
        project = self.object.project
        context["project"] = project
        context["ancestors"] = project.get_ancestors()
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
        """Add project and breadcrumb ancestors to context."""
        context = super().get_context_data(**kwargs)
        project = self.object.project
        context["project"] = project
        context["ancestors"] = project.get_ancestors()
        return context

    def form_valid(self, form) -> HttpResponse:
        """Show success message and remove the assignment."""
        name = self.object.hardware_part.name
        messages.success(self.request, f"Hardware '{name}' removed from project.")
        return super().form_valid(form)

    def get_success_url(self) -> str:
        """Redirect to the project detail page."""
        return reverse("core:project_detail", kwargs={"pk": self.object.project_id})
