"""Tests for top-down, per-build-path print preset resolution (Variant B)."""

from django.test import TestCase

from core.models import OrcaPrintPreset, Part, Project, ProjectComponent, ProjectPart
from core.models.parts import resolve_part_preset


def _preset(name: str) -> OrcaPrintPreset:
    return OrcaPrintPreset.objects.create(
        name=name,
        orca_name=name,
        state=OrcaPrintPreset.STATE_RESOLVED,
        instantiation=True,
    )


class ResolvePartPresetLeafTests(TestCase):
    def test_override_wins_over_nearest_project(self) -> None:
        override = _preset("Override")
        nearest = _preset("Nearest")
        project = Project.objects.create(name="M", default_print_preset=nearest)
        part = Part.objects.create(name="p", print_preset=override)
        self.assertEqual(resolve_part_preset(part, project), override)

    def test_falls_back_to_nearest_project_preset(self) -> None:
        nearest = _preset("Nearest")
        project = Project.objects.create(name="M", default_print_preset=nearest)
        part = Part.objects.create(name="p")
        self.assertEqual(resolve_part_preset(part, project), nearest)

    def test_no_override_no_project_returns_none(self) -> None:
        part = Part.objects.create(name="p")
        self.assertIsNone(resolve_part_preset(part, None))


class ResolvePartPresetsPerPathTests(TestCase):
    def test_nearest_project_preset_carried_per_path(self) -> None:
        cabin_preset = _preset("CabinPreset")
        frame_preset = _preset("FramePreset")
        truck = Project.objects.create(name="Truck", default_print_preset=_preset("TruckPreset"))
        cabin = Project.objects.create(name="Cabin", default_print_preset=cabin_preset)
        frame = Project.objects.create(name="Frame", default_print_preset=frame_preset)
        ProjectComponent.objects.create(parent_project=truck, child_project=cabin, quantity=1)
        ProjectComponent.objects.create(parent_project=truck, child_project=frame, quantity=1)
        bolt = Part.objects.create(name="bolt")  # no override
        ProjectPart.objects.create(project=cabin, part=bolt, quantity=4)
        ProjectPart.objects.create(project=frame, part=bolt, quantity=10)

        resolved = truck.resolve_part_presets()
        presets = sorted((mult, preset.name) for _p, mult, preset in resolved)
        self.assertEqual(presets, [(4, "CabinPreset"), (10, "FramePreset")])

    def test_part_override_beats_nearest_on_every_path(self) -> None:
        override = _preset("Override")
        cabin = Project.objects.create(name="Cabin", default_print_preset=_preset("CabinPreset"))
        bolt = Part.objects.create(name="bolt", print_preset=override)
        ProjectPart.objects.create(project=cabin, part=bolt, quantity=2)
        resolved = cabin.resolve_part_presets()
        self.assertEqual([(p.name, m, pr.name) for p, m, pr in resolved], [("bolt", 2, "Override")])
