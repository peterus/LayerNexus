"""Part URL patterns."""

from django.urls import path

from core.views import (
    CreateJobsFromProjectView,
    PartCopyView,
    PartCreateView,
    PartDeleteView,
    PartDetailView,
    PartReEstimateView,
    PartUpdateView,
    PrintedPartRecordCreateView,
    PrintedPartRecordDeleteView,
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
    path("parts/<int:pk>/copy/", PartCopyView.as_view(), name="part_copy"),
    path("parts/<int:pk>/re-estimate/", PartReEstimateView.as_view(), name="part_re_estimate"),
    path("parts/<int:pk>/printed-records/", PrintedPartRecordCreateView.as_view(), name="printed_record_create"),
    path(
        "parts/<int:pk>/printed-records/<int:record_pk>/delete/",
        PrintedPartRecordDeleteView.as_view(),
        name="printed_record_delete",
    ),
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
