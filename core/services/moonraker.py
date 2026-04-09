"""Klipper/Moonraker API client for printer control."""

import logging
from pathlib import Path
from typing import Any

import requests

from core.services.printer_backend import NormalizedJobStatus, PrinterError

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 15  # seconds


_KLIPPER_STATE_MAP = {
    "printing": NormalizedJobStatus.STATE_PRINTING,
    "complete": NormalizedJobStatus.STATE_COMPLETE,
    "error": NormalizedJobStatus.STATE_ERROR,
    "cancelled": NormalizedJobStatus.STATE_CANCELLED,
    "standby": NormalizedJobStatus.STATE_STANDBY,
    "paused": NormalizedJobStatus.STATE_PRINTING,
}


def map_klipper_state(raw_state: str) -> str:
    """Map a raw Klipper/Moonraker ``print_stats.state`` to a normalized state.

    Unknown states fall back to :attr:`NormalizedJobStatus.STATE_IDLE`.
    """
    return _KLIPPER_STATE_MAP.get(raw_state, NormalizedJobStatus.STATE_IDLE)


class MoonrakerError(PrinterError):
    """Raised when a Moonraker API operation fails."""


class MoonrakerClient:
    """Client for the Klipper/Moonraker printer API."""

    def __init__(self, base_url: str, api_key: str = ""):
        """Initialize the Moonraker client.

        Args:
            base_url: Base URL of the Moonraker instance (e.g. http://192.168.1.100:7125).
            api_key: Optional API key for authentication.
        """
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key

    def _get_headers(self) -> dict[str, str]:
        """Build request headers including API key if configured.

        Returns:
            Dictionary with HTTP headers.
        """
        headers = {}
        if self.api_key:
            headers["X-Api-Key"] = self.api_key
        return headers

    def _request(self, method: str, endpoint: str, **kwargs: Any) -> dict:
        """Make an HTTP request to the Moonraker API.

        Args:
            method: HTTP method.
            endpoint: API endpoint path.
            **kwargs: Additional arguments passed to requests.

        Returns:
            Parsed JSON response.

        Raises:
            MoonrakerError: If the request fails.
        """
        url = f"{self.base_url}/{endpoint.lstrip('/')}"
        kwargs.setdefault("timeout", DEFAULT_TIMEOUT)
        kwargs.setdefault("headers", {}).update(self._get_headers())

        try:
            response = requests.request(method, url, **kwargs)
            response.raise_for_status()
            return response.json()
        except requests.ConnectionError as exc:
            raise MoonrakerError(f"Cannot connect to Moonraker at {self.base_url}") from exc
        except requests.Timeout as exc:
            raise MoonrakerError("Moonraker request timed out.") from exc
        except requests.HTTPError as exc:
            raise MoonrakerError(f"Moonraker API error: {exc.response.status_code} {exc.response.text}") from exc
        except requests.RequestException as exc:
            raise MoonrakerError(f"Moonraker request failed: {exc}") from exc

    def get_printer_status(self) -> dict:
        """Get the current printer status.

        Returns:
            Dictionary with printer status information.
        """
        return self._request(
            "GET",
            "printer/objects/query",
            params={
                "print_stats": None,
                "heater_bed": None,
                "extruder": None,
            },
        )

    def upload_gcode(self, file_path: str | Path, filename: str | None = None) -> dict:
        """Upload a G-code file to the printer.

        Args:
            file_path: Local path to the G-code file.
            filename: Optional remote filename. Defaults to the local filename.

        Returns:
            Upload result from Moonraker.

        Raises:
            FileNotFoundError: If the G-code file doesn't exist.
            MoonrakerError: If the upload fails.
        """
        file_path = Path(file_path)
        if not file_path.exists():
            raise FileNotFoundError(f"G-code file not found: {file_path}")

        if filename is None:
            filename = file_path.name

        url = f"{self.base_url}/server/files/upload"
        headers = self._get_headers()

        try:
            with open(file_path, "rb") as f:
                response = requests.post(
                    url,
                    headers=headers,
                    files={"file": (filename, f, "application/octet-stream")},
                    timeout=120,
                )
            response.raise_for_status()
            return response.json()
        except requests.ConnectionError as exc:
            raise MoonrakerError(f"Cannot connect to Moonraker at {self.base_url}") from exc
        except requests.RequestException as exc:
            raise MoonrakerError(f"Upload failed: {exc}") from exc

    def start_print(self, filename: str) -> dict:
        """Start printing a G-code file already on the printer.

        Args:
            filename: Name of the G-code file on the printer.

        Returns:
            Moonraker response.
        """
        return self._request(
            "POST",
            "printer/print/start",
            json={"filename": filename},
        )

    def get_job_status(self) -> NormalizedJobStatus:
        """Get the current print job status with normalized fields.

        Queries both ``print_stats`` (state, filename, durations) and
        ``virtual_sdcard`` (progress 0.0–1.0) and returns a
        :class:`NormalizedJobStatus`.

        Returns:
            Normalized job status.
        """
        data = self._request(
            "GET",
            "printer/objects/query",
            params={"print_stats": None, "virtual_sdcard": None},
        )
        status_data = data.get("result", {}).get("status", {})
        print_stats = status_data.get("print_stats", {})
        virtual_sd = status_data.get("virtual_sdcard", {})

        return NormalizedJobStatus(
            state=map_klipper_state(print_stats.get("state", "unknown")),
            progress=virtual_sd.get("progress", 0.0),
            filename=print_stats.get("filename", ""),
        )

    def get_job_status_raw(self) -> dict:
        """Get the raw Moonraker job status response.

        Returns:
            Dictionary with the full Moonraker response including
            ``print_stats`` and ``virtual_sdcard`` data.
        """
        return self._request(
            "GET",
            "printer/objects/query",
            params={"print_stats": None, "virtual_sdcard": None},
        )

    def cancel_print(self) -> dict:
        """Cancel the current print job.

        Returns:
            Moonraker response.
        """
        return self._request("POST", "printer/print/cancel")
