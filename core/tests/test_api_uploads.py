"""STL + document multipart upload tests for the build API."""

from __future__ import annotations

from unittest import mock

from django.contrib.auth.models import Permission, User
from django.core.files.uploadedfile import SimpleUploadedFile
from rest_framework.authtoken.models import Token
from rest_framework.test import APITestCase

from core.models import Part, Project, ProjectDocument


class ApiUploadTests(APITestCase):
    """Multipart STL upload (with estimation trigger) and document upload."""

    def setUp(self) -> None:
        """Authenticate a manage-capable user and create a project + part."""
        self.user = User.objects.create_user("designer", password="x")
        self.user.user_permissions.add(Permission.objects.get(codename="can_manage_projects"))
        token = Token.objects.create(user=self.user)
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {token.key}")
        self.project = Project.objects.create(name="Module")
        self.part = Part.objects.create(project=self.project, name="Gear", quantity=1)

    def test_stl_upload_sets_file_and_triggers_estimation(self) -> None:
        """Uploading an STL saves the file and calls the shared estimation entrypoint."""
        upload = SimpleUploadedFile("gear.stl", b"solid gear\nendsolid", content_type="model/stl")
        with mock.patch("core.api.views._trigger_part_estimation") as triggered:
            resp = self.client.post(f"/api/v1/parts/{self.part.pk}/stl/", {"stl_file": upload}, format="multipart")
        self.assertEqual(resp.status_code, 200, resp.data)
        self.part.refresh_from_db()
        self.assertTrue(self.part.stl_file)
        self.assertIn("gear", self.part.stl_file.name)
        triggered.assert_called_once_with(self.part)

    def test_stl_upload_rejects_non_stl(self) -> None:
        """A non-STL upload is rejected with 400."""
        upload = SimpleUploadedFile("gear.txt", b"not stl", content_type="text/plain")
        with mock.patch("core.api.views._trigger_part_estimation"):
            resp = self.client.post(f"/api/v1/parts/{self.part.pk}/stl/", {"stl_file": upload}, format="multipart")
        self.assertEqual(resp.status_code, 400)

    def test_stl_upload_missing_file(self) -> None:
        """A request with no file is rejected with 400."""
        with mock.patch("core.api.views._trigger_part_estimation"):
            resp = self.client.post(f"/api/v1/parts/{self.part.pk}/stl/", {}, format="multipart")
        self.assertEqual(resp.status_code, 400)

    def test_document_upload(self) -> None:
        """Uploading an allowed document creates a ProjectDocument attached to the project."""
        upload = SimpleUploadedFile("manual.pdf", b"%PDF-1.4 fake", content_type="application/pdf")
        resp = self.client.post(
            f"/api/v1/projects/{self.project.pk}/documents/",
            {"file": upload},
            format="multipart",
        )
        self.assertEqual(resp.status_code, 201, resp.data)
        doc = ProjectDocument.objects.get(project=self.project)
        self.assertEqual(doc.name, "manual")  # auto-filled from filename
        self.assertEqual(doc.uploaded_by, self.user)

    def test_document_upload_rejects_bad_extension(self) -> None:
        """An unsupported document extension is rejected with 400."""
        upload = SimpleUploadedFile("evil.exe", b"MZ", content_type="application/octet-stream")
        resp = self.client.post(
            f"/api/v1/projects/{self.project.pk}/documents/",
            {"file": upload},
            format="multipart",
        )
        self.assertEqual(resp.status_code, 400)

    def test_document_delete(self) -> None:
        """A document can be listed and deleted."""
        upload = SimpleUploadedFile("notes.txt", b"hello", content_type="text/plain")
        created = self.client.post(
            f"/api/v1/projects/{self.project.pk}/documents/",
            {"file": upload},
            format="multipart",
        )
        doc_id = created.data["id"]
        resp = self.client.delete(f"/api/v1/projects/{self.project.pk}/documents/{doc_id}/")
        self.assertEqual(resp.status_code, 204)
        self.assertFalse(ProjectDocument.objects.filter(pk=doc_id).exists())
