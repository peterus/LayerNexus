"""Project hardware shopping list views."""

from itertools import groupby
from typing import Any

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.db.models import QuerySet
from django.http import Http404, HttpRequest, HttpResponse, HttpResponseBadRequest
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.views import View
from django.views.generic import DetailView

from core.mixins import ProjectManageMixin
from core.models import HardwareShoppingItem, Project
from core.models.hardware import ShoppingItemNotRequired

__all__ = [
    "ProjectShoppingListView",
    "ProjectShoppingItemSetView",
    "ProjectShoppingCartOrderedView",
    "ProjectShoppingResetView",
]


class ProjectShoppingListView(LoginRequiredMixin, DetailView):
    """Show the aggregated hardware shopping list of a project."""

    model = Project
    template_name = "core/project_shopping_list.html"
    context_object_name = "project"

    def get_queryset(self) -> QuerySet:
        """Prefetch the aggregate tree so sub-project recursion avoids N+1 queries."""
        return Project.objects.prefetch_related(*Project.aggregate_prefetch_lookups())

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        """Add rows, rows grouped by category and status counters."""
        context = super().get_context_data(**kwargs)
        rows = HardwareShoppingItem.build_list(self.object)
        context["rows"] = rows
        context["grouped_rows"] = [
            (label, list(group)) for label, group in groupby(rows, key=lambda r: r.hardware_part.get_category_display())
        ]
        context["counts"] = HardwareShoppingItem.counts(rows)
        return context


def _get_project(pk: int) -> Project:
    """Load a project with the aggregate tree prefetched for ``build_list``."""
    return get_object_or_404(Project.objects.prefetch_related(*Project.aggregate_prefetch_lookups()), pk=pk)


def _list_url(project: Project, anchor: str = "") -> str:
    """Return the shopping list URL of a project, optionally with an anchor."""
    return f"{reverse('core:project_shopping_list', args=[project.pk])}{anchor}"


class ProjectShoppingItemSetView(ProjectManageMixin, View):
    """Set the shopping status of a single hardware part (POST only)."""

    http_method_names = ["post"]

    def post(self, request: HttpRequest, pk: int, hardware_part_pk: int) -> HttpResponse:
        """Apply the submitted ``status`` and redirect back to the part's row."""
        project = _get_project(pk)
        try:
            row = HardwareShoppingItem.set_status(project, hardware_part_pk, request.POST.get("status", ""))
        except ValueError:
            return HttpResponseBadRequest("Invalid shopping status.")
        except ShoppingItemNotRequired as exc:
            raise Http404(str(exc)) from exc
        messages.success(request, f"{row.hardware_part.name}: {row.status.replace('_', ' ')}.")
        return redirect(_list_url(project, f"#hw-{hardware_part_pk}"))


class ProjectShoppingCartOrderedView(ProjectManageMixin, View):
    """Mark all in-cart items as ordered (POST only)."""

    http_method_names = ["post"]

    def post(self, request: HttpRequest, pk: int) -> HttpResponse:
        """Move every in-cart row to ordered."""
        project = _get_project(pk)
        count = HardwareShoppingItem.mark_cart_ordered(project)
        messages.success(request, f"{count} item(s) marked as ordered.")
        return redirect(_list_url(project))


class ProjectShoppingResetView(ProjectManageMixin, View):
    """Reset the whole shopping list to open (POST only)."""

    http_method_names = ["post"]

    def post(self, request: HttpRequest, pk: int) -> HttpResponse:
        """Delete all stored shopping statuses of the project."""
        project = _get_project(pk)
        count = HardwareShoppingItem.reset(project)
        messages.success(request, f"Shopping list reset ({count} item(s)).")
        return redirect(_list_url(project))
