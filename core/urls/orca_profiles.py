"""OrcaSlicer profile URL patterns."""

from django.urls import path

from core.views import (
    OrcaFilamentProfileDeleteView,
    OrcaFilamentProfileDetailView,
    OrcaFilamentProfileImportView,
    OrcaFilamentProfileListView,
    OrcaMachineProfileDeleteView,
    OrcaMachineProfileDetailView,
    OrcaMachineProfileImportView,
    OrcaMachineProfileListView,
    OrcaPrintPresetDeleteView,
    OrcaPrintPresetDetailView,
    OrcaPrintPresetImportView,
    OrcaPrintPresetListView,
)

urlpatterns = [
    # Machine Profiles
    path(
        "orca-machine-profiles/",
        OrcaMachineProfileListView.as_view(),
        name="orcamachineprofile_list",
    ),
    path(
        "orca-machine-profiles/import/",
        OrcaMachineProfileImportView.as_view(),
        name="orcamachineprofile_import",
    ),
    path(
        "orca-machine-profiles/<int:pk>/",
        OrcaMachineProfileDetailView.as_view(),
        name="orcamachineprofile_detail",
    ),
    path(
        "orca-machine-profiles/<int:pk>/delete/",
        OrcaMachineProfileDeleteView.as_view(),
        name="orcamachineprofile_delete",
    ),
    # Filament Profiles
    path(
        "orca-filament-profiles/",
        OrcaFilamentProfileListView.as_view(),
        name="orcafilamentprofile_list",
    ),
    path(
        "orca-filament-profiles/import/",
        OrcaFilamentProfileImportView.as_view(),
        name="orcafilamentprofile_import",
    ),
    path(
        "orca-filament-profiles/<int:pk>/",
        OrcaFilamentProfileDetailView.as_view(),
        name="orcafilamentprofile_detail",
    ),
    path(
        "orca-filament-profiles/<int:pk>/delete/",
        OrcaFilamentProfileDeleteView.as_view(),
        name="orcafilamentprofile_delete",
    ),
    # Print Presets
    path(
        "orca-print-presets/",
        OrcaPrintPresetListView.as_view(),
        name="orcaprintpreset_list",
    ),
    path(
        "orca-print-presets/import/",
        OrcaPrintPresetImportView.as_view(),
        name="orcaprintpreset_import",
    ),
    path(
        "orca-print-presets/<int:pk>/",
        OrcaPrintPresetDetailView.as_view(),
        name="orcaprintpreset_detail",
    ),
    path(
        "orca-print-presets/<int:pk>/delete/",
        OrcaPrintPresetDeleteView.as_view(),
        name="orcaprintpreset_delete",
    ),
]
