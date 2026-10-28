"""Tests for bottom-up print preset resolution on the project tree."""

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase

from core.models import OrcaPrintPreset, Part, Project


def _preset(name: str) -> OrcaPrintPreset:
    return OrcaPrintPreset.objects.create(
        name=name,
        orca_name=name,
        state=OrcaPrintPreset.STATE_RESOLVED,
        instantiation=True,
    )


class BottomUpPresetResolutionTests(TestCase):
    """``Part.effective_print_preset``: own preset, else nearest ancestor default."""

    def test_part_override_wins(self) -> None:
        override = _preset("Override")
        project = Project.objects.create(name="M", default_print_preset=_preset("Project"))
        part = Part.objects.create(name="p", project=project, print_preset=override)
        self.assertEqual(part.effective_print_preset, override)
        self.assertEqual(part.effective_print_preset_id, override.pk)

    def test_inherits_from_own_project(self) -> None:
        preset = _preset("Project")
        project = Project.objects.create(name="M", default_print_preset=preset)
        part = Part.objects.create(name="p", project=project)
        self.assertEqual(part.effective_print_preset, preset)
        self.assertEqual(part.effective_print_preset_id, preset.pk)

    def test_inherits_from_nearest_ancestor_skipping_empty_levels(self) -> None:
        preset_a = _preset("A")
        root = Project.objects.create(name="R", default_print_preset=preset_a)
        mid = Project.objects.create(name="M", parent=root)
        leaf = Project.objects.create(name="L", parent=mid)
        part = Part.objects.create(name="p", project=leaf)
        self.assertEqual(part.effective_print_preset, preset_a)

        preset_b = _preset("B")
        mid.default_print_preset = preset_b
        mid.save()
        part = Part.objects.get(pk=part.pk)
        self.assertEqual(part.effective_print_preset, preset_b)
        self.assertEqual(part.effective_print_preset_id, preset_b.pk)

    def test_none_when_no_preset_anywhere(self) -> None:
        root = Project.objects.create(name="R")
        sub = Project.objects.create(name="S", parent=root)
        part = Part.objects.create(name="p", project=sub)
        self.assertIsNone(part.effective_print_preset)
        self.assertIsNone(part.effective_print_preset_id)

    def test_is_estimable_requires_stl_and_resolved_preset(self) -> None:
        bare_root = Project.objects.create(name="Bare")
        root = Project.objects.create(name="R", default_print_preset=_preset("A"))
        stl = SimpleUploadedFile("p.stl", b"solid p")
        no_stl = Part.objects.create(name="no stl", project=root)
        no_preset = Part.objects.create(name="no preset", project=bare_root, stl_file=stl)
        ok = Part.objects.create(name="ok", project=root, stl_file=SimpleUploadedFile("q.stl", b"solid q"))
        self.assertFalse(no_stl.is_estimable())
        self.assertFalse(no_preset.is_estimable())
        self.assertTrue(ok.is_estimable())
