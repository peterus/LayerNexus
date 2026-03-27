"""Bambu Lab Cloud account URL patterns."""

from django.urls import path

from core.views import (
    BambuAccountDeleteView,
    BambuAccountListView,
    BambuAccountRefreshView,
    BambuAccountStep1View,
    BambuAccountStep2View,
    BambuAccountStep3View,
)

urlpatterns = [
    # Auth wizard
    path("bambu/connect/", BambuAccountStep1View.as_view(), name="bambuaccount_step1"),
    path("bambu/verify/", BambuAccountStep2View.as_view(), name="bambuaccount_step2"),
    path("bambu/devices/", BambuAccountStep3View.as_view(), name="bambuaccount_step3"),
    # Account management
    path("bambu/accounts/", BambuAccountListView.as_view(), name="bambuaccount_list"),
    path("bambu/accounts/<int:pk>/delete/", BambuAccountDeleteView.as_view(), name="bambuaccount_delete"),
    path("bambu/accounts/<int:pk>/refresh/", BambuAccountRefreshView.as_view(), name="bambuaccount_refresh"),
]
