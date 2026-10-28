"""DRF views for the iterative build API.

Top-level resources (projects, parts, hardware catalogue) are ``ModelViewSet``s exposed
through a router; documents and hardware assignments are nested under a project via
generic list/create + detail views. The project tree is expressed by ``Project.parent``
and ``Part.project``; deleting a project deletes its subtree and is refused (``409``)
while unfinished print jobs contain any of its parts.
"""

from __future__ import annotations

from typing import Any

from django.db import IntegrityError, transaction
from django.db.models.deletion import ProtectedError
from django.shortcuts import get_object_or_404
from drf_spectacular.utils import OpenApiResponse, extend_schema, inline_serializer
from rest_framework import filters, generics, serializers, status, viewsets
from rest_framework.decorators import action
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.request import Request
from rest_framework.response import Response

from core.api.permissions import ReadOrProjectManage
from core.api.serializers import (
    HardwarePartSerializer,
    OrcaPrintPresetSerializer,
    PartSerializer,
    ProjectDocumentSerializer,
    ProjectHardwareSerializer,
    ProjectSerializer,
    ProjectTreeSerializer,
    SpoolmanFilamentMappingSerializer,
)
from core.models import (
    HardwarePart,
    OrcaPrintPreset,
    Part,
    Project,
    ProjectDocument,
    ProjectHardware,
    SpoolmanFilamentMapping,
)
from core.views.helpers import _trigger_part_estimation

# Model-file upload limits mirror the UI ``PartForm.clean_stl_file`` rules.
STL_MAX_SIZE = 100 * 1024 * 1024  # 100 MB
ALLOWED_MODEL_EXTENSIONS = (".stl", ".3mf")


def _part_ref(part: Part) -> dict[str, Any]:
    """Return a JSON-safe ``{id, name}`` reference for a part."""
    return {"id": part.pk, "name": part.name}


def _jsonify_filament_requirements(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Replace the ``parts`` model objects in each filament bucket with id/name refs."""
    return [{**row, "parts": [_part_ref(p) for p in row["parts"]]} for row in rows]


def _jsonify_hardware_requirements(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Replace the ``hardware_part`` model object in each bucket with an id/name ref."""
    out: list[dict[str, Any]] = []
    for row in rows:
        hardware_part = row["hardware_part"]
        out.append({**row, "hardware_part": {"id": hardware_part.pk, "name": hardware_part.name}})
    return out


class ProjectViewSet(viewsets.ModelViewSet):
    """CRUD for projects plus a nested read-only ``tree``."""

    queryset = Project.objects.all()
    serializer_class = ProjectSerializer
    permission_classes = [ReadOrProjectManage]
    filter_backends = [filters.SearchFilter]
    search_fields = ["name", "description"]

    #: Actions that need parts plus their print history (progress / printed counts).
    _AGGREGATE_ACTIONS = frozenset({"re_estimate", "requirements"})
    #: Actions that only list the subtree's projects, parts (and hardware).
    _STRUCTURE_ACTIONS = frozenset({"validate", "tree"})

    def get_queryset(self) -> Any:
        """Prefetch the subtree for the actions that walk it (no N+1)."""
        queryset = super().get_queryset()
        if self.action in self._AGGREGATE_ACTIONS:
            queryset = queryset.prefetch_related(*Project.aggregate_prefetch_lookups())
        elif self.action in self._STRUCTURE_ACTIONS:
            lookups: list[str] = []
            prefix = ""
            for _ in range(4):
                lookups += [f"{prefix}parts", f"{prefix}subprojects"]
                prefix += "subprojects__"
            queryset = queryset.prefetch_related(*lookups)
        return queryset

    @extend_schema(
        summary="Delete a project and its whole subtree",
        description=(
            "Deletes the project with all sub-projects, parts, documents and hardware "
            "assignments. Refused with 409 while an unfinished print job (any status "
            "other than completed, failed or cancelled) contains a part of the subtree."
        ),
        responses={
            204: None,
            409: inline_serializer(
                "ProjectDeleteBlocked",
                fields={
                    "detail": serializers.CharField(),
                    "blocking_jobs": serializers.ListField(child=serializers.IntegerField()),
                },
            ),
        },
    )
    def destroy(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        """Delete the subtree unless unfinished print jobs still use it.

        Returns:
            204 on success; 409 with the blocking job ids otherwise.
        """
        project = self.get_object()
        with transaction.atomic():
            blocking = list(project.blocking_jobs().values_list("pk", flat=True))
            if blocking:
                return Response(
                    {
                        "detail": "Project cannot be deleted while unfinished print jobs contain its parts.",
                        "blocking_jobs": blocking,
                    },
                    status=status.HTTP_409_CONFLICT,
                )
            project.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)

    @extend_schema(
        summary="Re-queue estimation for all eligible parts in the project tree",
        description=(
            "Collects every part in the project subtree, clears its prior "
            "estimation results and re-queues it for the background worker. Parts "
            "without an STL file, or with no resolvable preset (no override and no "
            "project default), are silently skipped. "
            "Write action — requires the `can_manage_projects` permission."
        ),
        request=None,
        responses={
            202: inline_serializer(
                "ReEstimateResponse",
                fields={"queued": serializers.IntegerField()},
            )
        },
    )
    @action(detail=True, methods=["post"], url_path="re-estimate")
    def re_estimate(self, request: Request, pk: str | None = None) -> Response:
        """Queue estimation for every eligible part in the project's subtree.

        Mirrors :class:`~core.views.projects.ProjectReEstimateView`: clears the existing
        estimation data of every subtree part and re-queues it. Parts without an STL file
        or with no resolvable preset (no override and no project default up the tree) are
        silently skipped. This is a write action so ``ReadOrProjectManage`` requires the
        ``core.can_manage_projects`` permission.

        Returns:
            202 response with ``{"queued": <count>}``.
        """
        project = self.get_object()
        count = 0
        for part, _needed in project._collect_parts_with_multiplier():
            if not part.stl_file or part.effective_print_preset_id is None:
                continue
            Part.objects.filter(pk=part.pk).update(
                filament_used_grams=None,
                filament_used_meters=None,
                estimated_print_time=None,
                estimation_status=Part.ESTIMATION_NONE,
                estimation_error="",
                estimated_with_preset=None,
            )
            _trigger_part_estimation(part)
            count += 1
        return Response({"queued": count}, status=status.HTTP_202_ACCEPTED)

    @extend_schema(
        summary="Nested tree for this project",
        description="Returns the full sub-project / part / hardware hierarchy for orientation.",
        responses={200: ProjectTreeSerializer},
    )
    @action(detail=True, methods=["get"])
    def tree(self, request: Request, pk: str | None = None) -> Response:
        """Return the nested tree (sub-projects, parts, hardware) for orientation."""
        project = self.get_object()
        serializer = ProjectTreeSerializer(project, context=self.get_serializer_context())
        return Response(serializer.data)

    @extend_schema(
        summary="Aggregate build requirements for this project",
        description=(
            "Returns rolled-up totals and per-filament/hardware breakdowns across the "
            "project subtree: `total_parts_count`, `total_filament_grams/meters`, "
            "`total_hardware_cost`, `filament_requirements`, `hardware_requirements`."
        ),
        responses={
            200: inline_serializer(
                "RequirementsResponse",
                fields={
                    "total_parts_count": serializers.IntegerField(),
                    "total_filament_grams": serializers.FloatField(allow_null=True),
                    "total_filament_meters": serializers.FloatField(allow_null=True),
                    "total_hardware_cost": serializers.FloatField(allow_null=True),
                    "filament_requirements": serializers.ListField(),
                    "hardware_requirements": serializers.ListField(),
                },
            )
        },
    )
    @action(detail=True, methods=["get"])
    def requirements(self, request: Request, pk: str | None = None) -> Response:
        """Return the aggregate build requirements (parts, filament, hardware).

        Reuses the project's existing aggregation methods and sanitises their nested
        model objects into JSON-safe id/name references so an API client can reason
        about what the whole project needs.
        """
        project = self.get_object()
        return Response(
            {
                "total_parts_count": project.total_parts_count,
                "total_filament_grams": project.total_filament_grams,
                "total_filament_meters": project.total_filament_meters,
                "total_hardware_cost": project.total_hardware_cost,
                "filament_requirements": _jsonify_filament_requirements(project.filament_requirements()),
                "hardware_requirements": _jsonify_hardware_requirements(project.hardware_requirements()),
            }
        )

    @extend_schema(
        summary="Completeness check for this project",
        description=(
            "Flags parts missing an STL file, a Spoolman filament id, or whose "
            "estimation errored, and flags the project itself when it has no parts. "
            "`ok` is `true` only when `issues` is empty."
        ),
        responses={
            200: inline_serializer(
                "ValidateResponse",
                fields={
                    "ok": serializers.BooleanField(),
                    "issues": serializers.ListField(
                        child=inline_serializer(
                            "ValidationIssue",
                            fields={
                                "part_id": serializers.IntegerField(allow_null=True),
                                "part_name": serializers.CharField(allow_null=True),
                                "issue": serializers.CharField(),
                            },
                        )
                    ),
                },
            )
        },
    )
    @action(detail=True, methods=["get"])
    def validate(self, request: Request, pk: str | None = None) -> Response:
        """Report completeness issues that would block building the project.

        Flags each part of the project subtree that is missing an STL
        file, missing a ``spoolman_filament_id``, or whose estimation errored, plus the
        project itself when it has no parts at all. ``ok`` is ``True`` only when the
        issue list is empty.
        """
        project = self.get_object()
        parts = [part for part, _needed in project._collect_parts_with_multiplier()]
        issues: list[dict[str, Any]] = []
        if not parts:
            issues.append({"part_id": None, "part_name": None, "issue": "Project has no parts."})
        for part in parts:
            if not part.stl_file:
                issues.append({"part_id": part.pk, "part_name": part.name, "issue": "Missing STL file."})
            if part.spoolman_filament_id is None:
                issues.append({"part_id": part.pk, "part_name": part.name, "issue": "Missing Spoolman filament id."})
            if part.estimation_status == Part.ESTIMATION_ERROR:
                issues.append({"part_id": part.pk, "part_name": part.name, "issue": "Estimation status is error."})
        return Response({"ok": not issues, "issues": issues})


class PartViewSet(viewsets.ModelViewSet):
    """CRUD for parts plus a multipart ``stl`` upload action that triggers estimation."""

    queryset = Part.objects.all()
    serializer_class = PartSerializer
    permission_classes = [ReadOrProjectManage]
    filter_backends = [filters.SearchFilter]
    search_fields = ["name", "material"]

    @extend_schema(
        summary="Re-queue estimation for this part",
        description=(
            "Validates prerequisites (STL file present and a resolvable "
            "preset), clears any prior estimation results and triggers the background "
            "worker. Returns 400 only if there is no STL file or no resolvable preset "
            "(prior results are preserved). "
            "Write action — requires the `can_manage_projects` permission."
        ),
        request=None,
        responses={202: PartSerializer, 400: OpenApiResponse(description="Missing STL file or resolvable preset.")},
    )
    @action(detail=True, methods=["post"], url_path="estimate")
    def estimate(self, request: Request, pk: str | None = None) -> Response:
        """Re-queue estimation for a single part, clearing any prior results.

        Mirrors :class:`~core.views.parts.PartReEstimateView`: validates that the part
        has both an STL file and a resolvable print preset, then resets the estimation
        fields to ``none`` and triggers the background worker.  This is a write action
        so ``ReadOrProjectManage`` requires the ``core.can_manage_projects`` permission.

        Returns:
            202 response with the serialized part so the caller can inspect its current
            ``estimation_status``; 400 if a prerequisite is missing (the prior estimate
            is left untouched in that case).
        """
        part = self.get_object()
        # Validate prerequisites before clearing so a part that cannot be estimated
        # (no STL / no preset) keeps its prior result instead of being silently wiped
        # while the background worker no-ops.
        if not part.stl_file:
            return Response(
                {"detail": "Part has no STL file to estimate."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if part.effective_print_preset_id is None:
            return Response(
                {"detail": "Part has no resolvable print preset (no override and no project default)."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        Part.objects.filter(pk=part.pk).update(
            filament_used_grams=None,
            filament_used_meters=None,
            estimated_print_time=None,
            estimation_status=Part.ESTIMATION_NONE,
            estimation_error="",
            estimated_with_preset=None,
        )
        _trigger_part_estimation(part)
        part.refresh_from_db()
        serializer = PartSerializer(part, context=self.get_serializer_context())
        return Response(serializer.data, status=status.HTTP_202_ACCEPTED)

    @extend_schema(
        summary="Upload an STL or 3MF model file for this part",
        description=(
            "Multipart upload: send the file in the `stl_file` field. "
            "Accepted formats: `.stl`, `.3mf`. Max size: 100 MB. "
            "Saves the file on the part and triggers the same background estimation the UI runs. "
            "Write action — requires the `can_manage_projects` permission."
        ),
        request=inline_serializer(
            "StlUploadRequest",
            fields={"stl_file": serializers.FileField()},
        ),
        responses={200: PartSerializer, 400: OpenApiResponse(description="Missing or invalid file.")},
    )
    @action(
        detail=True,
        methods=["post"],
        url_path="stl",
        parser_classes=[MultiPartParser, FormParser],
    )
    def stl(self, request: Request, pk: str | None = None) -> Response:
        """Attach an uploaded model file (STL or 3MF) to the part and trigger the same estimation as the UI."""
        part = self.get_object()
        uploaded = request.FILES.get("stl_file") or request.FILES.get("file")
        if uploaded is None:
            return Response(
                {"stl_file": ["No file supplied (use the 'stl_file' multipart field)."]},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if not uploaded.name.lower().endswith(ALLOWED_MODEL_EXTENSIONS):
            return Response(
                {"stl_file": ["Only STL and 3MF files are allowed."]},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if uploaded.size > STL_MAX_SIZE:
            return Response({"stl_file": ["File size must be under 100 MB."]}, status=status.HTTP_400_BAD_REQUEST)

        part.stl_file = uploaded
        part.save()
        # The model file changed, so the stored estimate's provenance is stale.
        Part.objects.filter(pk=part.pk).update(estimated_with_preset=None)
        _trigger_part_estimation(part)
        serializer = PartSerializer(part, context=self.get_serializer_context())
        return Response(serializer.data, status=status.HTTP_200_OK)


class HardwarePartViewSet(viewsets.ModelViewSet):
    """CRUD for the reusable hardware catalogue."""

    queryset = HardwarePart.objects.all()
    serializer_class = HardwarePartSerializer
    permission_classes = [ReadOrProjectManage]

    def perform_create(self, serializer: HardwarePartSerializer) -> None:
        """Stamp the creating user onto the catalogue entry."""
        serializer.save(created_by=self.request.user)

    def destroy(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        """Delete the part, returning 409 if it is still referenced by projects."""
        instance = self.get_object()
        try:
            instance.delete()
        except (ProtectedError, IntegrityError):
            used_count = instance.project_assignments.count()
            return Response(
                {
                    "detail": (
                        f"Cannot delete '{instance.name}': it is used in {used_count} "
                        f"project{'s' if used_count != 1 else ''}. Remove it from all projects first."
                    )
                },
                status=status.HTTP_409_CONFLICT,
            )
        return Response(status=status.HTTP_204_NO_CONTENT)


class SpoolmanFilamentMappingViewSet(viewsets.ReadOnlyModelViewSet):
    """Read-only lookup of Spoolman filament mappings (valid ``spoolman_filament_id`` choices)."""

    queryset = SpoolmanFilamentMapping.objects.all()
    serializer_class = SpoolmanFilamentMappingSerializer
    permission_classes = [ReadOrProjectManage]


class OrcaPrintPresetViewSet(viewsets.ReadOnlyModelViewSet):
    """Read-only lookup of instantiable, resolved print presets (valid ``part.print_preset`` choices)."""

    queryset = OrcaPrintPreset.objects.filter(
        state=OrcaPrintPreset.STATE_RESOLVED,
        instantiation=True,
    )
    serializer_class = OrcaPrintPresetSerializer
    permission_classes = [ReadOrProjectManage]


class _ProjectScopedMixin:
    """Shared helpers for views nested under ``/projects/{project_pk}/``."""

    permission_classes = [ReadOrProjectManage]

    def get_project(self) -> Project:
        """Return the parent project named by the URL, 404 if it does not exist."""
        return get_object_or_404(Project, pk=self.kwargs["project_pk"])


class ProjectDocumentListCreate(_ProjectScopedMixin, generics.ListCreateAPIView):
    """List / upload documents attached to a project (multipart)."""

    serializer_class = ProjectDocumentSerializer
    parser_classes = [MultiPartParser, FormParser]

    def get_queryset(self) -> Any:
        """Return the documents of the URL project."""
        return ProjectDocument.objects.filter(project_id=self.kwargs["project_pk"])

    def perform_create(self, serializer: ProjectDocumentSerializer) -> None:
        """Attach the document to the URL project and stamp the uploader."""
        serializer.save(project=self.get_project(), uploaded_by=self.request.user)


class ProjectDocumentDetail(_ProjectScopedMixin, generics.RetrieveDestroyAPIView):
    """Retrieve / delete a single project document."""

    serializer_class = ProjectDocumentSerializer

    def get_queryset(self) -> Any:
        """Scope documents to the URL project."""
        return ProjectDocument.objects.filter(project_id=self.kwargs["project_pk"])


class ProjectHardwareListCreate(_ProjectScopedMixin, generics.ListCreateAPIView):
    """List / assign hardware parts to a project (idempotent)."""

    serializer_class = ProjectHardwareSerializer

    def get_queryset(self) -> Any:
        """Return the hardware assignments of the URL project."""
        return ProjectHardware.objects.filter(project_id=self.kwargs["project_pk"])

    def create(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        """Validate then ``get_or_create`` the project↔hardware assignment for idempotency."""
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        project = self.get_project()
        hardware_part = serializer.validated_data["hardware_part"]
        quantity = serializer.validated_data.get("quantity", 1)
        notes = serializer.validated_data.get("notes", "")
        assignment, created = ProjectHardware.objects.get_or_create(
            project=project,
            hardware_part=hardware_part,
            defaults={"quantity": quantity, "notes": notes},
        )
        out = self.get_serializer(assignment)
        return Response(out.data, status=status.HTTP_201_CREATED if created else status.HTTP_200_OK)


class ProjectHardwareDetail(_ProjectScopedMixin, generics.RetrieveUpdateDestroyAPIView):
    """Retrieve / update quantity / delete a single hardware assignment."""

    serializer_class = ProjectHardwareSerializer

    def get_queryset(self) -> Any:
        """Scope hardware assignments to the URL project."""
        return ProjectHardware.objects.filter(project_id=self.kwargs["project_pk"])
