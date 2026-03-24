"""Printer profile URL patterns."""

from django.urls import path

from core.views import (
    CostProfileUpdateView,
    PrinterProfileCreateView,
    PrinterProfileDeleteView,
    PrinterProfileListView,
    PrinterProfileUpdateView,
)

urlpatterns = [
    path("printers/", PrinterProfileListView.as_view(), name="printerprofile_list"),
    path(
        "printers/new/",
        PrinterProfileCreateView.as_view(),
        name="printerprofile_create",
    ),
    path(
        "printers/<int:pk>/edit/",
        PrinterProfileUpdateView.as_view(),
        name="printerprofile_update",
    ),
    path(
        "printers/<int:pk>/delete/",
        PrinterProfileDeleteView.as_view(),
        name="printerprofile_delete",
    ),
    path(
        "printers/<int:printer_pk>/cost/",
        CostProfileUpdateView.as_view(),
        name="costprofile_update",
    ),
]
