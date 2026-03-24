"""Tests for URL resolution."""

from django.test import TestCase
from django.urls import reverse


class URLResolutionTests(TestCase):
    """Verify that all named URLs resolve without errors."""

    def test_dashboard(self):
        self.assertEqual(reverse("core:dashboard"), "/")

    def test_farm_dashboard(self):
        self.assertEqual(reverse("core:farm_dashboard"), "/farm/")

    def test_statistics(self):
        self.assertEqual(reverse("core:statistics"), "/statistics/")

    def test_project_list(self):
        self.assertEqual(reverse("core:project_list"), "/projects/")

    def test_project_create(self):
        self.assertEqual(reverse("core:project_create"), "/projects/new/")

    def test_project_detail(self):
        url = reverse("core:project_detail", args=[1])
        self.assertEqual(url, "/projects/1/")

    def test_project_update(self):
        url = reverse("core:project_update", args=[1])
        self.assertEqual(url, "/projects/1/edit/")

    def test_project_delete(self):
        url = reverse("core:project_delete", args=[1])
        self.assertEqual(url, "/projects/1/delete/")

    def test_project_cost(self):
        url = reverse("core:project_cost", args=[1])
        self.assertEqual(url, "/projects/1/cost/")

    def test_part_create(self):
        url = reverse("core:part_create", args=[1])
        self.assertEqual(url, "/projects/1/parts/new/")

    def test_part_detail(self):
        url = reverse("core:part_detail", args=[1])
        self.assertEqual(url, "/parts/1/")

    def test_part_update(self):
        url = reverse("core:part_update", args=[1])
        self.assertEqual(url, "/parts/1/edit/")

    def test_part_delete(self):
        url = reverse("core:part_delete", args=[1])
        self.assertEqual(url, "/parts/1/delete/")

    def test_printerprofile_list(self):
        self.assertEqual(reverse("core:printerprofile_list"), "/printers/")

    def test_printerprofile_create(self):
        self.assertEqual(reverse("core:printerprofile_create"), "/printers/new/")

    def test_printerprofile_update(self):
        url = reverse("core:printerprofile_update", args=[1])
        self.assertEqual(url, "/printers/1/edit/")

    def test_printerprofile_delete(self):
        url = reverse("core:printerprofile_delete", args=[1])
        self.assertEqual(url, "/printers/1/delete/")

    def test_printjob_list(self):
        self.assertEqual(reverse("core:printjob_list"), "/jobs/")

    def test_printjob_create(self):
        self.assertEqual(reverse("core:printjob_create"), "/jobs/new/")

    def test_printjob_detail(self):
        url = reverse("core:printjob_detail", args=[1])
        self.assertEqual(url, "/jobs/1/")

    def test_printqueue_list(self):
        self.assertEqual(reverse("core:printqueue_list"), "/queue/")

    def test_printqueue_create(self):
        self.assertEqual(reverse("core:printqueue_create"), "/queue/add/")

    def test_orca_machine_list(self):
        self.assertEqual(reverse("core:orcamachineprofile_list"), "/orca-machine-profiles/")

    def test_orca_filament_list(self):
        self.assertEqual(reverse("core:orcafilamentprofile_list"), "/orca-filament-profiles/")

    def test_orca_print_preset_list(self):
        self.assertEqual(reverse("core:orcaprintpreset_list"), "/orca-print-presets/")

    def test_register(self):
        self.assertEqual(reverse("core:register"), "/register/")

    def test_profile(self):
        self.assertEqual(reverse("core:profile"), "/profile/")

    def test_user_list(self):
        self.assertEqual(reverse("core:user_list"), "/users/")

    def test_user_create(self):
        self.assertEqual(reverse("core:user_create"), "/users/create/")

    def test_materialprofile_list(self):
        self.assertEqual(reverse("core:materialprofile_list"), "/materials/")
