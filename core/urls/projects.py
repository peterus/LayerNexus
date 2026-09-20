"""Project URL patterns."""

from django.urls import path

from core.views import (
    ProjectAddComponentView,
    ProjectAddPartView,
    ProjectComponentDeleteView,
    ProjectComponentUpdateView,
    ProjectCostView,
    ProjectCreateView,
    ProjectDeleteView,
    ProjectDetailView,
    ProjectDuplicateAsVariantView,
    ProjectListView,
    ProjectPartDeleteView,
    ProjectPartUpdateView,
    ProjectUpdateView,
    SubProjectCreateView,
)

urlpatterns = [
    path("projects/", ProjectListView.as_view(), name="project_list"),
    path("projects/new/", ProjectCreateView.as_view(), name="project_create"),
    path("projects/<int:pk>/", ProjectDetailView.as_view(), name="project_detail"),
    path(
        "projects/<int:pk>/components/add/",
        ProjectAddComponentView.as_view(),
        name="project_add_component",
    ),
    path(
        "projects/<int:pk>/parts/add/",
        ProjectAddPartView.as_view(),
        name="project_add_part",
    ),
    path(
        "components/<int:pk>/remove/",
        ProjectComponentDeleteView.as_view(),
        name="project_component_remove",
    ),
    path(
        "components/<int:pk>/quantity/",
        ProjectComponentUpdateView.as_view(),
        name="project_component_quantity",
    ),
    path(
        "project-parts/<int:pk>/remove/",
        ProjectPartDeleteView.as_view(),
        name="project_part_remove",
    ),
    path(
        "project-parts/<int:pk>/quantity/",
        ProjectPartUpdateView.as_view(),
        name="project_part_quantity",
    ),
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
        "projects/<int:pk>/duplicate/",
        ProjectDuplicateAsVariantView.as_view(),
        name="project_duplicate",
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
