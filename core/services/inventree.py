"""InvenTree API client for inventory management integration."""

import logging
from typing import Any

import requests

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 15  # seconds


class InvenTreeError(Exception):
    """Raised when an InvenTree API operation fails."""


class InvenTreeClient:
    """Client for the InvenTree inventory management API."""

    def __init__(self, base_url: str, api_token: str = ""):
        """Initialize the InvenTree client.

        Args:
            base_url: Base URL of the InvenTree instance (e.g. http://inventree.local:8000).
            api_token: API token for authentication.
        """
        self.base_url = base_url.rstrip("/")
        self.api_token = api_token

    def _get_headers(self) -> dict[str, str]:
        """Build request headers including API token if configured.

        Returns:
            Dictionary with HTTP headers.
        """
        headers = {"Accept": "application/json"}
        if self.api_token:
            headers["Authorization"] = f"Token {self.api_token}"
        return headers

    def _request(self, method: str, endpoint: str, **kwargs: Any) -> dict | list | None:
        """Make an HTTP request to the InvenTree API.

        Args:
            method: HTTP method (GET, POST, PUT, DELETE).
            endpoint: API endpoint path.
            **kwargs: Additional arguments passed to requests.

        Returns:
            Parsed JSON response or None for empty responses.

        Raises:
            InvenTreeError: If the request fails.
        """
        url = f"{self.base_url}/api/{endpoint.lstrip('/')}"
        kwargs.setdefault("timeout", DEFAULT_TIMEOUT)
        kwargs["headers"] = {**kwargs.get("headers", {}), **self._get_headers()}

        try:
            response = requests.request(method, url, **kwargs)
            response.raise_for_status()
            if response.status_code == 204 or not response.content:
                return None
            return response.json()
        except requests.ConnectionError as exc:
            raise InvenTreeError(f"Cannot connect to InvenTree at {self.base_url}") from exc
        except requests.Timeout as exc:
            raise InvenTreeError("InvenTree request timed out.") from exc
        except requests.HTTPError as exc:
            raise InvenTreeError(f"InvenTree API error: {exc.response.status_code} {exc.response.text}") from exc
        except requests.RequestException as exc:
            raise InvenTreeError(f"InvenTree request failed: {exc}") from exc

    # -- Parts / Stock --

    def get_parts(self, **params) -> list:
        """List parts from InvenTree.

        Args:
            **params: Optional query parameters (category, search, etc.).

        Returns:
            List of part dictionaries.
        """
        return self._request("GET", "part/", params=params)

    def get_part(self, part_id: int) -> dict:
        """Get details for a single part.

        Args:
            part_id: InvenTree part ID.

        Returns:
            Part dictionary.
        """
        return self._request("GET", f"part/{part_id}/")

    def get_stock_items(self, part_id: int | None = None) -> list:
        """List stock items, optionally filtered by part.

        Args:
            part_id: Optional InvenTree part ID filter.

        Returns:
            List of stock item dictionaries.
        """
        params = {}
        if part_id is not None:
            params["part"] = part_id
        return self._request("GET", "stock/", params=params)

    def adjust_stock(self, stock_item_id: int, quantity: float, notes: str = "") -> dict:
        """Adjust the quantity of a stock item (e.g. consume filament).

        Args:
            stock_item_id: ID of the stock item.
            quantity: Amount to adjust (negative to subtract).
            notes: Optional notes.

        Returns:
            Updated stock item.
        """
        return self._request(
            "POST",
            "stock/adjust/",
            json={
                "items": [{"pk": stock_item_id, "quantity": quantity}],
                "notes": notes,
            },
        )

    # -- Categories --

    def get_categories(self) -> list:
        """List all part categories."""
        return self._request("GET", "part/category/")

    # -- BOM (Bill of Materials) --

    def get_bom(self, part_id: int) -> list:
        """Get the Bill of Materials for a part.

        Args:
            part_id: InvenTree part ID.

        Returns:
            List of BOM item dictionaries.
        """
        return self._request("GET", "bom/", params={"part": part_id})

    def create_bom_item(self, part_id: int, sub_part_id: int, quantity: float) -> dict:
        """Add an item to a part's Bill of Materials.

        Args:
            part_id: Parent part ID.
            sub_part_id: Sub-part ID to add.
            quantity: Required quantity.

        Returns:
            Created BOM item dictionary.
        """
        return self._request(
            "POST",
            "bom/",
            json={"part": part_id, "sub_part": sub_part_id, "quantity": quantity},
        )
