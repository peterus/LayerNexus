"""URL routing for the iterative build API (mounted at ``/api/v1/``).

Top-level resources are registered on a ``DefaultRouter`` (which also provides the
browsable API root at ``/api/v1/``); composition edges, documents and hardware
assignments are nested under a project with explicit paths.
"""

from __future__ import annotations

from django.urls import include, path
from rest_framework.routers import DefaultRouter

from core.api.views import (
    HardwarePartViewSet,
    OrcaPrintPresetViewSet,
    PartViewSet,
    ProjectComponentDetail,
    ProjectComponentListCreate,
    ProjectDocumentDetail,
    ProjectDocumentListCreate,
    ProjectHardwareDetail,
    ProjectHardwareListCreate,
    ProjectPartDetail,
    ProjectPartListCreate,
    ProjectViewSet,
    SpoolmanFilamentMappingViewSet,
)

app_name = "api"

router = DefaultRouter()
router.register("projects", ProjectViewSet, basename="project")
router.register("parts", PartViewSet, basename="part")
router.register("hardware-parts", HardwarePartViewSet, basename="hardwarepart")
router.register("spoolman-filaments", SpoolmanFilamentMappingViewSet, basename="spoolman-filament")
router.register("print-presets", OrcaPrintPresetViewSet, basename="print-preset")

urlpatterns = [
    path("", include(router.urls)),
    path(
        "projects/<int:project_pk>/components/",
        ProjectComponentListCreate.as_view(),
        name="project-components",
    ),
    path(
        "projects/<int:project_pk>/components/<int:pk>/",
        ProjectComponentDetail.as_view(),
        name="project-component-detail",
    ),
    path(
        "projects/<int:project_pk>/parts/",
        ProjectPartListCreate.as_view(),
        name="project-parts",
    ),
    path(
        "projects/<int:project_pk>/parts/<int:pk>/",
        ProjectPartDetail.as_view(),
        name="project-part-detail",
    ),
    path(
        "projects/<int:project_pk>/documents/",
        ProjectDocumentListCreate.as_view(),
        name="project-documents",
    ),
    path(
        "projects/<int:project_pk>/documents/<int:pk>/",
        ProjectDocumentDetail.as_view(),
        name="project-document-detail",
    ),
    path(
        "projects/<int:project_pk>/hardware/",
        ProjectHardwareListCreate.as_view(),
        name="project-hardware",
    ),
    path(
        "projects/<int:project_pk>/hardware/<int:pk>/",
        ProjectHardwareDetail.as_view(),
        name="project-hardware-detail",
    ),
]
