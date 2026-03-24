"""Authentication and user management forms."""

from typing import Any

from django import forms
from django.contrib.auth.forms import UserCreationForm
from django.contrib.auth.models import Group, User

__all__ = [
    "UserRegistrationForm",
    "UserManagementForm",
    "ProfileUpdateForm",
]


class UserRegistrationForm(UserCreationForm):
    """Extended user registration form with email field."""

    email = forms.EmailField(required=True)

    class Meta:
        model = User
        fields = ["username", "email", "password1", "password2"]


class UserManagementForm(forms.ModelForm):
    """Admin form for creating/editing users with role assignment.

    Allows assigning users to one of the three role groups:
    Admin, Operator, or Designer.
    """

    ROLE_CHOICES = [
        ("", "— Select a role —"),
        ("Admin", "Admin"),
        ("Operator", "Operator"),
        ("Designer", "Designer"),
    ]

    role = forms.ChoiceField(
        choices=ROLE_CHOICES,
        required=True,
        label="Role",
        help_text="Determines what the user can access.",
    )
    password1 = forms.CharField(
        label="Password",
        widget=forms.PasswordInput,
        required=False,
        help_text="Leave empty to keep current password (edit only).",
    )
    password2 = forms.CharField(
        label="Confirm Password",
        widget=forms.PasswordInput,
        required=False,
    )

    class Meta:
        model = User
        fields = ["username", "email", "first_name", "last_name", "is_active"]

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        """Pre-fill role from existing group membership."""
        super().__init__(*args, **kwargs)
        if self.instance and self.instance.pk:
            groups = self.instance.groups.values_list("name", flat=True)
            for role_name in ("Admin", "Operator", "Designer"):
                if role_name in groups:
                    self.fields["role"].initial = role_name
                    break

    def clean(self) -> dict[str, Any]:
        """Validate password pair matches."""
        cleaned = super().clean()
        pw1 = cleaned.get("password1", "")
        pw2 = cleaned.get("password2", "")
        if pw1 or pw2:
            if pw1 != pw2:
                raise forms.ValidationError("Passwords do not match.")
        elif not self.instance.pk:
            raise forms.ValidationError("Password is required for new users.")
        return cleaned

    def save(self, commit: bool = True) -> User:
        """Save user and assign to the selected role group."""
        user = super().save(commit=False)
        pw = self.cleaned_data.get("password1")
        if pw:
            user.set_password(pw)
        role_name = self.cleaned_data["role"]
        if role_name == "Admin":
            user.is_staff = True
            user.is_superuser = True
        else:
            user.is_staff = False
            user.is_superuser = False
        if commit:
            user.save()
            # Clear existing role groups and assign selected
            role_groups = Group.objects.filter(name__in=["Admin", "Operator", "Designer"])
            user.groups.remove(*role_groups)
            group = Group.objects.get(name=role_name)
            user.groups.add(group)
        return user


class ProfileUpdateForm(forms.ModelForm):
    """Form for users to update their own profile."""

    class Meta:
        model = User
        fields = ["first_name", "last_name", "email"]
