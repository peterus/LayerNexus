"""Tests for print queue views."""

from django.test import Client, TestCase, override_settings
from django.urls import reverse

from core.models import PrinterProfile
from core.tests.mixins import TestDataMixin


@override_settings(ALLOWED_HOSTS=["testserver"])
class PrintQueueViewTests(TestDataMixin, TestCase):
    """Tests for PrintQueue views."""

    def setUp(self):
        super().setUp()
        self.client = Client()
        self.client.login(username="testuser", password="testpass123")
        self.printer = PrinterProfile.objects.create(name="Test Printer", created_by=self.user)

    def test_queue_list_200(self):
        r = self.client.get(reverse("core:printqueue_list"))
        self.assertEqual(r.status_code, 200)

    def test_queue_create_get(self):
        r = self.client.get(reverse("core:printqueue_create"))
        self.assertEqual(r.status_code, 200)
