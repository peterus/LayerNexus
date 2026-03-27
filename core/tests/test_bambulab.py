"""Tests for the Bambu Lab integration.

Covers:
- PrinterBackend protocol and factory function
- Token encryption/decryption helpers
- BambuLabClient with mocked bambulab library
- Auth wizard views (session-based 3-step flow)
- PrinterProfileForm conditional validation
- Queue views with PrinterError handling
"""

from unittest.mock import MagicMock, patch

from django.test import Client, TestCase, override_settings
from django.urls import reverse

from core.models import BambuCloudAccount, PrinterProfile
from core.services.bambulab import (
    BambuLabClient,
    BambuLabError,
    decrypt_token,
    encrypt_token,
)
from core.services.printer_backend import (
    NormalizedJobStatus,
    PrinterError,
    get_printer_backend,
)
from core.tests.mixins import TestDataMixin

# ── Token encryption tests ─────────────────────────────────────────────


class TokenEncryptionTests(TestCase):
    """Test Fernet-based token encryption and decryption."""

    def test_encrypt_decrypt_roundtrip(self):
        """Encrypted token decrypts to the original value."""
        token = "eyJhbGciOiJSUzI1NiJ9.test-token-value"
        encrypted = encrypt_token(token)
        self.assertNotEqual(encrypted, token)
        self.assertEqual(decrypt_token(encrypted), token)

    def test_empty_string_passthrough(self):
        """Empty strings pass through without encryption."""
        self.assertEqual(encrypt_token(""), "")
        self.assertEqual(decrypt_token(""), "")

    def test_encrypted_is_different_each_time(self):
        """Fernet produces different ciphertext for same plaintext."""
        token = "test-token"
        enc1 = encrypt_token(token)
        enc2 = encrypt_token(token)
        # Fernet uses a timestamp nonce so results differ
        self.assertNotEqual(enc1, enc2)
        # But both decrypt to the same value
        self.assertEqual(decrypt_token(enc1), token)
        self.assertEqual(decrypt_token(enc2), token)

    def test_decrypt_invalid_ciphertext_raises(self):
        """Decrypting garbage raises BambuLabError."""
        with self.assertRaises(BambuLabError):
            decrypt_token("not-a-valid-fernet-token")


# ── Factory function tests ─────────────────────────────────────────────


class PrinterBackendFactoryTests(TestDataMixin, TestCase):
    """Test get_printer_backend() factory function."""

    def test_klipper_returns_moonraker_client(self):
        """Factory returns MoonrakerClient for Klipper printers."""
        from core.services.moonraker import MoonrakerClient

        self.printer.printer_type = PrinterProfile.TYPE_KLIPPER
        self.printer.moonraker_url = "http://192.168.1.100:7125"
        self.printer.save()

        backend = get_printer_backend(self.printer)
        self.assertIsInstance(backend, MoonrakerClient)

    def test_klipper_without_url_raises(self):
        """Factory raises PrinterError for Klipper without URL."""
        self.printer.printer_type = PrinterProfile.TYPE_KLIPPER
        self.printer.moonraker_url = ""
        self.printer.save()

        with self.assertRaises(PrinterError) as ctx:
            get_printer_backend(self.printer)
        self.assertIn("Moonraker URL", str(ctx.exception))

    def test_bambulab_without_account_raises(self):
        """Factory raises PrinterError for Bambu Lab without account."""
        self.printer.printer_type = PrinterProfile.TYPE_BAMBULAB
        self.printer.bambu_account = None
        self.printer.save()

        with self.assertRaises(PrinterError) as ctx:
            get_printer_backend(self.printer)
        self.assertIn("account", str(ctx.exception).lower())

    def test_unknown_type_raises(self):
        """Factory raises PrinterError for unknown printer type."""
        self.printer.printer_type = "unknown_type"
        self.printer.save()

        with self.assertRaises(PrinterError) as ctx:
            get_printer_backend(self.printer)
        self.assertIn("Unknown printer type", str(ctx.exception))


# ── NormalizedJobStatus tests ──────────────────────────────────────────


class NormalizedJobStatusTests(TestCase):
    """Test NormalizedJobStatus data class."""

    def test_terminal_states(self):
        """Terminal states are correctly identified."""
        for state in ("complete", "error", "cancelled", "standby"):
            status = NormalizedJobStatus(state=state)
            self.assertTrue(status.is_terminal, f"{state} should be terminal")

    def test_non_terminal_states(self):
        """Non-terminal states are correctly identified."""
        for state in ("printing", "idle"):
            status = NormalizedJobStatus(state=state)
            self.assertFalse(status.is_terminal, f"{state} should not be terminal")

    def test_repr(self):
        """String representation includes key fields."""
        status = NormalizedJobStatus(state="printing", progress=0.5, filename="test.gcode")
        repr_str = repr(status)
        self.assertIn("printing", repr_str)
        self.assertIn("50.0%", repr_str)
        self.assertIn("test.gcode", repr_str)

    def test_defaults(self):
        """Default values are sensible."""
        status = NormalizedJobStatus(state="idle")
        self.assertEqual(status.progress, 0.0)
        self.assertEqual(status.filename, "")
        self.assertEqual(status.temperatures, {})


# ── BambuLabClient tests ──────────────────────────────────────────────


class BambuLabClientInitTests(TestDataMixin, TestCase):
    """Test BambuLabClient initialization and validation."""

    def setUp(self):
        super().setUp()
        self.account = BambuCloudAccount.objects.create(
            user=self.user,
            email="test@bambu.com",
            region="global",
            token=encrypt_token("test-jwt-token"),
            bambu_uid="u_123456",
            is_active=True,
        )
        self.printer.printer_type = PrinterProfile.TYPE_BAMBULAB
        self.printer.bambu_account = self.account
        self.printer.bambu_device_id = "01234567890ABCD"
        self.printer.bambu_ip_address = "192.168.1.50"
        self.printer.save()

    def test_init_success(self):
        """Client initializes successfully with valid config."""
        client = BambuLabClient(self.printer)
        self.assertEqual(client._device_id, "01234567890ABCD")
        self.assertEqual(client._ip_address, "192.168.1.50")

    def test_init_no_account_raises(self):
        """Client raises BambuLabError without linked account."""
        self.printer.bambu_account = None
        self.printer.save()

        with self.assertRaises(BambuLabError):
            BambuLabClient(self.printer)

    def test_init_inactive_account_raises(self):
        """Client raises BambuLabError for inactive account."""
        self.account.is_active = False
        self.account.save()

        with self.assertRaises(BambuLabError) as ctx:
            BambuLabClient(self.printer)
        self.assertIn("inactive", str(ctx.exception).lower())

    def test_init_no_device_id_raises(self):
        """Client raises BambuLabError without device ID."""
        self.printer.bambu_device_id = ""
        self.printer.save()

        with self.assertRaises(BambuLabError) as ctx:
            BambuLabClient(self.printer)
        self.assertIn("device ID", str(ctx.exception))

    @patch("core.services.bambulab.BambuLabClient._get_cloud_client")
    def test_get_printer_status_success(self, mock_cloud):
        """get_printer_status returns device info from Cloud API."""
        mock_client = MagicMock()
        mock_cloud.return_value = mock_client
        mock_client.get_devices.return_value = {
            "devices": [
                {
                    "dev_id": "01234567890ABCD",
                    "online": True,
                    "print_status": "IDLE",
                    "dev_product_name": "P1S",
                    "name": "My P1S",
                },
            ],
        }

        client = BambuLabClient(self.printer)
        status = client.get_printer_status()

        self.assertTrue(status["online"])
        self.assertEqual(status["model"], "P1S")

    @patch("core.services.bambulab.BambuLabClient._get_cloud_client")
    def test_get_printer_status_device_not_found(self, mock_cloud):
        """get_printer_status raises if device not in account."""
        mock_client = MagicMock()
        mock_cloud.return_value = mock_client
        mock_client.get_devices.return_value = {"devices": []}

        client = BambuLabClient(self.printer)
        with self.assertRaises(BambuLabError):
            client.get_printer_status()

    def test_upload_gcode_file_not_found(self):
        """upload_gcode raises FileNotFoundError for missing file."""
        client = BambuLabClient(self.printer)
        with self.assertRaises(FileNotFoundError):
            client.upload_gcode("/nonexistent/file.gcode")

    @patch("core.services.bambulab.BambuLabClient._mqtt_request_status")
    def test_get_job_status_normalizes(self, mock_mqtt):
        """get_job_status normalizes MQTT data to NormalizedJobStatus."""
        mock_mqtt.return_value = {
            "print": {
                "gcode_state": "RUNNING",
                "mc_percent": 45,
                "subtask_name": "model.3mf",
                "bed_temper": 60.0,
                "nozzle_temper": 220.5,
            },
        }

        client = BambuLabClient(self.printer)
        status = client.get_job_status()

        self.assertIsInstance(status, NormalizedJobStatus)
        self.assertEqual(status.state, NormalizedJobStatus.STATE_PRINTING)
        self.assertAlmostEqual(status.progress, 0.45)
        self.assertEqual(status.filename, "model.3mf")
        self.assertEqual(status.temperatures["nozzle"], 220.5)

    @patch("core.services.bambulab.BambuLabClient._mqtt_request_status")
    def test_get_job_status_finish_state(self, mock_mqtt):
        """FINISH maps to STATE_COMPLETE."""
        mock_mqtt.return_value = {
            "print": {
                "gcode_state": "FINISH",
                "mc_percent": 100,
                "subtask_name": "done.gcode",
            },
        }

        client = BambuLabClient(self.printer)
        status = client.get_job_status()

        self.assertEqual(status.state, NormalizedJobStatus.STATE_COMPLETE)
        self.assertTrue(status.is_terminal)

    @patch("core.services.bambulab.BambuLabClient._mqtt_request_status")
    @patch("core.services.bambulab.BambuLabClient._get_job_status_from_cloud")
    def test_get_job_status_fallback_to_cloud(self, mock_cloud_status, mock_mqtt):
        """Falls back to Cloud API when MQTT fails."""
        mock_mqtt.side_effect = BambuLabError("MQTT timeout")
        mock_cloud_status.return_value = NormalizedJobStatus(
            state=NormalizedJobStatus.STATE_IDLE,
        )

        client = BambuLabClient(self.printer)
        status = client.get_job_status()

        self.assertEqual(status.state, NormalizedJobStatus.STATE_IDLE)
        mock_cloud_status.assert_called_once()


# ── PrinterProfileForm tests ──────────────────────────────────────────


class PrinterProfileFormTests(TestDataMixin, TestCase):
    """Test conditional validation in PrinterProfileForm."""

    def test_klipper_valid(self):
        """Klipper printer with URL is valid."""
        from core.forms import PrinterProfileForm

        form = PrinterProfileForm(data={
            "name": "My Klipper",
            "printer_type": PrinterProfile.TYPE_KLIPPER,
            "moonraker_url": "http://192.168.1.100:7125",
        })
        self.assertTrue(form.is_valid(), form.errors)

    def test_klipper_without_url_invalid(self):
        """Klipper printer without URL is invalid."""
        from core.forms import PrinterProfileForm

        form = PrinterProfileForm(data={
            "name": "My Klipper",
            "printer_type": PrinterProfile.TYPE_KLIPPER,
            "moonraker_url": "",
        })
        self.assertFalse(form.is_valid())
        self.assertIn("moonraker_url", form.errors)

    def test_bambulab_valid(self):
        """Bambu Lab printer with account and device ID is valid."""
        from core.forms import PrinterProfileForm

        account = BambuCloudAccount.objects.create(
            user=self.user,
            email="test@bambu.com",
            region="global",
            token="encrypted-token",
            is_active=True,
        )
        form = PrinterProfileForm(data={
            "name": "My Bambu",
            "printer_type": PrinterProfile.TYPE_BAMBULAB,
            "bambu_account": account.pk,
            "bambu_device_id": "ABC123",
        })
        self.assertTrue(form.is_valid(), form.errors)

    def test_bambulab_without_account_invalid(self):
        """Bambu Lab printer without account is invalid."""
        from core.forms import PrinterProfileForm

        form = PrinterProfileForm(data={
            "name": "My Bambu",
            "printer_type": PrinterProfile.TYPE_BAMBULAB,
            "bambu_device_id": "ABC123",
        })
        self.assertFalse(form.is_valid())
        self.assertIn("bambu_account", form.errors)

    def test_bambulab_without_device_id_invalid(self):
        """Bambu Lab printer without device ID is invalid."""
        from core.forms import PrinterProfileForm

        account = BambuCloudAccount.objects.create(
            user=self.user,
            email="test@bambu.com",
            region="global",
            is_active=True,
        )
        form = PrinterProfileForm(data={
            "name": "My Bambu",
            "printer_type": PrinterProfile.TYPE_BAMBULAB,
            "bambu_account": account.pk,
            "bambu_device_id": "",
        })
        self.assertFalse(form.is_valid())
        self.assertIn("bambu_device_id", form.errors)


# ── Auth wizard view tests ─────────────────────────────────────────────


@override_settings(ALLOWED_HOSTS=["testserver"])
class BambuAuthWizardViewTests(TestDataMixin, TestCase):
    """Test the 3-step Bambu Lab authentication wizard."""

    def setUp(self):
        super().setUp()
        self.client = Client()
        self.client.login(username="testuser", password="testpass123")

    def test_step1_get(self):
        """Step 1 form renders correctly."""
        resp = self.client.get(reverse("core:bambuaccount_step1"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Bambu Lab")

    def test_step2_requires_step1(self):
        """Step 2 redirects if step 1 not completed."""
        resp = self.client.get(reverse("core:bambuaccount_step2"))
        self.assertEqual(resp.status_code, 302)
        self.assertIn("connect", resp.url)

    def test_step3_requires_token(self):
        """Step 3 redirects if no token in session."""
        resp = self.client.get(reverse("core:bambuaccount_step3"))
        self.assertEqual(resp.status_code, 302)
        self.assertIn("connect", resp.url)

    @patch("bambulab.BambuAuthenticator")
    def test_step1_post_stores_session(self, mock_auth_cls):
        """Step 1 POST stores email/region in session and redirects."""
        mock_auth = MagicMock()
        mock_auth_cls.return_value = mock_auth
        mock_auth._login_request = MagicMock()

        resp = self.client.post(reverse("core:bambuaccount_step1"), {
            "email": "user@bambu.com",
            "password": "secret123",
            "region": "global",
        })

        self.assertEqual(resp.status_code, 302)
        session = self.client.session
        self.assertEqual(session.get("bambu_auth_email"), "user@bambu.com")
        self.assertEqual(session.get("bambu_auth_region"), "global")

    def test_step2_get_with_session(self):
        """Step 2 renders when session has email."""
        session = self.client.session
        session["bambu_auth_email"] = "user@bambu.com"
        session["bambu_auth_password"] = "secret123"
        session["bambu_auth_region"] = "global"
        session.save()

        resp = self.client.get(reverse("core:bambuaccount_step2"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "user@bambu.com")

    @patch("bambulab.BambuClient")
    @patch("bambulab.BambuAuthenticator")
    def test_step2_post_success(self, mock_auth_cls, mock_client_cls):
        """Step 2 POST with valid code stores token and redirects."""
        # Setup session
        session = self.client.session
        session["bambu_auth_email"] = "user@bambu.com"
        session["bambu_auth_password"] = "secret123"
        session["bambu_auth_region"] = "global"
        session.save()

        # Mock auth
        mock_auth = MagicMock()
        mock_auth_cls.return_value = mock_auth
        mock_auth.login.return_value = "jwt-token-abc123"

        # Mock user info
        mock_client = MagicMock()
        mock_client_cls.return_value = mock_client
        mock_client.get_user_info.return_value = {"uid": "u_99999"}

        resp = self.client.post(reverse("core:bambuaccount_step2"), {
            "code": "123456",
        })

        self.assertEqual(resp.status_code, 302)
        session = self.client.session
        self.assertEqual(session.get("bambu_auth_token"), "jwt-token-abc123")
        self.assertEqual(session.get("bambu_auth_uid"), "u_99999")
        # Password should be cleared from session
        self.assertIsNone(session.get("bambu_auth_password"))

    def test_step2_post_invalid_code(self):
        """Step 2 rejects non-numeric code."""
        session = self.client.session
        session["bambu_auth_email"] = "user@bambu.com"
        session["bambu_auth_password"] = "secret123"
        session["bambu_auth_region"] = "global"
        session.save()

        resp = self.client.post(reverse("core:bambuaccount_step2"), {
            "code": "abcdef",
        })
        self.assertEqual(resp.status_code, 200)
        self.assertFormError(resp.context["form"], "code", "The code must contain only digits.")

    @patch("bambulab.BambuClient")
    def test_step3_creates_account_and_printer(self, mock_client_cls):
        """Step 3 POST creates BambuCloudAccount and PrinterProfile."""
        # Setup session
        session = self.client.session
        session["bambu_auth_email"] = "user@bambu.com"
        session["bambu_auth_region"] = "global"
        session["bambu_auth_token"] = "jwt-token-abc123"
        session["bambu_auth_uid"] = "u_99999"
        session.save()

        # Mock device list
        mock_client = MagicMock()
        mock_client_cls.return_value = mock_client
        mock_client.get_devices.return_value = {
            "devices": [
                {
                    "dev_id": "DEVICE001",
                    "name": "My P1S",
                    "dev_product_name": "P1S",
                    "online": True,
                },
            ],
        }

        resp = self.client.post(reverse("core:bambuaccount_step3"), {
            "device_id": "DEVICE001",
            "lan_ip": "192.168.1.50",
        })

        self.assertEqual(resp.status_code, 302)

        # Verify account created
        account = BambuCloudAccount.objects.get(email="user@bambu.com")
        self.assertEqual(account.user, self.user)
        self.assertEqual(account.region, "global")
        self.assertEqual(account.bambu_uid, "u_99999")
        self.assertTrue(account.is_active)

        # Verify printer created
        printer = PrinterProfile.objects.get(bambu_device_id="DEVICE001")
        self.assertEqual(printer.name, "My P1S")
        self.assertEqual(printer.printer_type, PrinterProfile.TYPE_BAMBULAB)
        self.assertEqual(printer.bambu_account, account)
        self.assertEqual(str(printer.bambu_ip_address), "192.168.1.50")

        # Verify session cleaned up
        session = self.client.session
        self.assertIsNone(session.get("bambu_auth_token"))
        self.assertIsNone(session.get("bambu_auth_email"))


# ── Account management view tests ─────────────────────────────────────


@override_settings(ALLOWED_HOSTS=["testserver"])
class BambuAccountManagementTests(TestDataMixin, TestCase):
    """Test Bambu Lab account list and delete views."""

    def setUp(self):
        super().setUp()
        self.client = Client()
        self.client.login(username="testuser", password="testpass123")
        self.account = BambuCloudAccount.objects.create(
            user=self.user,
            email="test@bambu.com",
            region="global",
            token=encrypt_token("jwt-token"),
            bambu_uid="u_12345",
            is_active=True,
        )

    def test_list_shows_own_accounts(self):
        """Account list only shows current user's accounts."""
        # Create account for other user
        BambuCloudAccount.objects.create(
            user=self.other_user,
            email="other@bambu.com",
            region="global",
            is_active=True,
        )

        resp = self.client.get(reverse("core:bambuaccount_list"))
        self.assertEqual(resp.status_code, 200)
        accounts = resp.context["accounts"]
        self.assertEqual(len(accounts), 1)
        self.assertEqual(accounts[0].email, "test@bambu.com")

    def test_delete_confirmation_page(self):
        """Delete confirmation shows account details."""
        resp = self.client.get(
            reverse("core:bambuaccount_delete", kwargs={"pk": self.account.pk})
        )
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "test@bambu.com")

    def test_delete_removes_account(self):
        """POST to delete removes the account."""
        resp = self.client.post(
            reverse("core:bambuaccount_delete", kwargs={"pk": self.account.pk})
        )
        self.assertEqual(resp.status_code, 302)
        self.assertFalse(
            BambuCloudAccount.objects.filter(pk=self.account.pk).exists()
        )

    def test_cannot_delete_other_users_account(self):
        """Cannot delete an account belonging to another user."""
        other_account = BambuCloudAccount.objects.create(
            user=self.other_user,
            email="other@bambu.com",
            region="global",
        )
        resp = self.client.post(
            reverse("core:bambuaccount_delete", kwargs={"pk": other_account.pk})
        )
        self.assertEqual(resp.status_code, 404)
        self.assertTrue(
            BambuCloudAccount.objects.filter(pk=other_account.pk).exists()
        )

    def test_refresh_redirects_to_step1(self):
        """Refresh action pre-fills session and redirects."""
        resp = self.client.post(
            reverse("core:bambuaccount_refresh", kwargs={"pk": self.account.pk})
        )
        self.assertEqual(resp.status_code, 302)
        self.assertIn("connect", resp.url)
        session = self.client.session
        self.assertEqual(session.get("bambu_auth_email"), "test@bambu.com")
        self.assertEqual(session.get("bambu_auth_region"), "global")

    def test_unauthenticated_redirect(self):
        """Unauthenticated users are redirected to login."""
        self.client.logout()
        resp = self.client.get(reverse("core:bambuaccount_list"))
        self.assertEqual(resp.status_code, 302)
        self.assertIn("login", resp.url)

    def test_designer_cannot_access_wizard(self):
        """Designer role (no can_manage_printers) gets 403 on wizard."""
        from django.contrib.auth.models import Group, User

        designer = User.objects.create_user(username="designer_test", password="testpass123")
        designer_group = Group.objects.get(name="Designer")
        designer.groups.add(designer_group)

        self.client.login(username="designer_test", password="testpass123")
        resp = self.client.get(reverse("core:bambuaccount_step1"))
        self.assertEqual(resp.status_code, 403)

    def test_designer_cannot_delete_account(self):
        """Designer role gets 403 on account delete."""
        from django.contrib.auth.models import Group, User

        designer = User.objects.create_user(username="designer_del", password="testpass123")
        designer_group = Group.objects.get(name="Designer")
        designer.groups.add(designer_group)

        self.client.login(username="designer_del", password="testpass123")
        resp = self.client.post(
            reverse("core:bambuaccount_delete", kwargs={"pk": self.account.pk})
        )
        self.assertEqual(resp.status_code, 403)
