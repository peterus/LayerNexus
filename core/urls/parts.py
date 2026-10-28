"""Part URL patterns."""

from django.urls import path

from core.views import (
    CreateJobsFromProjectView,
    PartCreateView,
    PartDeleteView,
    PartDetailView,
    PartReEstimateView,
    PartUpdateView,
    ProjectReEstimateView,
)

urlpatterns = [
    path(
        "projects/<int:project_pk>/parts/new/",
        PartCreateView.as_view(),
        name="part_create",
    ),
    path("parts/<int:pk>/", PartDetailView.as_view(), name="part_detail"),
    path("parts/<int:pk>/edit/", PartUpdateView.as_view(), name="part_update"),
    path("parts/<int:pk>/delete/", PartDeleteView.as_view(), name="part_delete"),
    path("parts/<int:pk>/re-estimate/", PartReEstimateView.as_view(), name="part_re_estimate"),
    path(
        "projects/<int:pk>/re-estimate/",
        ProjectReEstimateView.as_view(),
        name="project_re_estimate",
    ),
    path(
        "projects/<int:pk>/create-jobs/",
        CreateJobsFromProjectView.as_view(),
        name="project_create_jobs",
    ),
]
