"""Print job URL patterns."""

from django.urls import path

from core.views import (
    AddPartToJobView,
    PrintJobCloseView,
    PrintJobCreateView,
    PrintJobDeleteView,
    PrintJobDetailView,
    PrintJobEnqueueAllView,
    PrintJobListView,
    PrintJobReopenView,
    PrintJobResliceRequeueView,
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
        "jobs/<int:pk>/reslice-requeue/",
        PrintJobResliceRequeueView.as_view(),
        name="printjob_reslice_requeue",
    ),
    path(
        "jobs/<int:pk>/close/",
        PrintJobCloseView.as_view(),
        name="printjob_close",
    ),
    path(
        "jobs/<int:pk>/reopen/",
        PrintJobReopenView.as_view(),
        name="printjob_reopen",
    ),
    path(
        "jobs/<int:pk>/enqueue-all/",
        PrintJobEnqueueAllView.as_view(),
        name="printjob_enqueue_all",
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
