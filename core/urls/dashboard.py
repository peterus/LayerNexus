"""Dashboard URL patterns."""

from django.urls import path

from core.views import (
    AdminDashboardView,
    DashboardView,
    FarmDashboardView,
    StatisticsView,
)

urlpatterns = [
    path("", DashboardView.as_view(), name="dashboard"),
    path("farm/", FarmDashboardView.as_view(), name="farm_dashboard"),
    path("statistics/", StatisticsView.as_view(), name="statistics"),
    path("admin-dashboard/", AdminDashboardView.as_view(), name="admin_dashboard"),
]
