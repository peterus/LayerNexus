"""Tests for dashboard, statistics, and admin dashboard views."""

from django.contrib.auth.models import Group
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from core.models import Part
from core.tests.mixins import TestDataMixin


@override_settings(ALLOWED_HOSTS=["testserver"])
class DashboardViewTests(TestDataMixin, TestCase):
    """Tests for the dashboard view."""

    def setUp(self):
        super().setUp()
        self.client.login(username="testuser", password="testpass123")

    def test_dashboard_status(self):
        resp = self.client.get(reverse("core:dashboard"))
        self.assertEqual(resp.status_code, 200)

    def test_dashboard_context(self):
        resp = self.client.get(reverse("core:dashboard"))
        self.assertIn("projects", resp.context)
        self.assertIn("recent_jobs", resp.context)


@override_settings(ALLOWED_HOSTS=["testserver"])
class MaterialsListViewTests(TestDataMixin, TestCase):
    """Tests for the Spoolman-backed materials list view."""

    def setUp(self):
        super().setUp()
        self.client = Client()
        self.client.login(username="testuser", password="testpass123")

    def test_list_200(self):
        r = self.client.get(reverse("core:materialprofile_list"))
        self.assertEqual(r.status_code, 200)


@override_settings(ALLOWED_HOSTS=["testserver"])
class StatisticsViewTests(TestDataMixin, TestCase):
    """Tests for the statistics view."""

    def setUp(self):
        super().setUp()
        self.client = Client()
        self.client.login(username="testuser", password="testpass123")

    def test_statistics_200(self):
        r = self.client.get(reverse("core:statistics"))
        self.assertEqual(r.status_code, 200)

    def test_statistics_has_context(self):
        r = self.client.get(reverse("core:statistics"))
        self.assertIn("total_projects", r.context)
        self.assertIn("total_parts", r.context)
        self.assertIn("total_jobs", r.context)


@override_settings(ALLOWED_HOSTS=["testserver"])
class DashboardEnhancedTests(TestDataMixin, TestCase):
    """Tests for the enhanced dashboard."""

    def setUp(self):
        super().setUp()
        self.client = Client()
        self.client.login(username="testuser", password="testpass123")

    def test_dashboard_has_statistics(self):
        r = self.client.get(reverse("core:dashboard"))
        self.assertEqual(r.status_code, 200)
        self.assertIn("total_projects", r.context)
        self.assertIn("total_parts", r.context)
        self.assertIn("active_jobs", r.context)
        self.assertIn("completed_jobs", r.context)


class AdminDashboardViewTests(TestDataMixin, TestCase):
    """Tests for the AdminDashboardView."""

    def setUp(self):
        super().setUp()
        self.client.login(username="testuser", password="testpass123")
        self.url = reverse("core:admin_dashboard")

    def test_admin_can_access(self):
        """Admin users can access the admin dashboard."""
        resp = self.client.get(self.url)
        self.assertEqual(resp.status_code, 200)

    def test_non_admin_denied(self):
        """Non-admin users receive a 403."""
        # other_user has no Admin group
        designer_group = Group.objects.get(name="Designer")
        self.other_user.groups.add(designer_group)
        self.client.login(username="otheruser", password="otherpass123")
        resp = self.client.get(self.url)
        self.assertEqual(resp.status_code, 403)

    def test_operator_denied(self):
        """Operator users also receive a 403."""
        operator_group = Group.objects.get(name="Operator")
        self.other_user.groups.add(operator_group)
        self.client.login(username="otheruser", password="otherpass123")
        resp = self.client.get(self.url)
        self.assertEqual(resp.status_code, 403)

    def test_nav_visible_for_admin(self):
        """Admin Dashboard link is visible in navigation for admins."""
        resp = self.client.get(reverse("core:dashboard"))
        self.assertContains(resp, "admin-dashboard/")

    def test_nav_hidden_for_non_admin(self):
        """Admin Dashboard link is hidden for non-admin users."""
        designer_group = Group.objects.get(name="Designer")
        self.other_user.groups.add(designer_group)
        self.client.login(username="otheruser", password="otherpass123")
        resp = self.client.get(reverse("core:dashboard"))
        self.assertNotContains(resp, "admin-dashboard/")

    def test_context_contains_system_stats(self):
        """Context includes expected system statistic keys."""
        resp = self.client.get(self.url)
        for key in [
            "total_projects",
            "total_parts",
            "total_users",
            "total_printers",
            "total_jobs",
            "total_storage_mb",
        ]:
            self.assertIn(key, resp.context, f"Missing context key: {key}")

    def test_context_contains_estimation_data(self):
        """Context includes unified queue and estimation breakdown data."""
        resp = self.client.get(self.url)
        for key in [
            "queue_active",
            "queue_waiting",
            "queue_errors",
            "queue_active_count",
            "queue_waiting_count",
            "queue_error_count",
            "parts_estimated",
            "parts_estimating_count",
            "parts_pending_count",
            "parts_error_count",
        ]:
            self.assertIn(key, resp.context, f"Missing context key: {key}")

    def test_context_contains_recent_activity(self):
        """Context includes recent projects, parts, and jobs."""
        resp = self.client.get(self.url)
        self.assertIn("recent_projects", resp.context)
        self.assertIn("recent_parts", resp.context)
        self.assertIn("recent_jobs", resp.context)

    def test_error_parts_displayed(self):
        """Parts with estimation errors appear in queue_errors."""
        self.part.estimation_status = Part.ESTIMATION_ERROR
        self.part.estimation_error = "Test error"
        self.part.save()
        resp = self.client.get(self.url)
        self.assertEqual(resp.context["queue_error_count"], 1)
        self.assertEqual(resp.context["parts_error_count"], 1)
        self.assertEqual(resp.context["queue_errors"][0]["type"], "estimation")

    def test_estimating_parts_displayed(self):
        """Parts actively estimating appear in queue_active."""
        self.part.estimation_status = Part.ESTIMATION_ESTIMATING
        self.part.save()
        resp = self.client.get(self.url)
        self.assertEqual(resp.context["queue_active_count"], 1)
        self.assertEqual(resp.context["parts_estimating_count"], 1)
        self.assertEqual(resp.context["queue_active"][0]["type"], "estimation")

    def test_pending_parts_displayed(self):
        """Parts waiting for estimation appear in queue_waiting."""
        self.part.estimation_status = Part.ESTIMATION_PENDING
        self.part.save()
        resp = self.client.get(self.url)
        self.assertEqual(resp.context["queue_waiting_count"], 1)
        self.assertEqual(resp.context["parts_pending_count"], 1)
        self.assertEqual(resp.context["queue_waiting"][0]["type"], "estimation")

    def test_unauthenticated_denied(self):
        """Unauthenticated users get a 403."""
        self.client.logout()
        resp = self.client.get(self.url)
        self.assertEqual(resp.status_code, 403)
