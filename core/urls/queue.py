"""Print queue URL patterns."""

from django.urls import path

from core.views import (
    CancelQueueEntryView,
    PrintQueueCreateView,
    PrintQueueDeleteView,
    PrintQueueListView,
    QueueCheckPrinterStatusView,
    QueueEntryReviewView,
    RunAllQueuesView,
    RunNextQueueView,
)

urlpatterns = [
    path("queue/", PrintQueueListView.as_view(), name="printqueue_list"),
    path("queue/add/", PrintQueueCreateView.as_view(), name="printqueue_create"),
    path(
        "queue/<int:pk>/delete/",
        PrintQueueDeleteView.as_view(),
        name="printqueue_delete",
    ),
    path(
        "queue/run/<int:printer_pk>/",
        RunNextQueueView.as_view(),
        name="run_queue",
    ),
    path(
        "queue/run-all/",
        RunAllQueuesView.as_view(),
        name="run_all_queues",
    ),
    path(
        "queue/<int:pk>/review/",
        QueueEntryReviewView.as_view(),
        name="printqueue_review",
    ),
    path(
        "api/queue/<int:pk>/check-status/",
        QueueCheckPrinterStatusView.as_view(),
        name="queue_check_printer_status",
    ),
    path(
        "queue/<int:pk>/cancel/",
        CancelQueueEntryView.as_view(),
        name="printqueue_cancel",
    ),
]
