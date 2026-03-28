"""Bambu Lab printer backend using the bambu-lab-cloud-api library.

Provides :class:`BambuLabClient` implementing the :class:`PrinterBackend`
protocol for Bambu Lab printers (P1P, P1S, X1, A1 series).

Communication paths:
- **Cloud API** — device listing, status polling, cloud print start.
- **MQTT** — real-time status, print start/pause/stop commands.
- **Local FTP** — G-code upload over LAN (preferred over Cloud upload).
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import logging
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings

from core.services.printer_backend import NormalizedJobStatus, PrinterError

if TYPE_CHECKING:
    from core.models import PrinterProfile

logger = logging.getLogger(__name__)

# Timeout for waiting on MQTT status responses
MQTT_STATUS_TIMEOUT = 15  # seconds
FTP_UPLOAD_TIMEOUT = 300  # seconds (large files)


class BambuLabError(PrinterError):
    """Raised when a Bambu Lab API/MQTT operation fails."""


# ── Token encryption helpers ───────────────────────────────────────────


def _derive_fernet_key() -> bytes:
    """Derive a Fernet-compatible key from Django's SECRET_KEY.

    Returns:
        A 32-byte URL-safe base64-encoded key suitable for Fernet.
    """
    digest = hashlib.sha256(settings.SECRET_KEY.encode()).digest()
    return base64.urlsafe_b64encode(digest)


def encrypt_token(plaintext: str) -> str:
    """Encrypt a plaintext token for database storage.

    Args:
        plaintext: The JWT token to encrypt.

    Returns:
        Base64-encoded ciphertext string.
    """
    if not plaintext:
        return ""
    fernet = Fernet(_derive_fernet_key())
    return fernet.encrypt(plaintext.encode()).decode()


def decrypt_token(ciphertext: str) -> str:
    """Decrypt an encrypted token from the database.

    Args:
        ciphertext: The encrypted token string.

    Returns:
        The decrypted JWT token.

    Raises:
        BambuLabError: If decryption fails (corrupted or wrong key).
    """
    if not ciphertext:
        return ""
    fernet = Fernet(_derive_fernet_key())
    try:
        return fernet.decrypt(ciphertext.encode()).decode()
    except InvalidToken as exc:
        raise BambuLabError(
            "Cannot decrypt Bambu Lab token. This may happen if DJANGO_SECRET_KEY changed since the token was stored."
        ) from exc


# ── State mapping ──────────────────────────────────────────────────────

_BAMBU_STATE_MAP: dict[str, str] = {
    "RUNNING": NormalizedJobStatus.STATE_PRINTING,
    "PAUSE": NormalizedJobStatus.STATE_PRINTING,  # paused is still "active"
    "FINISH": NormalizedJobStatus.STATE_COMPLETE,
    "FAILED": NormalizedJobStatus.STATE_ERROR,
    "IDLE": NormalizedJobStatus.STATE_IDLE,
}


def _normalize_bambu_state(raw_state: str) -> str:
    """Map Bambu Lab gcode_state to a NormalizedJobStatus state string."""
    return _BAMBU_STATE_MAP.get(raw_state.upper(), NormalizedJobStatus.STATE_IDLE)


# ── BambuLabClient ─────────────────────────────────────────────────────


class BambuLabClient:
    """Printer backend for Bambu Lab printers.

    Implements the :class:`PrinterBackend` protocol.  Prefers LAN
    communication (FTP upload, MQTT commands) with Cloud API fallback.

    Args:
        printer: A :class:`PrinterProfile` instance with ``printer_type``
            set to ``"bambulab"`` and a linked :class:`BambuCloudAccount`.
    """

    def __init__(self, printer: PrinterProfile) -> None:
        self.printer = printer
        self.account = printer.bambu_account

        if not self.account:
            raise BambuLabError(f"Printer '{printer.name}' has no Bambu Lab account linked.")
        if not self.account.is_active:
            raise BambuLabError(f"Bambu Lab account '{self.account.email}' is inactive. Please re-authenticate.")

        self._token = decrypt_token(self.account.token)
        self._device_id = printer.bambu_device_id
        self._ip_address = printer.bambu_ip_address

        if not self._device_id:
            raise BambuLabError(f"Printer '{printer.name}' has no Bambu Lab device ID configured.")

    # ── Cloud API helpers ──────────────────────────────────────────────

    def _get_cloud_client(self) -> Any:
        """Create a BambuClient instance for Cloud API calls.

        Returns:
            A ``bambulab.BambuClient`` instance.

        Raises:
            BambuLabError: If the library is not installed or token is invalid.
        """
        try:
            from bambulab import BambuClient
        except ImportError as exc:
            raise BambuLabError("bambu-lab-cloud-api is not installed. Run: pip install bambu-lab-cloud-api") from exc

        return BambuClient(token=self._token, region=self.account.region)

    def _get_mqtt_client(self) -> Any:
        """Create an MQTTClient instance for real-time communication.

        Returns:
            A ``bambulab.MQTTClient`` instance.

        Raises:
            BambuLabError: If the library is not installed or credentials
                are missing.
        """
        try:
            from bambulab import MQTTClient
        except ImportError as exc:
            raise BambuLabError("bambu-lab-cloud-api is not installed. Run: pip install bambu-lab-cloud-api") from exc

        if not self.account.bambu_uid:
            raise BambuLabError(f"Bambu Lab account '{self.account.email}' has no user ID. Please re-authenticate.")

        return MQTTClient(
            username=self.account.bambu_uid,
            access_token=self._token,
            device_id=self._device_id,
        )

    def _get_ftp_client(self) -> Any:
        """Create a LocalFTPClient for LAN file uploads.

        Returns:
            A ``bambulab.LocalFTPClient`` instance.

        Raises:
            BambuLabError: If no LAN IP is configured or library missing.
        """
        if not self._ip_address:
            raise BambuLabError(
                f"Printer '{self.printer.name}' has no LAN IP address configured. LAN upload requires a local IP."
            )

        try:
            from bambulab import LocalFTPClient
        except ImportError as exc:
            raise BambuLabError("bambu-lab-cloud-api is not installed. Run: pip install bambu-lab-cloud-api") from exc

        # Get access code from device info
        cloud = self._get_cloud_client()
        try:
            devices = cloud.get_devices()
            device_list = devices if isinstance(devices, list) else devices.get("devices", [])
            access_code = ""
            for dev in device_list:
                dev_id = dev.get("dev_id", "")
                if dev_id == self._device_id:
                    access_code = dev.get("dev_access_code", "")
                    break

            if not access_code:
                raise BambuLabError(
                    f"Cannot find access code for device {self._device_id}. Verify the device is bound to your account."
                )

            return LocalFTPClient(self._ip_address, access_code)
        except BambuLabError:
            raise
        except Exception as exc:
            raise BambuLabError(f"Failed to initialize FTP client: {exc}") from exc

    # ── MQTT helper for synchronous command + wait ─────────────────────

    def _mqtt_request_status(self) -> dict[str, Any]:
        """Connect to MQTT, request full status, and return the data.

        This is a synchronous helper that connects, sends ``pushall``,
        waits for a response, and disconnects.

        Returns:
            The latest status data dict from the printer.

        Raises:
            BambuLabError: On connection or timeout errors.
        """
        mqtt = self._get_mqtt_client()
        received_data: dict[str, Any] = {}
        event = threading.Event()

        def on_message(device_id: str, data: dict) -> None:
            received_data.update(data)
            event.set()

        mqtt.on_message = on_message

        try:
            mqtt.connect(blocking=False)
            mqtt.request_full_status()

            if not event.wait(timeout=MQTT_STATUS_TIMEOUT):
                raise BambuLabError(
                    f"Timeout waiting for MQTT status from '{self.printer.name}' (waited {MQTT_STATUS_TIMEOUT}s)."
                )

            return received_data
        except BambuLabError:
            raise
        except Exception as exc:
            raise BambuLabError(f"MQTT communication failed with '{self.printer.name}': {exc}") from exc
        finally:
            with contextlib.suppress(Exception):
                mqtt.disconnect()

    # ── PrinterBackend protocol implementation ─────────────────────────

    def get_printer_status(self) -> dict[str, Any]:
        """Get current printer status via Cloud API.

        Returns:
            Dictionary with device info including online status,
            print status, model name, and temperatures.

        Raises:
            BambuLabError: If the Cloud API call fails.
        """
        cloud = self._get_cloud_client()
        try:
            devices = cloud.get_devices()
            device_list = devices if isinstance(devices, list) else devices.get("devices", [])

            for dev in device_list:
                if dev.get("dev_id") == self._device_id:
                    return {
                        "online": dev.get("online", False),
                        "print_status": dev.get("print_status", "unknown"),
                        "model": dev.get("dev_product_name", "unknown"),
                        "name": dev.get("name", self.printer.name),
                        "nozzle_diameter": dev.get("nozzle_diameter"),
                    }

            raise BambuLabError(f"Device {self._device_id} not found in account '{self.account.email}'.")
        except BambuLabError:
            raise
        except Exception as exc:
            raise BambuLabError(f"Failed to get printer status: {exc}") from exc

    def upload_gcode(self, file_path: str | Path, filename: str | None = None) -> dict[str, Any]:
        """Upload G-code to the printer, preferring LAN FTP.

        Tries LocalFTPClient (LAN) first.  Falls back to Cloud upload
        if no LAN IP is configured.

        Args:
            file_path: Local path to the G-code file.
            filename: Optional remote filename.

        Returns:
            Dictionary with upload result details.

        Raises:
            FileNotFoundError: If the file does not exist.
            BambuLabError: If upload fails via both LAN and Cloud.
        """
        file_path = Path(file_path)
        if not file_path.exists():
            raise FileNotFoundError(f"G-code file not found: {file_path}")

        if filename is None:
            filename = file_path.name

        # Try LAN FTP upload first
        if self._ip_address:
            try:
                return self._upload_via_ftp(file_path, filename)
            except BambuLabError as ftp_exc:
                logger.warning(
                    "LAN FTP upload failed for '%s', falling back to Cloud: %s",
                    self.printer.name,
                    ftp_exc,
                )

        # Fallback: Cloud upload
        return self._upload_via_cloud(file_path, filename)

    def _upload_via_ftp(self, file_path: Path, filename: str) -> dict[str, Any]:
        """Upload a file via LocalFTPClient (LAN).

        Args:
            file_path: Local path to the file.
            filename: Remote filename.

        Returns:
            Upload result dictionary.
        """
        ftp = self._get_ftp_client()
        try:
            ftp.connect()
            result = ftp.upload_file(str(file_path), target_dir="/", remote_filename=filename)
            logger.info(
                "LAN FTP upload successful for '%s': %s",
                self.printer.name,
                filename,
            )
            return {
                "method": "lan_ftp",
                "filename": filename,
                "remote_path": result.get("remote_path", f"/{filename}"),
            }
        except Exception as exc:
            raise BambuLabError(f"FTP upload failed: {exc}") from exc
        finally:
            with contextlib.suppress(Exception):
                ftp.disconnect()

    def _upload_via_cloud(self, file_path: Path, filename: str) -> dict[str, Any]:
        """Upload a file via Cloud API.

        Args:
            file_path: Local path to the file.
            filename: Remote filename.

        Returns:
            Upload result dictionary.
        """
        cloud = self._get_cloud_client()
        try:
            # Cloud API doesn't support custom filenames; the file is
            # identified by its original name on the server side.
            result = cloud.upload_file(str(file_path))
            logger.info(
                "Cloud upload successful for '%s': %s (original name used by Cloud API)",
                self.printer.name,
                filename,
            )
            return {
                "method": "cloud",
                "filename": filename,
                "result": result,
            }
        except Exception as exc:
            raise BambuLabError(f"Cloud upload failed: {exc}") from exc

    def start_print(self, filename: str) -> dict[str, Any]:
        """Start printing a previously uploaded file via MQTT.

        Connects to the printer's MQTT broker, sends a print start
        command, and disconnects.

        Args:
            filename: Name/path of the file on the printer.

        Returns:
            Dictionary with command result.

        Raises:
            BambuLabError: If the MQTT command fails.
        """
        mqtt = self._get_mqtt_client()
        try:
            mqtt.connect(blocking=False)
            # Allow connection to establish
            time.sleep(1)

            # Construct print start command
            print_command = {
                "print": {
                    "command": "project_file",
                    "sequence_id": str(int(time.time())),
                    "param": "Metadata/plate_1.gcode",
                    "subtask_name": filename,
                    "url": f"ftp://{filename}",
                    "file": filename,
                    "bed_type": "auto",
                    "timelapse": False,
                    "use_ams": False,
                }
            }
            mqtt.publish(print_command)
            logger.info(
                "Print start command sent for '%s' on '%s'",
                filename,
                self.printer.name,
            )

            return {"status": "command_sent", "filename": filename}
        except Exception as exc:
            raise BambuLabError(f"Failed to start print on '{self.printer.name}': {exc}") from exc
        finally:
            with contextlib.suppress(Exception):
                mqtt.disconnect()

    def get_job_status(self) -> NormalizedJobStatus:
        """Get current print job status via MQTT.

        Connects to MQTT, requests full status, normalizes the
        response, and returns a :class:`NormalizedJobStatus`.

        Returns:
            Normalized job status.

        Raises:
            BambuLabError: On MQTT communication failure.
        """
        try:
            data = self._mqtt_request_status()
        except BambuLabError:
            # Fallback to Cloud API for basic status
            logger.warning(
                "MQTT status failed for '%s', trying Cloud API fallback.",
                self.printer.name,
            )
            return self._get_job_status_from_cloud()

        print_data = data.get("print", {})
        raw_state = print_data.get("gcode_state", "IDLE")
        progress_pct = print_data.get("mc_percent", 0)

        return NormalizedJobStatus(
            state=_normalize_bambu_state(raw_state),
            progress=progress_pct / 100.0,
            filename=print_data.get("subtask_name", ""),
            temperatures={
                "bed": print_data.get("bed_temper", 0.0),
                "nozzle": print_data.get("nozzle_temper", 0.0),
            },
        )

    def _get_job_status_from_cloud(self) -> NormalizedJobStatus:
        """Fallback: get basic job status from Cloud API.

        Returns:
            Normalized job status with limited detail.
        """
        cloud = self._get_cloud_client()
        try:
            devices = cloud.get_devices()
            device_list = devices if isinstance(devices, list) else devices.get("devices", [])

            for dev in device_list:
                if dev.get("dev_id") == self._device_id:
                    cloud_status = dev.get("print_status", "IDLE").upper()
                    state_map = {
                        "ACTIVE": NormalizedJobStatus.STATE_PRINTING,
                        "IDLE": NormalizedJobStatus.STATE_IDLE,
                        "PAUSED": NormalizedJobStatus.STATE_PRINTING,
                        "FAILED": NormalizedJobStatus.STATE_ERROR,
                    }
                    state = state_map.get(cloud_status, NormalizedJobStatus.STATE_IDLE)
                    return NormalizedJobStatus(state=state)

            raise BambuLabError(f"Device {self._device_id} not found.")
        except BambuLabError:
            raise
        except Exception as exc:
            raise BambuLabError(f"Cloud status query failed: {exc}") from exc

    def cancel_print(self) -> dict[str, Any]:
        """Cancel the current print via MQTT stop command.

        Returns:
            Dictionary with command result.

        Raises:
            BambuLabError: If the MQTT command fails.
        """
        mqtt = self._get_mqtt_client()
        try:
            mqtt.connect(blocking=False)
            time.sleep(1)

            mqtt.stop_print()
            logger.info("Cancel command sent to '%s'", self.printer.name)

            return {"status": "cancel_sent"}
        except Exception as exc:
            raise BambuLabError(f"Failed to cancel print on '{self.printer.name}': {exc}") from exc
        finally:
            with contextlib.suppress(Exception):
                mqtt.disconnect()
