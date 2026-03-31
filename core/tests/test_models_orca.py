"""Tests for OrcaSlicer profile models (base, filament, machine, preset)."""

from django.contrib.auth.models import Group, User
from django.test import TestCase

from core.models import OrcaFilamentProfile, OrcaMachineProfile, OrcaPrintPreset


class OrcaProfileBaseTests(TestCase):
    """Tests for OrcaProfileBase common functionality via OrcaMachineProfile."""

    def setUp(self):
        self.user = User.objects.create_user(username="testuser", password="testpass123")
        admin_group = Group.objects.get(name="Admin")
        self.user.groups.add(admin_group)

    def test_str_resolved(self):
        profile = OrcaMachineProfile.objects.create(
            name="Bambu X1C",
            orca_name="bambu_x1c",
            state=OrcaMachineProfile.STATE_RESOLVED,
            created_by=self.user,
        )
        self.assertEqual(str(profile), "Bambu X1C")

    def test_str_pending(self):
        profile = OrcaMachineProfile.objects.create(
            name="Custom Machine",
            orca_name="custom_machine",
            state=OrcaMachineProfile.STATE_PENDING,
            created_by=self.user,
        )
        self.assertEqual(str(profile), "Custom Machine (pending)")

    def test_is_resolved_true(self):
        profile = OrcaMachineProfile.objects.create(
            name="Test",
            orca_name="test",
            state=OrcaMachineProfile.STATE_RESOLVED,
        )
        self.assertTrue(profile.is_resolved)

    def test_is_resolved_false(self):
        profile = OrcaMachineProfile.objects.create(
            name="Test",
            orca_name="test",
            state=OrcaMachineProfile.STATE_PENDING,
        )
        self.assertFalse(profile.is_resolved)

    def test_get_pending_for_parent(self):
        # Create resolved parent
        OrcaMachineProfile.objects.create(
            name="Parent",
            orca_name="parent_machine",
            state=OrcaMachineProfile.STATE_RESOLVED,
        )
        # Create pending children
        child1 = OrcaMachineProfile.objects.create(
            name="Child1",
            orca_name="child1",
            state=OrcaMachineProfile.STATE_PENDING,
            inherits_name="parent_machine",
        )
        child2 = OrcaMachineProfile.objects.create(
            name="Child2",
            orca_name="child2",
            state=OrcaMachineProfile.STATE_PENDING,
            inherits_name="parent_machine",
        )
        # Create pending child for different parent
        OrcaMachineProfile.objects.create(
            name="OtherChild",
            orca_name="other_child",
            state=OrcaMachineProfile.STATE_PENDING,
            inherits_name="other_parent",
        )
        # Create resolved child (should not appear)
        OrcaMachineProfile.objects.create(
            name="ResolvedChild",
            orca_name="resolved_child",
            state=OrcaMachineProfile.STATE_RESOLVED,
            inherits_name="parent_machine",
        )

        pending = OrcaMachineProfile.get_pending_for_parent("parent_machine")

        self.assertEqual(pending.count(), 2)
        self.assertIn(child1, pending)
        self.assertIn(child2, pending)

    def test_get_pending_for_parent_no_results(self):
        pending = OrcaMachineProfile.get_pending_for_parent("nonexistent_parent")
        self.assertEqual(pending.count(), 0)

    def test_default_state_is_pending(self):
        profile = OrcaMachineProfile.objects.create(
            name="Defaults",
            orca_name="defaults",
        )
        self.assertEqual(profile.state, OrcaMachineProfile.STATE_PENDING)

    def test_default_settings_empty_dict(self):
        profile = OrcaMachineProfile.objects.create(
            name="Defaults",
            orca_name="defaults",
        )
        self.assertEqual(profile.settings, {})
        self.assertEqual(profile.uploaded_json, {})


class OrcaFilamentProfileTests(TestCase):
    """Tests for OrcaFilamentProfile-specific properties."""

    def test_first_filament_type(self):
        profile = OrcaFilamentProfile.objects.create(
            name="PLA",
            orca_name="pla",
            state=OrcaFilamentProfile.STATE_RESOLVED,
            settings={"filament_type": ["PLA"]},
        )
        self.assertEqual(profile.first_filament_type, "PLA")

    def test_first_filament_type_empty(self):
        profile = OrcaFilamentProfile.objects.create(
            name="Empty",
            orca_name="empty",
            settings={},
        )
        self.assertIsNone(profile.first_filament_type)

    def test_first_nozzle_temperature(self):
        profile = OrcaFilamentProfile.objects.create(
            name="PLA",
            orca_name="pla",
            settings={"nozzle_temperature": [210]},
        )
        self.assertEqual(profile.first_nozzle_temperature, 210)

    def test_first_nozzle_temperature_none(self):
        profile = OrcaFilamentProfile.objects.create(
            name="Empty",
            orca_name="empty",
            settings={},
        )
        self.assertIsNone(profile.first_nozzle_temperature)

    def test_first_bed_temperature(self):
        profile = OrcaFilamentProfile.objects.create(
            name="PLA",
            orca_name="pla",
            settings={"bed_temperature": [60]},
        )
        self.assertEqual(profile.first_bed_temperature, 60)

    def test_first_bed_temperature_none(self):
        profile = OrcaFilamentProfile.objects.create(
            name="Empty",
            orca_name="empty",
            settings={},
        )
        self.assertIsNone(profile.first_bed_temperature)

    def test_first_max_volumetric_speed(self):
        profile = OrcaFilamentProfile.objects.create(
            name="PLA",
            orca_name="pla",
            settings={"filament_max_volumetric_speed": [12.5]},
        )
        self.assertAlmostEqual(profile.first_max_volumetric_speed, 12.5)

    def test_first_max_volumetric_speed_none(self):
        profile = OrcaFilamentProfile.objects.create(
            name="Empty",
            orca_name="empty",
            settings={},
        )
        self.assertIsNone(profile.first_max_volumetric_speed)

    def test_first_nozzle_temperature_invalid(self):
        profile = OrcaFilamentProfile.objects.create(
            name="Bad",
            orca_name="bad",
            settings={"nozzle_temperature": ["not_a_number"]},
        )
        self.assertIsNone(profile.first_nozzle_temperature)


class OrcaMachineProfileTests(TestCase):
    """Tests for OrcaMachineProfile-specific properties."""

    def test_bed_size_from_printable_area(self):
        profile = OrcaMachineProfile.objects.create(
            name="Machine",
            orca_name="machine",
            settings={"printable_area": ["0x0", "256x0", "256x256", "0x256"]},
        )
        self.assertAlmostEqual(profile.bed_size_x, 256.0)
        self.assertAlmostEqual(profile.bed_size_y, 256.0)

    def test_bed_size_no_printable_area(self):
        profile = OrcaMachineProfile.objects.create(
            name="Machine",
            orca_name="machine",
            settings={},
        )
        self.assertIsNone(profile.bed_size_x)
        self.assertIsNone(profile.bed_size_y)

    def test_first_nozzle_diameter(self):
        profile = OrcaMachineProfile.objects.create(
            name="Machine",
            orca_name="machine",
            settings={"nozzle_diameter": [0.4]},
        )
        self.assertAlmostEqual(profile.first_nozzle_diameter, 0.4)

    def test_first_nozzle_diameter_none(self):
        profile = OrcaMachineProfile.objects.create(
            name="Machine",
            orca_name="machine",
            settings={},
        )
        self.assertIsNone(profile.first_nozzle_diameter)

    def test_printable_height(self):
        profile = OrcaMachineProfile.objects.create(
            name="Machine",
            orca_name="machine",
            settings={"printable_height": 250},
        )
        self.assertAlmostEqual(profile.printable_height, 250.0)

    def test_printable_height_none(self):
        profile = OrcaMachineProfile.objects.create(
            name="Machine",
            orca_name="machine",
            settings={},
        )
        self.assertIsNone(profile.printable_height)


class OrcaPrintPresetTests(TestCase):
    """Tests for OrcaPrintPreset-specific properties."""

    def test_infill_density_display(self):
        preset = OrcaPrintPreset.objects.create(
            name="Standard",
            orca_name="standard",
            settings={"sparse_infill_density": "15%"},
        )
        self.assertEqual(preset.infill_density_display, "15%")

    def test_infill_density_display_none(self):
        preset = OrcaPrintPreset.objects.create(
            name="Standard",
            orca_name="standard",
            settings={},
        )
        self.assertIsNone(preset.infill_density_display)

    def test_supports_enabled_true(self):
        preset = OrcaPrintPreset.objects.create(
            name="Standard",
            orca_name="standard",
            settings={"enable_support": True},
        )
        self.assertTrue(preset.supports_enabled)

    def test_supports_enabled_false(self):
        preset = OrcaPrintPreset.objects.create(
            name="Standard",
            orca_name="standard",
            settings={"enable_support": False},
        )
        self.assertFalse(preset.supports_enabled)

    def test_supports_enabled_missing(self):
        preset = OrcaPrintPreset.objects.create(
            name="Standard",
            orca_name="standard",
            settings={},
        )
        self.assertFalse(preset.supports_enabled)
