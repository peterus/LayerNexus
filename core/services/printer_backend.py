"""Printer backend abstraction layer.

Defines the ``PrinterBackend`` protocol that all printer backends
(Moonraker, Bambu Lab, …) must implement, plus a factory function
to create the correct backend for a given ``PrinterProfile``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from core.models import PrinterProfile

logger = logging.getLogger(__name__)


class PrinterError(Exception):
    """Base exception for all printer backend errors."""


@dataclass
class NormalizedJobStatus:
    """Normalized print job status returned by all backends.

    Attributes:
        state: One of ``"printing"``, ``"complete"``, ``"error"``,
            ``"cancelled"``, ``"standby"``, ``"idle"``.
        progress: Print progress as a float from 0.0 to 1.0.
        filename: Name of the file currently being printed.
        temperatures: Optional dict with ``"bed"`` and ``"nozzle"`` keys
            mapping to float temperatures in °C.
    """

    STATE_PRINTING = "printing"
    STATE_COMPLETE = "complete"
    STATE_ERROR = "error"
    STATE_CANCELLED = "cancelled"
    STATE_STANDBY = "standby"
    STATE_IDLE = "idle"
    TERMINAL_STATES = {STATE_COMPLETE, STATE_ERROR, STATE_CANCELLED, STATE_STANDBY}

    state: str
    progress: float = 0.0
    filename: str = ""
    temperatures: dict[str, float] = field(default_factory=dict)

    def __repr__(self) -> str:
        return (
            f"NormalizedJobStatus(state={self.state!r}, progress={self.progress:.1%}, "
            f"filename={self.filename!r})"
        )

    @property
    def is_terminal(self) -> bool:
        """Return ``True`` if the print is no longer actively running."""
        return self.state in self.TERMINAL_STATES


@runtime_checkable
class PrinterBackend(Protocol):
    """Protocol that all printer backends must implement.

    Each method may raise :class:`PrinterError` (or a subclass) on
    failure.  Views should catch ``PrinterError`` for uniform error
    handling regardless of the backend type.
    """

    def get_printer_status(self) -> dict[str, Any]:
        """Get current printer status (online check, temperatures, …).

        Returns:
            Backend-specific status dictionary.  The only guarantee is
            that calling this method without an exception means the
            printer is reachable.
        """
        ...

    def upload_gcode(self, file_path: str | Path, filename: str | None = None) -> dict[str, Any]:
        """Upload a G-code file to the printer.

        Args:
            file_path: Local path to the G-code file.
            filename: Optional remote filename.  Defaults to the local
                filename if not provided.

        Returns:
            Backend-specific upload result dictionary.

        Raises:
            FileNotFoundError: If the G-code file does not exist.
            PrinterError: If the upload fails.
        """
        ...

    def start_print(self, filename: str) -> dict[str, Any]:
        """Start printing a previously uploaded file.

        Args:
            filename: Name / ID of the file on the printer.

        Returns:
            Backend-specific response.
        """
        ...

    def get_job_status(self) -> NormalizedJobStatus:
        """Get the current print job status with normalized fields.

        Returns:
            A :class:`NormalizedJobStatus` instance.
        """
        ...

    def cancel_print(self) -> dict[str, Any]:
        """Cancel the current print job.

        Returns:
            Backend-specific response.
        """
        ...


def get_printer_backend(printer: PrinterProfile) -> PrinterBackend:
    """Create the appropriate backend client for a printer profile.

    Args:
        printer: The printer profile to create a backend for.

    Returns:
        A backend instance implementing :class:`PrinterBackend`.

    Raises:
        PrinterError: If the printer type is unknown or not configured.
    """
    from core.models import PrinterProfile as PP

    if printer.printer_type == PP.TYPE_KLIPPER:
        from core.services.moonraker import MoonrakerClient

        if not printer.moonraker_url:
            raise PrinterError(f"Printer '{printer.name}' has no Moonraker URL configured.")
        return MoonrakerClient(printer.moonraker_url, printer.moonraker_api_key)

    if printer.printer_type == PP.TYPE_BAMBULAB:
        from core.services.bambulab import BambuLabClient

        return BambuLabClient(printer)

    raise PrinterError(f"Unknown printer type: {printer.printer_type!r}")
