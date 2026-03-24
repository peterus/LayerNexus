"""Print job URL patterns."""

from django.urls import path

from core.views import (
    AddPartToJobView,
    PrintJobCreateView,
    PrintJobDeleteView,
    PrintJobDetailView,
    PrintJobListView,
    PrintJobSliceView,
    PrintJobUpdateView,
    RemovePartFromJobView,
)

urlpatterns = [
    path("jobs/", PrintJobListView.as_view(), name="printjob_list"),
    path("jobs/new/", PrintJobCreateView.as_view(), name="printjob_create"),
    path(
        "jobs/<int:pk>/",
        PrintJobDetailView.as_view(),
        name="printjob_detail",
    ),
    path(
        "jobs/<int:pk>/edit/",
        PrintJobUpdateView.as_view(),
        name="printjob_update",
    ),
    path(
        "jobs/<int:pk>/delete/",
        PrintJobDeleteView.as_view(),
        name="printjob_delete",
    ),
    path(
        "jobs/<int:pk>/slice/",
        PrintJobSliceView.as_view(),
        name="printjob_slice",
    ),
    path(
        "jobs/<int:job_pk>/remove-part/<int:job_part_pk>/",
        RemovePartFromJobView.as_view(),
        name="printjob_remove_part",
    ),
    path(
        "parts/<int:part_pk>/add-to-job/",
        AddPartToJobView.as_view(),
        name="add_part_to_job",
    ),
]
