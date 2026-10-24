"""Tests for the slicing helper functions in core.services.slicing."""

from unittest.mock import patch

from django.test import SimpleTestCase, TestCase

from core.models import OrcaMachineProfile
from core.services.slicing import _build_slicer_kwargs, _resolve_bed_type


class ResolveBedTypeTests(SimpleTestCase):
    """_resolve_bed_type mirrors OrcaSlicer's Preset::get_default_bed_type."""

    def test_named_bed_type_is_kept(self) -> None:
        """A valid bed type name is used as-is."""
        self.assertEqual(_resolve_bed_type("Textured PEI Plate"), "Textured PEI Plate")

    def test_legacy_integer_bed_type_is_mapped(self) -> None:
        """A legacy numeric value maps to the enum name."""
        self.assertEqual(_resolve_bed_type("1"), "Cool Plate")
        self.assertEqual(_resolve_bed_type("4"), "Textured PEI Plate")

    def test_empty_falls_back_to_high_temp_plate(self) -> None:
        """Empty or missing values fall back to the GUI default."""
        self.assertEqual(_resolve_bed_type(""), "High Temp Plate")
        self.assertEqual(_resolve_bed_type(None), "High Temp Plate")

    def test_invalid_falls_back_to_high_temp_plate(self) -> None:
        """Unknown values fall back to the GUI default."""
        self.assertEqual(_resolve_bed_type("Glass"), "High Temp Plate")
        self.assertEqual(_resolve_bed_type("0"), "High Temp Plate")
        self.assertEqual(_resolve_bed_type("7"), "High Temp Plate")


class BuildSlicerKwargsBedTypeTests(TestCase):
    """The machine's bed type is always forwarded as ``bed_type``."""

    def setUp(self) -> None:
        """Create a resolved machine profile."""
        self.machine = OrcaMachineProfile.objects.create(
            name="Test Machine",
            orca_name="test_machine",
            state=OrcaMachineProfile.STATE_RESOLVED,
            instantiation=True,
        )

    def _build(self, orca_json: dict) -> dict:
        """Run _build_slicer_kwargs with a mocked machine JSON."""
        with patch("core.services.profile_import.to_orca_json", return_value=orca_json):
            return _build_slicer_kwargs(self.machine, None, None)

    def test_default_bed_type_is_forwarded(self) -> None:
        """A set default_bed_type selects the plate type for slicing."""
        kwargs = self._build({"default_bed_type": "Textured PEI Plate"})
        self.assertEqual(kwargs["bed_type"], "Textured PEI Plate")

    def test_empty_default_bed_type_uses_high_temp_plate(self) -> None:
        """Single-bed printers (empty default_bed_type) slice with High Temp Plate, not Cool Plate."""
        kwargs = self._build({"default_bed_type": ""})
        self.assertEqual(kwargs["bed_type"], "High Temp Plate")

    def test_missing_default_bed_type_uses_high_temp_plate(self) -> None:
        """A machine without default_bed_type slices with High Temp Plate."""
        kwargs = self._build({})
        self.assertEqual(kwargs["bed_type"], "High Temp Plate")

    def test_no_machine_profile_has_no_bed_type(self) -> None:
        """Without a machine profile no bed_type is set."""
        kwargs = _build_slicer_kwargs(None, None, None)
        self.assertNotIn("bed_type", kwargs)
