"""Hardware URL patterns (catalogue library and project assignments)."""

from django.urls import path

from core.views import (
    HardwarePartCreateView,
    HardwarePartDeleteView,
    HardwarePartListView,
    HardwarePartUpdateView,
    ProjectHardwareCreateView,
    ProjectHardwareDeleteView,
    ProjectHardwareUpdateView,
)

urlpatterns = [
    # Catalogue library
    path(
        "hardware-library/",
        HardwarePartListView.as_view(),
        name="hardware_library",
    ),
    path(
        "hardware-library/new/",
        HardwarePartCreateView.as_view(),
        name="hardware_part_create",
    ),
    path(
        "hardware-library/<int:pk>/edit/",
        HardwarePartUpdateView.as_view(),
        name="hardware_part_update",
    ),
    path(
        "hardware-library/<int:pk>/delete/",
        HardwarePartDeleteView.as_view(),
        name="hardware_part_delete",
    ),
    # Project assignments
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
