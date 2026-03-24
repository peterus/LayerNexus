"""Project hardware URL patterns."""

from django.urls import path

from core.views import (
    ProjectHardwareCreateView,
    ProjectHardwareDeleteView,
    ProjectHardwareUpdateView,
)

urlpatterns = [
    path(
        "projects/<int:project_pk>/hardware/new/",
        ProjectHardwareCreateView.as_view(),
        name="hardware_create",
    ),
    path(
        "hardware/<int:pk>/edit/",
        ProjectHardwareUpdateView.as_view(),
        name="hardware_update",
    ),
    path(
        "hardware/<int:pk>/delete/",
        ProjectHardwareDeleteView.as_view(),
        name="hardware_delete",
    ),
]
