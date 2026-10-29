"""Tests for moving parts and sub-projects through the web edit forms."""

from unittest import mock

from django.urls import reverse

from core.models import OrcaPrintPreset, Part, PrintJob, PrintJobPart, Project
from core.tests.mixins import _RBACTestBase


def _preset(name: str) -> OrcaPrintPreset:
    """Create a resolved, instantiable print preset."""
    return OrcaPrintPreset.objects.create(
        name=name, orca_name=name, state=OrcaPrintPreset.STATE_RESOLVED, instantiation=True
    )


@mock.patch("core.views.helpers._start_orcaslicer_worker")
class MoveViewTests(_RBACTestBase):
    """R (preset X) → S → T; a second root B (preset Y); a third root C (preset X)."""

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
        self.part = Part.objects.create(
            name="leaf-part",
            project=self.leaf,
            stl_file="stl_files/leaf.stl",
            estimation_status=Part.ESTIMATION_SUCCESS,
            estimated_with_preset=self.preset_x,
            filament_used_grams=5.0,
        )

    def _part_data(self, project: Project, **overrides: object) -> dict[str, object]:
        data: dict[str, object] = {
            "name": self.part.name,
            "quantity": self.part.quantity,
            "project": project.pk,
            "color": "",
            "material": "",
        }
        data.update(overrides)
        return data

    def _project_data(self, project: Project, parent: Project | None, **overrides: object) -> dict[str, object]:
        data: dict[str, object] = {
            "name": project.name,
            "description": "",
            "quantity": project.quantity,
            "parent": parent.pk if parent else "",
            "default_print_preset": project.default_print_preset_id or "",
        }
        data.update(overrides)
        return data

    def _edit_project(self, project: Project, data: dict[str, object]):  # noqa: ANN202
        return self.client.post(reverse("core:project_update", args=[project.pk]), data)

    def test_part_form_offers_project_with_path_labels(self, _worker: mock.Mock) -> None:
        resp = self.client.get(reverse("core:part_update", args=[self.part.pk]))
        field = resp.context["form"].fields["project"]
        labels = dict(field.choices)
        self.assertEqual(labels[self.leaf.pk], "R › S › T")
        self.assertEqual(resp.context["form"].initial["project"], self.leaf.pk)

    def test_part_create_form_has_no_project_field(self, _worker: mock.Mock) -> None:
        resp = self.client.get(reverse("core:part_create", args=[self.leaf.pk]))
        self.assertNotIn("project", resp.context["form"].fields)

    def test_move_part_to_other_project_keeps_print_history(self, _worker: mock.Mock) -> None:
        job = PrintJob.objects.create(name="done", status=PrintJob.STATUS_COMPLETED)
        PrintJobPart.objects.create(print_job=job, part=self.part, quantity=1)
        resp = self.client.post(reverse("core:part_update", args=[self.part.pk]), self._part_data(self.same))
        self.assertRedirects(resp, reverse("core:part_detail", args=[self.part.pk]))
        self.part.refresh_from_db()
        self.assertEqual(self.part.project, self.same)
        self.assertEqual(PrintJobPart.objects.filter(part=self.part).count(), 1)
        self.assertEqual(self.part.stl_file.name, "stl_files/leaf.stl")

    def test_move_part_same_preset_keeps_estimate(self, worker: mock.Mock) -> None:
        self.client.post(reverse("core:part_update", args=[self.part.pk]), self._part_data(self.same))
        self.part.refresh_from_db()
        self.assertEqual(self.part.estimation_status, Part.ESTIMATION_SUCCESS)
        self.assertEqual(self.part.filament_used_grams, 5.0)
        worker.assert_not_called()

    def test_move_part_with_changed_preset_requeues(self, worker: mock.Mock) -> None:
        self.client.post(reverse("core:part_update", args=[self.part.pk]), self._part_data(self.other))
        self.part.refresh_from_db()
        self.assertEqual(self.part.project, self.other)
        self.assertEqual(self.part.estimation_status, Part.ESTIMATION_PENDING)
        self.assertIsNone(self.part.filament_used_grams)
        worker.assert_called()

    def test_project_edit_form_parent_excludes_own_subtree(self, _worker: mock.Mock) -> None:
        resp = self.client.get(reverse("core:project_update", args=[self.sub.pk]))
        field = resp.context["form"].fields["parent"]
        offered = {pk for pk, _label in field.choices if pk != ""}
        self.assertLessEqual({self.root.pk, self.other.pk, self.same.pk}, offered)
        self.assertFalse({self.sub.pk, self.leaf.pk} & offered)
        self.assertFalse(field.required)
        self.assertEqual(dict(field.choices)[""], "(top-level project)")

    def test_root_edit_form_offers_optional_parent(self, _worker: mock.Mock) -> None:
        resp = self.client.get(reverse("core:project_update", args=[self.root.pk]))
        field = resp.context["form"].fields["parent"]
        self.assertFalse(field.required)
        self.assertNotIn(self.leaf.pk, {pk for pk, _label in field.choices})

    def test_move_subproject_to_other_parent(self, _worker: mock.Mock) -> None:
        resp = self._edit_project(self.sub, self._project_data(self.sub, self.same))
        self.assertRedirects(resp, reverse("core:project_detail", args=[self.sub.pk]))
        self.sub.refresh_from_db()
        self.assertEqual(self.sub.parent, self.same)
        self.assertEqual(self.sub.quantity, 2)
        self.leaf.refresh_from_db()
        self.assertEqual(self.leaf.parent, self.sub)

    def test_move_into_own_subtree_rejected(self, _worker: mock.Mock) -> None:
        for target in (self.sub, self.leaf):
            resp = self._edit_project(self.sub, self._project_data(self.sub, target))
            self.assertEqual(resp.status_code, 200)
            self.assertIn("parent", resp.context["form"].errors)
            self.sub.refresh_from_db()
            self.assertEqual(self.sub.parent, self.root)

    def test_move_into_own_subtree_rechecked_on_save(self, _worker: mock.Mock) -> None:
        """A cycle that appears between form validation and save is still refused."""
        original_clean = Project.clean

        def _reparent_target_first(project: Project) -> None:
            # Simulate a concurrent move: the target becomes a descendant of the moved project.
            Project.objects.filter(pk=self.other.pk).update(parent=self.leaf)
            original_clean(project)

        with mock.patch.object(Project, "clean", _reparent_target_first):
            resp = self._edit_project(self.sub, self._project_data(self.sub, self.other))
        self.assertEqual(resp.status_code, 200)
        self.assertIn("parent", resp.context["form"].errors)
        self.sub.refresh_from_db()
        self.assertEqual(self.sub.parent, self.root)

    def test_move_subproject_to_root_requires_preset(self, _worker: mock.Mock) -> None:
        resp = self._edit_project(self.sub, self._project_data(self.sub, None))
        self.assertEqual(resp.status_code, 200)
        self.assertIn("default_print_preset", resp.context["form"].errors)
        self.sub.refresh_from_db()
        self.assertEqual(self.sub.parent, self.root)

        resp = self._edit_project(self.sub, self._project_data(self.sub, None, default_print_preset=self.preset_y.pk))
        self.assertEqual(resp.status_code, 302)
        self.sub.refresh_from_db()
        self.assertIsNone(self.sub.parent)
        self.assertEqual(self.sub.default_print_preset, self.preset_y)
        self.assertEqual(self.sub.quantity, 1)  # roots always have quantity 1

    def test_root_cannot_clear_its_preset(self, _worker: mock.Mock) -> None:
        resp = self._edit_project(self.root, self._project_data(self.root, None, default_print_preset=""))
        self.assertEqual(resp.status_code, 200)
        self.assertIn("default_print_preset", resp.context["form"].errors)

    def test_move_root_under_other_project(self, _worker: mock.Mock) -> None:
        resp = self._edit_project(self.root, self._project_data(self.root, self.other))
        self.assertEqual(resp.status_code, 302)
        self.root.refresh_from_db()
        self.assertEqual(self.root.parent, self.other)
        self.assertEqual(self.root.default_print_preset, self.preset_x)

    def test_move_requeues_parts_with_changed_preset(self, worker: mock.Mock) -> None:
        own = Part.objects.create(
            name="own-preset",
            project=self.sub,
            stl_file="stl_files/own.stl",
            print_preset=self.preset_x,
            estimation_status=Part.ESTIMATION_SUCCESS,
            estimated_with_preset=self.preset_x,
        )
        self._edit_project(self.sub, self._project_data(self.sub, self.other))
        self.part.refresh_from_db()
        own.refresh_from_db()
        self.assertEqual(self.part.estimation_status, Part.ESTIMATION_PENDING)  # X → Y (inherited)
        self.assertEqual(own.estimation_status, Part.ESTIMATION_SUCCESS)  # own preset: unchanged
        worker.assert_called()

    def test_move_with_unchanged_preset_does_not_requeue(self, worker: mock.Mock) -> None:
        self._edit_project(self.sub, self._project_data(self.sub, self.same))
        self.part.refresh_from_db()
        self.assertEqual(self.part.estimation_status, Part.ESTIMATION_SUCCESS)
        worker.assert_not_called()

    def test_changing_project_preset_requeues_inheriting_parts(self, worker: mock.Mock) -> None:
        self._edit_project(self.root, self._project_data(self.root, None, default_print_preset=self.preset_y.pk))
        self.part.refresh_from_db()
        self.assertEqual(self.part.estimation_status, Part.ESTIMATION_PENDING)
        worker.assert_called()

    def test_move_forbidden_for_operator(self, _worker: mock.Mock) -> None:
        self.client.logout()
        self.client.login(username="operator_user", password="testpass123")
        resp = self._edit_project(self.sub, self._project_data(self.sub, self.other))
        self.assertEqual(resp.status_code, 403)
        resp = self.client.post(reverse("core:part_update", args=[self.part.pk]), self._part_data(self.other))
        self.assertEqual(resp.status_code, 403)
        self.sub.refresh_from_db()
        self.part.refresh_from_db()
        self.assertEqual((self.sub.parent, self.part.project), (self.root, self.leaf))

    def test_part_post_without_project_keeps_project(self, _worker: mock.Mock) -> None:
        data = self._part_data(self.leaf)
        del data["project"]
        Part.objects.filter(pk=self.part.pk).update(estimated_with_preset=None)  # e.g. estimation failed
        resp = self.client.post(reverse("core:part_update", args=[self.part.pk]), data)
        self.assertEqual(resp.status_code, 302)
        self.part.refresh_from_db()
        self.assertEqual(self.part.project, self.leaf)
        self.assertEqual(self.part.estimation_status, Part.ESTIMATION_SUCCESS)  # not counted as a move
        _worker.assert_not_called()

    def test_subproject_post_without_parent_does_not_requeue(self, worker: mock.Mock) -> None:
        Part.objects.filter(pk=self.part.pk).update(estimated_with_preset=None)
        data = self._project_data(self.sub, self.root)
        del data["parent"]
        resp = self._edit_project(self.sub, data)
        self.assertEqual(resp.status_code, 302)
        self.part.refresh_from_db()
        self.assertEqual(self.part.estimation_status, Part.ESTIMATION_SUCCESS)
        worker.assert_not_called()

    def test_subtree_move_starts_worker_once(self, worker: mock.Mock) -> None:
        for index in range(3):
            Part.objects.create(
                name=f"extra-{index}",
                project=self.leaf,
                stl_file=f"stl_files/extra-{index}.stl",
                estimation_status=Part.ESTIMATION_SUCCESS,
                estimated_with_preset=self.preset_x,
            )
        self._edit_project(self.sub, self._project_data(self.sub, self.other))
        self.assertEqual(Part.objects.filter(estimation_status=Part.ESTIMATION_PENDING).count(), 4)
        worker.assert_called_once()

    def test_part_post_with_empty_project_is_rejected(self, _worker: mock.Mock) -> None:
        data = {**self._part_data(self.leaf), "project": ""}
        resp = self.client.post(reverse("core:part_update", args=[self.part.pk]), data)
        self.assertEqual(resp.status_code, 200)
        self.assertIn("project", resp.context["form"].errors)
