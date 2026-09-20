"""DRF views for the iterative build API.

Top-level resources (projects, parts, hardware catalogue) are ``ModelViewSet``s exposed
through a router; composition edges, documents and hardware assignments are nested under
a project via generic list/create + detail views. Edge creates are idempotent
(``get_or_create`` against the unique constraints) and the component edge is cycle-guarded
so a client can retry a build step without producing duplicates or ``500``s.
"""

from __future__ import annotations

from typing import Any

from django.shortcuts import get_object_or_404
from rest_framework import filters, generics, status, viewsets
from rest_framework.decorators import action
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.request import Request
from rest_framework.response import Response

from core.api.permissions import ReadOrProjectManage
from core.api.serializers import (
    HardwarePartSerializer,
    OrcaPrintPresetSerializer,
    PartSerializer,
    ProjectComponentSerializer,
    ProjectDocumentSerializer,
    ProjectHardwareSerializer,
    ProjectPartEdgeSerializer,
    ProjectSerializer,
    ProjectTreeSerializer,
    SpoolmanFilamentMappingSerializer,
)
from core.models import (
    HardwarePart,
    OrcaPrintPreset,
    Part,
    Project,
    ProjectComponent,
    ProjectDocument,
    ProjectHardware,
    ProjectPart,
    SpoolmanFilamentMapping,
)
from core.views.helpers import _trigger_part_estimation

# STL upload limits mirror the UI ``PartForm.clean_stl_file`` rules.
STL_MAX_SIZE = 100 * 1024 * 1024  # 100 MB


class ProjectViewSet(viewsets.ModelViewSet):
    """CRUD for projects plus a nested read-only assembly ``tree``."""

    queryset = Project.objects.all()
    serializer_class = ProjectSerializer
    permission_classes = [ReadOrProjectManage]

    @action(detail=True, methods=["get"])
    def tree(self, request: Request, pk: str | None = None) -> Response:
        """Return the nested assembly tree (child modules, parts, hardware) for orientation."""
        project = self.get_object()
        serializer = ProjectTreeSerializer(project, context=self.get_serializer_context())
        return Response(serializer.data)


class PartViewSet(viewsets.ModelViewSet):
    """CRUD for parts plus a multipart ``stl`` upload action that triggers estimation."""

    queryset = Part.objects.all()
    serializer_class = PartSerializer
    permission_classes = [ReadOrProjectManage]

    @action(
        detail=True,
        methods=["post"],
        url_path="stl",
        parser_classes=[MultiPartParser, FormParser],
    )
    def stl(self, request: Request, pk: str | None = None) -> Response:
        """Attach an uploaded STL file to the part and trigger the same estimation as the UI."""
        part = self.get_object()
        uploaded = request.FILES.get("stl_file") or request.FILES.get("file")
        if uploaded is None:
            return Response(
                {"stl_file": ["No file supplied (use the 'stl_file' multipart field)."]},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if not uploaded.name.lower().endswith(".stl"):
            return Response({"stl_file": ["Only STL files are allowed."]}, status=status.HTTP_400_BAD_REQUEST)
        if uploaded.size > STL_MAX_SIZE:
            return Response({"stl_file": ["File size must be under 100 MB."]}, status=status.HTTP_400_BAD_REQUEST)

        part.stl_file = uploaded
        part.save()
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


class ProjectComponentListCreate(_ProjectScopedMixin, generics.ListCreateAPIView):
    """List / create sub-project composition edges under a parent project (idempotent)."""

    serializer_class = ProjectComponentSerializer

    def get_queryset(self) -> Any:
        """Return the edges whose parent is the URL project."""
        return ProjectComponent.objects.filter(parent_project_id=self.kwargs["project_pk"])

    def get_serializer_context(self) -> dict[str, Any]:
        """Expose the parent project so the serializer's cycle guard can run."""
        context = super().get_serializer_context()
        context["parent_project"] = self.get_project()
        return context

    def create(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        """Validate (incl. cycle guard) then ``get_or_create`` the edge for idempotency."""
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        parent = self.get_project()
        child = serializer.validated_data["child_project"]
        quantity = serializer.validated_data.get("quantity", 1)
        edge, created = ProjectComponent.objects.get_or_create(
            parent_project=parent,
            child_project=child,
            defaults={"quantity": quantity},
        )
        out = self.get_serializer(edge)
        return Response(out.data, status=status.HTTP_201_CREATED if created else status.HTTP_200_OK)


class ProjectComponentDetail(_ProjectScopedMixin, generics.RetrieveUpdateDestroyAPIView):
    """Retrieve / update quantity / delete a single sub-project composition edge."""

    serializer_class = ProjectComponentSerializer

    def get_queryset(self) -> Any:
        """Scope edges to the URL parent project."""
        return ProjectComponent.objects.filter(parent_project_id=self.kwargs["project_pk"])

    def get_serializer_context(self) -> dict[str, Any]:
        """Expose the parent project so the serializer's cycle guard can run."""
        context = super().get_serializer_context()
        context["parent_project"] = self.get_project()
        return context


class ProjectPartListCreate(_ProjectScopedMixin, generics.ListCreateAPIView):
    """List / attach reusable parts to a project via edges (idempotent)."""

    serializer_class = ProjectPartEdgeSerializer

    def get_queryset(self) -> Any:
        """Return the part edges of the URL project."""
        return ProjectPart.objects.filter(project_id=self.kwargs["project_pk"])

    def create(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        """Validate then ``get_or_create`` the project↔part edge for idempotency."""
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        project = self.get_project()
        part = serializer.validated_data["part"]
        quantity = serializer.validated_data.get("quantity", 1)
        edge, created = ProjectPart.objects.get_or_create(
            project=project,
            part=part,
            defaults={"quantity": quantity},
        )
        out = self.get_serializer(edge)
        return Response(out.data, status=status.HTTP_201_CREATED if created else status.HTTP_200_OK)


class ProjectPartDetail(_ProjectScopedMixin, generics.RetrieveUpdateDestroyAPIView):
    """Retrieve / update quantity / delete a single project↔part edge."""

    serializer_class = ProjectPartEdgeSerializer

    def get_queryset(self) -> Any:
        """Scope edges to the URL project."""
        return ProjectPart.objects.filter(project_id=self.kwargs["project_pk"])


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
    """List / assign hardware parts to a project via edges (idempotent)."""

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
