"""Tests for project document views."""

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from core.models import ProjectDocument
from core.tests.mixins import TestDataMixin


class ProjectDocumentViewTests(TestDataMixin, TestCase):
    """Tests for document CRUD views."""

    def setUp(self):
        super().setUp()
        self.client = Client()
        self.client.login(username="testuser", password="testpass123")

    def test_create_document(self):
        f = SimpleUploadedFile("test.pdf", b"content", content_type="application/pdf")
        url = reverse("core:document_create", args=[self.project.pk])
        resp = self.client.post(url, {"name": "Test Doc", "file": f})
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(self.project.documents.count(), 1)

    def test_delete_document(self):
        f = SimpleUploadedFile("test.pdf", b"content")
        doc = ProjectDocument.objects.create(project=self.project, name="Test", file=f)
        url = reverse("core:document_delete", args=[doc.pk])
        resp = self.client.post(url)
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(self.project.documents.count(), 0)

    def test_create_requires_permission(self):
        self.client.login(username="otheruser", password="otherpass123")
        url = reverse("core:document_create", args=[self.project.pk])
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 403)


@override_settings(MEDIA_ROOT="/tmp/layernexus_test_media/")  # noqa: S108
class ProjectDocumentDownloadViewTests(TestDataMixin, TestCase):
    """Tests for ProjectDocumentDownloadView."""

    def setUp(self):
        super().setUp()
        self.client = Client()
        f = SimpleUploadedFile("guide.pdf", b"pdf content", content_type="application/pdf")
        self.doc = ProjectDocument.objects.create(
            project=self.project,
            name="Guide",
            file=f,
        )
        self.url = reverse("core:document_download", args=[self.doc.pk])

    def test_download_redirects_anonymous(self):
        """Unauthenticated requests should be redirected to the login page."""
        self.client.logout()
        resp = self.client.get(self.url)
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/accounts/login/", resp["Location"])

    def test_download_returns_200_for_logged_in_user(self):
        """Authenticated users receive a 200 file response."""
        self.client.login(username="testuser", password="testpass123")
        resp = self.client.get(self.url)
        self.assertEqual(resp.status_code, 200)

    def test_download_content_disposition(self):
        """Response should include Content-Disposition attachment with a .pdf filename."""
        self.client.login(username="testuser", password="testpass123")
        resp = self.client.get(self.url)
        self.assertEqual(resp.status_code, 200)
        content_disposition = resp.get("Content-Disposition", "")
        self.assertIn("attachment", content_disposition)
        # Django may append a suffix to avoid name collisions, so check the extension only
        self.assertIn(".pdf", content_disposition)

    def test_download_404_for_missing_document(self):
        """Requesting a non-existent document pk returns 404."""
        self.client.login(username="testuser", password="testpass123")
        url = reverse("core:document_download", args=[99999])
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 404)
