"""Tests for the copy views and "add existing" (as deep copy) in the web UI."""

import shutil
import tempfile
from unittest import mock

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from django.urls import reverse

from core.models import OrcaPrintPreset, Part, Project
from core.tests.mixins import _RBACTestBase

MEDIA_ROOT = tempfile.mkdtemp(prefix="layernexus-copyviews-")


def _preset(name: str) -> OrcaPrintPreset:
    """Create a resolved, instantiable print preset."""
    return OrcaPrintPreset.objects.create(
        name=name, orca_name=name, state=OrcaPrintPreset.STATE_RESOLVED, instantiation=True
    )


@override_settings(MEDIA_ROOT=MEDIA_ROOT)
@mock.patch("core.views.helpers._start_orcaslicer_worker")
class CopyViewTests(_RBACTestBase):
    """R (preset X) → S (no preset) → T; plus a separate root B (preset Y)."""

    @classmethod
    def tearDownClass(cls) -> None:
        super().tearDownClass()
        shutil.rmtree(MEDIA_ROOT, ignore_errors=True)

    def setUp(self) -> None:
        super().setUp()
        self.client.login(username="designer_user", password="testpass123")
        self.preset_x = _preset("X")
        self.preset_y = _preset("Y")
        self.root = Project.objects.create(name="R", default_print_preset=self.preset_x)
        self.sub = Project.objects.create(name="S", parent=self.root, quantity=2)
        self.leaf = Project.objects.create(name="T", parent=self.sub)
        self.other = Project.objects.create(name="B", default_print_preset=self.preset_y)
        self.same = Project.objects.create(name="C", default_print_preset=self.preset_x)
        self.sub_part = Part.objects.create(
            name="s-part",
            project=self.sub,
            stl_file=SimpleUploadedFile("s.stl", b"solid s"),
            estimation_status=Part.ESTIMATION_SUCCESS,
            estimated_with_preset=self.preset_x,
            filament_used_grams=5.0,
        )

    def _copy_url(self, project: Project) -> str:
        return reverse("core:project_copy", args=[project.pk])

    def test_copy_get_prefills_name_and_excludes_subtree(self, _worker: mock.Mock) -> None:
        resp = self.client.get(self._copy_url(self.root))
        self.assertEqual(resp.status_code, 200)
        form = resp.context["form"]
        self.assertEqual(form["name"].value(), "R (copy)")
        targets = set(form.fields["target"].queryset)
        self.assertFalse({self.root, self.sub, self.leaf} & targets)
        self.assertIn(self.other, targets)
        self.assertContains(resp, "As new top-level project")

    def test_copy_target_labels_show_path(self, _worker: mock.Mock) -> None:
        resp = self.client.get(self._copy_url(self.other))
        self.assertContains(resp, "R › S › T")

    def test_copy_as_new_root(self, _worker: mock.Mock) -> None:
        resp = self.client.post(self._copy_url(self.sub), {"name": "S2", "target": ""})
        new = Project.objects.get(name="S2")
        self.assertRedirects(resp, reverse("core:project_detail", args=[new.pk]))
        self.assertIsNone(new.parent)
        self.assertEqual(new.default_print_preset, self.preset_x)
        self.assertEqual(new.subprojects.get().name, "T")
        self.assertEqual(self.sub.parent, self.root)

    def test_copy_into_other_project_as_subproject(self, _worker: mock.Mock) -> None:
        self.client.post(self._copy_url(self.sub), {"name": "S-in-B", "target": self.other.pk})
        new = Project.objects.get(name="S-in-B")
        self.assertEqual(new.parent, self.other)
        self.assertEqual(new.quantity, 2)

    def test_copy_post_with_target_in_subtree_rejected(self, _worker: mock.Mock) -> None:
        count = Project.objects.count()
        for target in (self.root, self.leaf):
            resp = self.client.post(self._copy_url(self.root), {"name": "loop", "target": target.pk})
            self.assertEqual(resp.status_code, 200)
            self.assertTrue(resp.context["form"].errors["target"])
        self.assertEqual(Project.objects.count(), count)

    def test_copy_post_model_validation_error_shown(self, _worker: mock.Mock) -> None:
        from django.core.exceptions import ValidationError

        with mock.patch.object(Project, "deep_copy", side_effect=ValidationError({"parent": "nope"})):
            resp = self.client.post(self._copy_url(self.sub), {"name": "x", "target": self.other.pk})
        self.assertEqual(resp.status_code, 200)
        self.assertIn("nope", str(resp.context["form"].errors))

    def test_copy_requeues_only_parts_with_changed_preset(self, worker: mock.Mock) -> None:
        self.client.post(self._copy_url(self.sub), {"name": "into-B", "target": self.other.pk})
        moved = Project.objects.get(name="into-B").parts.get()
        self.assertEqual(moved.estimation_status, Part.ESTIMATION_PENDING)
        self.assertIsNone(moved.estimated_with_preset)
        self.assertIsNone(moved.filament_used_grams)
        worker.assert_called()

        worker.reset_mock()
        self.client.post(self._copy_url(self.sub), {"name": "into-C", "target": self.same.pk})
        kept = Project.objects.get(name="into-C").parts.get()
        self.assertEqual(kept.estimation_status, Part.ESTIMATION_SUCCESS)
        self.assertEqual(kept.filament_used_grams, 5.0)
        worker.assert_not_called()

    def test_part_copy_get_prefills_source_project(self, _worker: mock.Mock) -> None:
        resp = self.client.get(reverse("core:part_copy", args=[self.sub_part.pk]))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.context["form"]["project"].value(), self.sub.pk)
        self.assertContains(resp, "R › S")

    def test_part_copy_into_other_project(self, worker: mock.Mock) -> None:
        resp = self.client.post(reverse("core:part_copy", args=[self.sub_part.pk]), {"project": self.other.pk})
        new = self.other.parts.get()
        self.assertRedirects(resp, reverse("core:part_detail", args=[new.pk]))
        self.assertEqual(new.name, "s-part")
        self.assertEqual(new.estimation_status, Part.ESTIMATION_PENDING)
        self.sub_part.refresh_from_db()
        self.assertEqual(self.sub_part.project, self.sub)
        self.assertEqual(self.sub_part.estimation_status, Part.ESTIMATION_SUCCESS)

    def test_part_copy_unknown_project_rejected(self, _worker: mock.Mock) -> None:
        count = Part.objects.count()
        resp = self.client.post(reverse("core:part_copy", args=[self.sub_part.pk]), {"project": 999999})
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.context["form"].errors["project"])
        self.assertEqual(Part.objects.count(), count)

    def test_copy_forbidden_for_operator(self, _worker: mock.Mock) -> None:
        self.client.login(username="operator_user", password="testpass123")
        for url in (self._copy_url(self.root), reverse("core:part_copy", args=[self.sub_part.pk])):
            self.assertEqual(self.client.get(url).status_code, 403)
            self.assertEqual(self.client.post(url, {"name": "x", "project": self.other.pk}).status_code, 403)

    def test_copy_button_visible_for_designer_only(self, _worker: mock.Mock) -> None:
        project_url = reverse("core:project_detail", args=[self.root.pk])
        part_url = reverse("core:part_detail", args=[self.sub_part.pk])
        self.assertContains(self.client.get(project_url), self._copy_url(self.root))
        self.assertContains(self.client.get(part_url), reverse("core:part_copy", args=[self.sub_part.pk]))
        self.client.login(username="operator_user", password="testpass123")
        self.assertNotContains(self.client.get(project_url), self._copy_url(self.root))
        self.assertNotContains(self.client.get(part_url), reverse("core:part_copy", args=[self.sub_part.pk]))

    # ------------------------------------------------------------------
    # "Add existing" as deep copy
    # ------------------------------------------------------------------

    def test_add_existing_part_copies_and_leaves_original(self, _worker: mock.Mock) -> None:
        count = Part.objects.count()
        resp = self.client.post(
            reverse("core:project_add_existing_part", args=[self.other.pk]), {"part": self.sub_part.pk}
        )
        self.assertRedirects(resp, reverse("core:project_detail", args=[self.other.pk]))
        self.assertEqual(Part.objects.count(), count + 1)
        self.assertEqual(self.other.parts.get().name, "s-part")
        self.sub_part.refresh_from_db()
        self.assertEqual(self.sub_part.project_id, self.sub.pk)

    def test_add_existing_part_unknown_part_shows_error(self, _worker: mock.Mock) -> None:
        count = Part.objects.count()
        resp = self.client.post(reverse("core:project_add_existing_part", args=[self.other.pk]), {"part": 999999})
        self.assertRedirects(resp, reverse("core:project_detail", args=[self.other.pk]))
        self.assertEqual(Part.objects.count(), count)

    def test_add_existing_subproject_copies_subtree(self, _worker: mock.Mock) -> None:
        resp = self.client.post(
            reverse("core:project_add_existing_subproject", args=[self.other.pk]), {"source": self.sub.pk}
        )
        self.assertRedirects(resp, reverse("core:project_detail", args=[self.other.pk]))
        new = self.other.subprojects.get()
        self.assertEqual(new.name, "S")
        self.assertEqual(new.subprojects.get().name, "T")
        self.assertEqual(new.parts.get().name, "s-part")
        self.sub.refresh_from_db()
        self.assertEqual(self.sub.parent, self.root)

    def test_add_existing_subproject_choices_exclude_own_subtree(self, _worker: mock.Mock) -> None:
        resp = self.client.get(reverse("core:project_detail", args=[self.sub.pk]))
        form = resp.context["add_existing_subproject_form"]
        sources = set(form.fields["source"].queryset)
        self.assertFalse({self.sub, self.leaf} & sources)
        self.assertIn(self.other, sources)
        self.assertIn(self.root, sources)
        self.assertIn("add_existing_part_form", resp.context)

    def test_add_existing_subproject_forged_ancestor_rejected(self, _worker: mock.Mock) -> None:
        before = (Project.objects.count(), Part.objects.count())
        for source in (self.root, self.sub):
            resp = self.client.post(
                reverse("core:project_add_existing_subproject", args=[self.sub.pk]), {"source": source.pk}, follow=True
            )
            self.assertEqual([m.level_tag for m in resp.context["messages"]], ["error"])
        self.assertEqual((Project.objects.count(), Part.objects.count()), before)

    def test_add_existing_forbidden_for_operator(self, _worker: mock.Mock) -> None:
        self.client.login(username="operator_user", password="testpass123")
        before = (Project.objects.count(), Part.objects.count())
        resp = self.client.post(
            reverse("core:project_add_existing_part", args=[self.other.pk]), {"part": self.sub_part.pk}
        )
        self.assertEqual(resp.status_code, 403)
        resp = self.client.post(
            reverse("core:project_add_existing_subproject", args=[self.other.pk]), {"source": self.sub.pk}
        )
        self.assertEqual(resp.status_code, 403)
        self.assertEqual((Project.objects.count(), Part.objects.count()), before)
        detail = self.client.get(reverse("core:project_detail", args=[self.other.pk]))
        self.assertNotIn("add_existing_part_form", detail.context)

    def test_add_existing_get_not_allowed(self, _worker: mock.Mock) -> None:
        resp = self.client.get(reverse("core:project_add_existing_part", args=[self.other.pk]))
        self.assertEqual(resp.status_code, 405)
