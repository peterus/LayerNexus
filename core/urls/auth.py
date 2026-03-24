"""Authentication and user management URL patterns."""

from django.urls import path

from core.views import (
    ProfileView,
    RegisterView,
    UserCreateView,
    UserDeleteView,
    UserListView,
    UserUpdateView,
)

urlpatterns = [
    path("register/", RegisterView.as_view(), name="register"),
    path("profile/", ProfileView.as_view(), name="profile"),
    # User Management (Admin)
    path("users/", UserListView.as_view(), name="user_list"),
    path("users/create/", UserCreateView.as_view(), name="user_create"),
    path("users/<int:pk>/edit/", UserUpdateView.as_view(), name="user_update"),
    path(
        "users/<int:pk>/delete/",
        UserDeleteView.as_view(),
        name="user_delete",
    ),
]
