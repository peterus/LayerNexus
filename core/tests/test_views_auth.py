"""Tests for authentication, registration, and profile views."""

from django.contrib.auth.models import Group, User
from django.test import TestCase, override_settings
from django.urls import reverse

from core.tests.mixins import TestDataMixin


@override_settings(ALLOWED_HOSTS=["testserver"])
class AuthRedirectTests(TestDataMixin, TestCase):
    """Unauthenticated users should be redirected to login."""

    def test_dashboard_redirect(self):
        resp = self.client.get(reverse("core:dashboard"))
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/accounts/login/", resp.url)

    def test_project_list_redirect(self):
        resp = self.client.get(reverse("core:project_list"))
        self.assertEqual(resp.status_code, 302)

    def test_profile_redirect(self):
        resp = self.client.get(reverse("core:profile"))
        self.assertEqual(resp.status_code, 302)

    def test_printjob_list_redirect(self):
        resp = self.client.get(reverse("core:printjob_list"))
        self.assertEqual(resp.status_code, 302)

    def test_register_accessible_anon(self):
        resp = self.client.get(reverse("core:register"))
        self.assertEqual(resp.status_code, 200)


@override_settings(ALLOWED_HOSTS=["testserver"])
class RegistrationViewTests(TestCase):
    """Tests for user registration."""

    def test_register_page_get(self):
        resp = self.client.get(reverse("core:register"))
        self.assertEqual(resp.status_code, 200)

    def test_register_creates_user(self):
        resp = self.client.post(
            reverse("core:register"),
            {
                "username": "brand_new",
                "email": "brand@new.com",
                "password1": "Str0ngP@ss!",
                "password2": "Str0ngP@ss!",
            },
        )
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(User.objects.filter(username="brand_new").exists())

    def test_register_logs_user_in(self):
        self.client.post(
            reverse("core:register"),
            {
                "username": "brand_new",
                "email": "brand@new.com",
                "password1": "Str0ngP@ss!",
                "password2": "Str0ngP@ss!",
            },
        )
        resp = self.client.get(reverse("core:dashboard"))
        self.assertEqual(resp.status_code, 200)


@override_settings(ALLOWED_HOSTS=["testserver"])
class ProfileViewTests(TestDataMixin, TestCase):
    """Tests for the profile view."""

    def setUp(self):
        super().setUp()
        self.client.login(username="testuser", password="testpass123")

    def test_profile_200(self):
        resp = self.client.get(reverse("core:profile"))
        self.assertEqual(resp.status_code, 200)

    def test_profile_context(self):
        resp = self.client.get(reverse("core:profile"))
        self.assertIn("total_parts", resp.context)
        self.assertIn("total_jobs", resp.context)


@override_settings(ALLOWED_HOSTS=["testserver"])
class RegistrationRoleAssignmentTests(TestCase):
    """Verify that the first user gets Admin role and subsequent get Designer."""

    def test_first_user_gets_admin(self):
        """First registered user should be assigned to Admin group."""
        self.client.post(
            reverse("core:register"),
            {
                "username": "first_user",
                "email": "first@test.com",
                "password1": "Str0ngP@ss!",
                "password2": "Str0ngP@ss!",
            },
        )
        user = User.objects.get(username="first_user")
        self.assertTrue(user.groups.filter(name="Admin").exists())

    def test_subsequent_user_gets_designer(self):
        """Second and later users should be assigned to Designer group."""
        # Create first user (gets Admin)
        User.objects.create_user(username="existing", password="pass123")
        admin_group = Group.objects.get(name="Admin")
        User.objects.get(username="existing").groups.add(admin_group)

        self.client.post(
            reverse("core:register"),
            {
                "username": "second_user",
                "email": "second@test.com",
                "password1": "Str0ngP@ss!",
                "password2": "Str0ngP@ss!",
            },
        )
        user = User.objects.get(username="second_user")
        self.assertTrue(user.groups.filter(name="Designer").exists())
        self.assertFalse(user.groups.filter(name="Admin").exists())
