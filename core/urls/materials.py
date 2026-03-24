"""Material and filament mapping URL patterns."""

from django.urls import path

from core.views import (
    MaterialProfileListView,
    SaveFilamentMappingView,
)

urlpatterns = [
    path(
        "materials/",
        MaterialProfileListView.as_view(),
        name="materialprofile_list",
    ),
    path(
        "materials/save-mapping/",
        SaveFilamentMappingView.as_view(),
        name="save_filament_mapping",
    ),
]
