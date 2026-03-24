"""Project document URL patterns."""

from django.urls import path

from core.views import (
    ProjectDocumentCreateView,
    ProjectDocumentDeleteView,
    ProjectDocumentDownloadView,
)

urlpatterns = [
    path(
        "projects/<int:project_pk>/documents/new/",
        ProjectDocumentCreateView.as_view(),
        name="document_create",
    ),
    path(
        "documents/<int:pk>/delete/",
        ProjectDocumentDeleteView.as_view(),
        name="document_delete",
    ),
    path(
        "documents/<int:pk>/download/",
        ProjectDocumentDownloadView.as_view(),
        name="document_download",
    ),
]
