"""Spoolman API client for filament tracking.

When a Spoolman instance is configured, LayerNexus uses it as the single
source of truth for filament/spool data — no need to duplicate materials
locally.
"""

import logging
from typing import Any

import requests

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 10  # seconds


class SpoolmanError(Exception):
    """Raised when a Spoolman API operation fails."""


class SpoolmanClient:
    """Client for the Spoolman filament management API."""

    def __init__(self, base_url: str):
        """Initialize the Spoolman client.

        Args:
            base_url: Base URL of the Spoolman instance (e.g. http://192.168.1.100:7912).
        """
        self.base_url = base_url.rstrip("/")

    def _request(self, method: str, endpoint: str, **kwargs: Any) -> dict | list:
        """Make an HTTP request to the Spoolman API.

        Args:
            method: HTTP method (GET, POST, PUT, DELETE).
            endpoint: API endpoint path.
            **kwargs: Additional arguments passed to requests.

        Returns:
            Parsed JSON response.

        Raises:
            SpoolmanError: If the request fails.
        """
        url = f"{self.base_url}/api/v1/{endpoint.lstrip('/')}"
        kwargs.setdefault("timeout", DEFAULT_TIMEOUT)

        try:
            response = requests.request(method, url, **kwargs)
            response.raise_for_status()
            return response.json()
        except requests.ConnectionError as exc:
            raise SpoolmanError(f"Cannot connect to Spoolman at {self.base_url}") from exc
        except requests.Timeout as exc:
            raise SpoolmanError("Spoolman request timed out.") from exc
        except requests.HTTPError as exc:
            raise SpoolmanError(f"Spoolman API error: {exc.response.status_code} {exc.response.text}") from exc
        except requests.RequestException as exc:
            raise SpoolmanError(f"Spoolman request failed: {exc}") from exc

    # ------------------------------------------------------------------
    # Spools
    # ------------------------------------------------------------------

    def get_spools(self) -> list:
        """List all available spools.

        Returns:
            List of spool dictionaries.
        """
        return self._request("GET", "spool")

    def get_spool(self, spool_id: int) -> dict:
        """Get details for a specific spool.

        Args:
            spool_id: ID of the spool.

        Returns:
            Spool details dictionary.
        """
        return self._request("GET", f"spool/{spool_id}")

    def use_filament(self, spool_id: int, grams: float) -> dict:
        """Track filament usage by consuming grams from a spool.

        Args:
            spool_id: ID of the spool to consume from.
            grams: Amount of filament used in grams.

        Returns:
            Updated spool details.
        """
        return self._request(
            "PUT",
            f"spool/{spool_id}/use",
            json={"use_weight": grams},
        )

    # ------------------------------------------------------------------
    # Filaments
    # ------------------------------------------------------------------

    def get_filaments(self) -> list:
        """List all available filaments.

        Returns:
            List of filament dictionaries.
        """
        return self._request("GET", "filament")

    def get_filament(self, filament_id: int) -> dict:
        """Get details for a specific filament.

        Args:
            filament_id: ID of the filament.

        Returns:
            Filament details dictionary.
        """
        return self._request("GET", f"filament/{filament_id}")

    # ------------------------------------------------------------------
    # Vendors
    # ------------------------------------------------------------------

    def get_vendors(self) -> list:
        """List all vendors.

        Returns:
            List of vendor dictionaries.
        """
        return self._request("GET", "vendor")
