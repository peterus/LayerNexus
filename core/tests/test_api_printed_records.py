"""Build API: manual printed-part records under ``/api/v1/parts/<id>/printed-records/``."""

from __future__ import annotations

from django.contrib.auth.models import Permission, User
from rest_framework.authtoken.models import Token
from rest_framework.test import APITestCase

from core.models import Part, PrintedPartRecord, Project


class ApiPrintedRecordTests(APITestCase):
    """List / create / delete bookings and the delete guards they add."""

    def setUp(self) -> None:
        """Authenticate a manage-capable user and create a part."""
        self.user = User.objects.create_user("designer", password="x")
        self.user.user_permissions.add(Permission.objects.get(codename="can_manage_projects"))
        self._auth(self.user)
        self.project = Project.objects.create(name="Module")
        self.part = Part.objects.create(name="R02", project=self.project, quantity=4)
        self.url = f"/api/v1/parts/{self.part.pk}/printed-records/"

    def _auth(self, user: User) -> None:
        token, _created = Token.objects.get_or_create(user=user)
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {token.key}")

    def test_api_create_and_list_records(self) -> None:
        resp = self.client.post(self.url, {"quantity": 3, "printed_on": "2026-10-29", "note": "Job 33"}, format="json")
        self.assertEqual(resp.status_code, 201, resp.data)
        self.assertEqual(resp.data["part"], self.part.pk)
        self.assertEqual(resp.data["created_by"], "designer")
        record = PrintedPartRecord.objects.get(pk=resp.data["id"])
        self.assertEqual((record.part, record.quantity, record.created_by), (self.part, 3, self.user))

        resp = self.client.get(self.url)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual([row["id"] for row in resp.data], [record.pk])
        self.assertEqual(Part.objects.get(pk=self.part.pk).printed_quantity, 3)

    def test_create_defaults_date_and_ignores_part_in_body(self) -> None:
        other = Part.objects.create(name="Other", project=self.project)
        resp = self.client.post(self.url, {"quantity": 1, "part": other.pk}, format="json")
        self.assertEqual(resp.status_code, 201, resp.data)
        record = PrintedPartRecord.objects.get(pk=resp.data["id"])
        self.assertEqual(record.part, self.part)
        self.assertIsNotNone(record.printed_on)

    def test_quantity_zero_rejected(self) -> None:
        resp = self.client.post(self.url, {"quantity": 0}, format="json")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("quantity", resp.data)
        self.assertFalse(PrintedPartRecord.objects.exists())

    def test_unknown_part_404_on_create(self) -> None:
        resp = self.client.post("/api/v1/parts/999999/printed-records/", {"quantity": 1}, format="json")
        self.assertEqual(resp.status_code, 404)

    def test_delete_record(self) -> None:
        record = PrintedPartRecord.objects.create(part=self.part, quantity=2)
        resp = self.client.delete(f"{self.url}{record.pk}/")
        self.assertEqual(resp.status_code, 204)
        self.assertFalse(PrintedPartRecord.objects.exists())

    def test_delete_record_of_other_part_404(self) -> None:
        other = Part.objects.create(name="Other", project=self.project)
        record = PrintedPartRecord.objects.create(part=other, quantity=2)
        resp = self.client.delete(f"{self.url}{record.pk}/")
        self.assertEqual(resp.status_code, 404)
        self.assertTrue(PrintedPartRecord.objects.filter(pk=record.pk).exists())

    def test_read_only_user_cannot_write(self) -> None:
        record = PrintedPartRecord.objects.create(part=self.part, quantity=2)
        self._auth(User.objects.create_user("reader", password="x"))
        self.assertEqual(self.client.get(self.url).status_code, 200)
        self.assertEqual(self.client.post(self.url, {"quantity": 1}, format="json").status_code, 403)
        self.assertEqual(self.client.delete(f"{self.url}{record.pk}/").status_code, 403)
        self.assertTrue(PrintedPartRecord.objects.filter(pk=record.pk).exists())

    def test_unauthenticated_denied(self) -> None:
        self.client.credentials()
        self.assertIn(self.client.get(self.url).status_code, (401, 403))

    def test_part_with_records_not_deletable(self) -> None:
        PrintedPartRecord.objects.create(part=self.part, quantity=1)
        resp = self.client.delete(f"/api/v1/parts/{self.part.pk}/")
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.data["reason"], "print_history")
        self.assertTrue(Part.objects.filter(pk=self.part.pk).exists())

    def test_project_with_booked_part_not_deletable(self) -> None:
        PrintedPartRecord.objects.create(part=self.part, quantity=1)
        resp = self.client.delete(f"/api/v1/projects/{self.project.pk}/")
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.data["reason"], "print_history")
        self.assertTrue(Project.objects.filter(pk=self.project.pk).exists())
