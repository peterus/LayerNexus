"""Service integration URL patterns."""

from django.urls import path

from core.views import (
    PrinterStatusView,
    SliceJobStatusView,
    SpoolmanFilamentsAPIView,
    SpoolmanSpoolsView,
    UploadToPrinterView,
)

urlpatterns = [
    path(
        "jobs/<int:pk>/slice-status/",
        SliceJobStatusView.as_view(),
        name="slice_job_status",
    ),
    path(
        "printers/<int:printer_pk>/spools/",
        SpoolmanSpoolsView.as_view(),
        name="spoolman_spools",
    ),
    path(
        "api/spoolman/filaments/",
        SpoolmanFilamentsAPIView.as_view(),
        name="spoolman_filaments_api",
    ),
    path(
        "printers/<int:printer_pk>/status/",
        PrinterStatusView.as_view(),
        name="printer_status",
    ),
    path(
        "plates/<int:plate_pk>/upload/",
        UploadToPrinterView.as_view(),
        name="upload_to_printer",
    ),
]
