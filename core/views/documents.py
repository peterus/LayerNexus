"""Project document views for the LayerNexus application."""

import logging
from pathlib import Path

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.http import FileResponse, HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404
from django.urls import reverse
from django.views import View
from django.views.generic import CreateView, DeleteView

from core.forms import ProjectDocumentForm
from core.mixins import ProjectManageMixin
from core.models import Project, ProjectDocument

logger = logging.getLogger(__name__)

__all__ = [
    "ProjectDocumentCreateView",
    "ProjectDocumentDeleteView",
    "ProjectDocumentDownloadView",
]


class ProjectDocumentCreateView(ProjectManageMixin, CreateView):
    """Upload a document to a project."""

    model = ProjectDocument
    form_class = ProjectDocumentForm
    template_name = "core/projectdocument_form.html"

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

    def form_valid(self, form: ProjectDocumentForm) -> HttpResponse:
        """Set project and uploaded_by before saving."""
        form.instance.project = self.get_project()
        form.instance.uploaded_by = self.request.user
        response = super().form_valid(form)
        messages.success(self.request, f"Document '{form.instance.name}' uploaded.")
        return response

    def get_success_url(self) -> str:
        """Redirect to the project detail page."""
        return reverse("core:project_detail", kwargs={"pk": self.kwargs["project_pk"]})


class ProjectDocumentDeleteView(ProjectManageMixin, DeleteView):
    """Delete a document from a project."""

    model = ProjectDocument
    template_name = "core/projectdocument_confirm_delete.html"
    context_object_name = "document"

    def get_context_data(self, **kwargs) -> dict:
        """Add project and breadcrumb ancestors to context."""
        context = super().get_context_data(**kwargs)
        project = self.object.project
        context["project"] = project
        context["ancestors"] = project.get_ancestors()
        return context

    def form_valid(self, form) -> HttpResponse:
        """Show success message and delete the document."""
        messages.success(self.request, f"Document '{self.object.name}' deleted.")
        return super().form_valid(form)

    def get_success_url(self) -> str:
        """Redirect to the project detail page."""
        return reverse("core:project_detail", kwargs={"pk": self.object.project_id})


class ProjectDocumentDownloadView(LoginRequiredMixin, View):
    """Serve a project document file with authentication enforcement."""

    def get(self, request: HttpRequest, pk: int) -> FileResponse:
        """Stream the document file to the authenticated user."""
        document = get_object_or_404(ProjectDocument, pk=pk)
        return FileResponse(
            document.file.open("rb"),
            as_attachment=True,
            filename=Path(document.file.name).name,
        )
