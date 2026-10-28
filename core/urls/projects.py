"""Project URL patterns."""

from django.urls import path

from core.views import (
    ProjectCostView,
    ProjectCreateView,
    ProjectDeleteView,
    ProjectDetailView,
    ProjectListView,
    ProjectUpdateView,
    SubProjectCreateView,
)

urlpatterns = [
    path("projects/", ProjectListView.as_view(), name="project_list"),
    path("projects/new/", ProjectCreateView.as_view(), name="project_create"),
    path("projects/<int:pk>/", ProjectDetailView.as_view(), name="project_detail"),
    path(
        "projects/<int:pk>/edit/",
        ProjectUpdateView.as_view(),
        name="project_update",
    ),
    path(
        "projects/<int:pk>/delete/",
        ProjectDeleteView.as_view(),
        name="project_delete",
    ),
    path(
        "projects/<int:pk>/cost/",
        ProjectCostView.as_view(),
        name="project_cost",
    ),
    path(
        "projects/<int:parent_pk>/subprojects/new/",
        SubProjectCreateView.as_view(),
        name="subproject_create",
    ),
]
