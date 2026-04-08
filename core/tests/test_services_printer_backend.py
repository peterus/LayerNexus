"""Tests for the printer backend abstraction and factory."""

from django.test import TestCase

from core.services.moonraker import MoonrakerClient, MoonrakerError
from core.services.printer_backend import (
    NormalizedJobStatus,
    PrinterBackend,
    PrinterError,
    PrinterNotConfiguredError,
    get_printer_backend,
)
from core.tests.mixins import TestDataMixin


class GetPrinterBackendTests(TestDataMixin, TestCase):
    """Tests for the ``get_printer_backend()`` factory."""

    def test_returns_moonraker_client_when_configured(self):
        self.printer.moonraker_url = "http://printer:7125"
        self.printer.moonraker_api_key = "secret"
        self.printer.save()

        backend = get_printer_backend(self.printer)

        self.assertIsInstance(backend, MoonrakerClient)
        self.assertEqual(backend.base_url, "http://printer:7125")
        self.assertEqual(backend.api_key, "secret")

    def test_raises_not_configured_when_url_missing(self):
        # TestDataMixin creates a printer with no moonraker_url
        with self.assertRaises(PrinterNotConfiguredError) as ctx:
            get_printer_backend(self.printer)

        self.assertIn(self.printer.name, str(ctx.exception))

    def test_not_configured_is_printer_error(self):
        """Views that only catch PrinterError should still handle missing config."""
        self.assertTrue(issubclass(PrinterNotConfiguredError, PrinterError))


class PrinterBackendProtocolTests(TestCase):
    """MoonrakerClient must satisfy the PrinterBackend protocol."""

    def test_moonraker_client_is_printer_backend(self):
        client = MoonrakerClient("http://printer:7125")
        self.assertIsInstance(client, PrinterBackend)


class NormalizedJobStatusTests(TestCase):
    """Tests for the NormalizedJobStatus dataclass."""

    def test_is_terminal_for_complete(self):
        status = NormalizedJobStatus(state=NormalizedJobStatus.STATE_COMPLETE, progress=1.0)
        self.assertTrue(status.is_terminal)

    def test_is_terminal_for_error_cancelled_standby(self):
        for state in (
            NormalizedJobStatus.STATE_ERROR,
            NormalizedJobStatus.STATE_CANCELLED,
            NormalizedJobStatus.STATE_STANDBY,
        ):
            self.assertTrue(NormalizedJobStatus(state=state).is_terminal, state)

    def test_not_terminal_for_printing_or_idle(self):
        for state in (NormalizedJobStatus.STATE_PRINTING, NormalizedJobStatus.STATE_IDLE):
            self.assertFalse(NormalizedJobStatus(state=state).is_terminal, state)


class ExceptionHierarchyTests(TestCase):
    """MoonrakerError and PrinterNotConfiguredError must extend PrinterError."""

    def test_moonraker_error_is_printer_error(self):
        self.assertTrue(issubclass(MoonrakerError, PrinterError))
