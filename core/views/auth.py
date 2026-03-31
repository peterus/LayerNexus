"""Authentication and user management views for the LayerNexus application."""

import logging
from typing import Any

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import login
from django.contrib.auth.mixins import LoginRequiredMixin
from django.contrib.auth.models import Group, User
from django.db import transaction
from django.db.models import QuerySet
from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect
from django.urls import reverse, reverse_lazy
from django.views.generic import CreateView, DeleteView, ListView, UpdateView

from core.forms import ProfileUpdateForm, UserManagementForm, UserRegistrationForm
from core.mixins import AdminRequiredMixin
from core.models import Part, PrintJob

logger = logging.getLogger(__name__)

__all__ = [
    "RegisterView",
    "ProfileView",
    "UserListView",
    "UserCreateView",
    "UserUpdateView",
    "UserDeleteView",
]


class RegisterView(CreateView):
    """User registration view.

    Registration can be disabled via the ALLOW_REGISTRATION setting.
    The very first user to register is automatically assigned the Admin role.
    """

    form_class = UserRegistrationForm
    template_name = "registration/register.html"
    success_url = reverse_lazy("core:dashboard")

    def dispatch(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        """Block access when registration is disabled."""
        if not getattr(settings, "ALLOW_REGISTRATION", True):
            messages.error(request, "Registration is currently disabled.")
            return redirect("login")
        return super().dispatch(request, *args, **kwargs)

    def form_valid(self, form: UserRegistrationForm) -> HttpResponse:
        """Save user, assign role group, and log in.

        Uses a transaction with select_for_update to prevent a race condition
        where two concurrent registrations could both see themselves as the
        first user and both receive Admin privileges.
        """
        with transaction.atomic():
            response = super().form_valid(form)
            user = self.object
            # Lock the Admin group row to serialize first-user checks.
            # get_or_create + select_for_update ensures only one transaction
            # can evaluate the "first user" condition at a time, even when
            # there are 0 pre-existing users (avoiding the empty-table race).
            Group.objects.select_for_update().get_or_create(name="Admin")
            is_first_user = User.objects.count() == 1
            if is_first_user:
                # First user becomes Admin
                group, _created = Group.objects.get_or_create(name="Admin")
                user.groups.add(group)
                user.is_staff = True
                user.is_superuser = True
                user.save(update_fields=["is_staff", "is_superuser"])
                messages.success(
                    self.request,
                    "Welcome! You are the first user and have been assigned the Admin role.",
                )
            else:
                # Default role for self-registered users: Designer
                group, _created = Group.objects.get_or_create(name="Designer")
                user.groups.add(group)
                messages.success(self.request, "Registration successful. Welcome!")
        login(self.request, user)
        return response


class ProfileView(LoginRequiredMixin, UpdateView):
    """User profile page — allows editing own name and email."""

    model = User
    form_class = ProfileUpdateForm
    template_name = "core/profile.html"
    success_url = reverse_lazy("core:profile")

    def get_object(self, queryset: QuerySet | None = None) -> User:
        """Return the currently logged-in user."""
        return self.request.user

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        """Add statistics to profile context."""
        context = super().get_context_data(**kwargs)
        context["total_parts"] = Part.objects.count()
        context["total_jobs"] = PrintJob.objects.count()
        return context

    def form_valid(self, form: ProfileUpdateForm) -> HttpResponse:
        """Save profile and show success message."""
        messages.success(self.request, "Profile updated.")
        return super().form_valid(form)


class UserListView(AdminRequiredMixin, ListView):
    """List all users with their roles."""

    model = User
    template_name = "core/user_list.html"
    context_object_name = "users"
    ordering = ["username"]


class UserCreateView(AdminRequiredMixin, CreateView):
    """Create a new user with role assignment."""

    model = User
    form_class = UserManagementForm
    template_name = "core/user_form.html"
    success_url = reverse_lazy("core:user_list")

    def form_valid(self, form: UserManagementForm) -> HttpResponse:
        """Save new user and show success message."""
        response = super().form_valid(form)
        messages.success(self.request, f"User '{form.instance.username}' created.")
        if "_save_and_add_another" in self.request.POST:
            return redirect(reverse("core:user_create"))
        return response


class UserUpdateView(AdminRequiredMixin, UpdateView):
    """Edit an existing user and their role."""

    model = User
    form_class = UserManagementForm
    template_name = "core/user_form.html"
    success_url = reverse_lazy("core:user_list")

    def form_valid(self, form: UserManagementForm) -> HttpResponse:
        """Save user changes and show success message."""
        messages.success(self.request, f"User '{form.instance.username}' updated.")
        return super().form_valid(form)


class UserDeleteView(AdminRequiredMixin, DeleteView):
    """Delete a user account."""

    model = User
    template_name = "core/user_confirm_delete.html"
    success_url = reverse_lazy("core:user_list")

    def form_valid(self, form: Any) -> HttpResponse:
        """Prevent self-deletion and show message."""
        if self.get_object() == self.request.user:
            messages.error(self.request, "You cannot delete your own account.")
            return redirect("core:user_list")
        messages.success(self.request, f"User '{self.get_object().username}' deleted.")
        return super().form_valid(form)
