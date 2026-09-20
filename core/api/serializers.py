"""DRF serializers for the iterative build API.

All composition is expressed through the Phase-1 edge models
(:class:`~core.models.composition.ProjectComponent` / ``ProjectPart``); the legacy
``Project.parent`` / ``Part.project`` FKs are not exposed as composition inputs so the
API survives the future Phase-6 contract cleanup. The one unavoidable exception is
:attr:`PartSerializer.project`: ``Part.project`` is currently ``NOT NULL``, so a part
must name its owning module on creation. The model's transitional ``save()`` shim then
keeps the matching ``ProjectPart`` edge in sync, and reusable sharing across assemblies
is done purely via the ``POST /projects/{id}/parts/`` edge endpoint.
"""

from __future__ import annotations

from typing import Any

from rest_framework import serializers

from core.forms.documents import ALLOWED_DOCUMENT_EXTENSIONS, MAX_DOCUMENT_SIZE
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
from core.models.composition import component_would_create_cycle


class ProjectSerializer(serializers.ModelSerializer):
    """Serialize a project's scalar fields.

    Sub-project composition is managed via the ``components`` edge endpoint, not the
    legacy ``parent``/``quantity`` fields, so those are deliberately not writable here.
    """

    class Meta:
        model = Project
        fields = [
            "id",
            "name",
            "description",
            "default_print_preset",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "created_at", "updated_at"]


class PartSerializer(serializers.ModelSerializer):
    """Serialize a part's writable scalar fields plus read-only estimation results.

    ``stl_file`` is read-only here — it is uploaded through the dedicated multipart
    ``POST /parts/{id}/stl/`` endpoint so ordinary create/update stays JSON. ``project``
    is the owning module (required on creation; see the module docstring).
    """

    project = serializers.PrimaryKeyRelatedField(queryset=Project.objects.all())

    class Meta:
        model = Part
        fields = [
            "id",
            "project",
            "name",
            "stl_file",
            "quantity",
            "spoolman_filament_id",
            "color",
            "material",
            "print_preset",
            "notes",
            "filament_used_grams",
            "filament_used_meters",
            "estimated_print_time",
            "estimation_status",
            "estimation_error",
            "created_at",
            "updated_at",
        ]
        read_only_fields = [
            "id",
            "stl_file",
            "filament_used_grams",
            "filament_used_meters",
            "estimated_print_time",
            "estimation_status",
            "estimation_error",
            "created_at",
            "updated_at",
        ]


class ProjectComponentSerializer(serializers.ModelSerializer):
    """Serialize a sub-project composition edge (parent supplied by the URL).

    The cycle guard runs in :meth:`validate` using the parent from the view context, so
    both create and quantity/child updates return a structured ``400`` instead of the
    model's ``save()`` raising an unhandled ``ValidationError`` (which would be a ``500``).
    """

    class Meta:
        model = ProjectComponent
        fields = ["id", "child_project", "quantity"]

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        """Reject edges that would make an assembly (transitively) contain itself."""
        parent = self.context.get("parent_project")
        child = attrs.get("child_project") or getattr(self.instance, "child_project", None)
        if parent is not None and child is not None and component_would_create_cycle(parent.pk, child.pk):
            raise serializers.ValidationError({"child_project": "This would make an assembly contain itself (cycle)."})
        return attrs


class ProjectPartEdgeSerializer(serializers.ModelSerializer):
    """Serialize a project↔part composition edge (project supplied by the URL)."""

    class Meta:
        model = ProjectPart
        fields = ["id", "part", "quantity"]


class ProjectDocumentSerializer(serializers.ModelSerializer):
    """Serialize a project document upload, reusing the UI form's file rules."""

    class Meta:
        model = ProjectDocument
        fields = ["id", "name", "file", "created_at"]
        read_only_fields = ["id", "created_at"]

    def validate_file(self, value: Any) -> Any:
        """Validate the uploaded file's extension and size against the reference form."""
        import os

        _, ext = os.path.splitext(value.name)
        if ext.lower() not in ALLOWED_DOCUMENT_EXTENSIONS:
            allowed = ", ".join(sorted(ALLOWED_DOCUMENT_EXTENSIONS))
            raise serializers.ValidationError(f"File type '{ext}' is not allowed. Allowed types: {allowed}")
        if value.size > MAX_DOCUMENT_SIZE:
            raise serializers.ValidationError("File size must be under 75 MB.")
        return value

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        """Auto-fill ``name`` from the uploaded filename when it is left empty."""
        name = (attrs.get("name") or "").strip()
        if not name:
            uploaded = attrs.get("file")
            if uploaded is not None and getattr(uploaded, "name", ""):
                from pathlib import PurePosixPath

                attrs["name"] = PurePosixPath(uploaded.name).stem
        return attrs


class HardwarePartSerializer(serializers.ModelSerializer):
    """Serialize a reusable hardware catalogue entry."""

    class Meta:
        model = HardwarePart
        fields = ["id", "name", "category", "url", "unit_price", "notes", "created_at", "updated_at"]
        read_only_fields = ["id", "created_at", "updated_at"]


class ProjectHardwareSerializer(serializers.ModelSerializer):
    """Serialize a hardware assignment edge (project supplied by the URL)."""

    class Meta:
        model = ProjectHardware
        fields = ["id", "hardware_part", "quantity", "notes", "created_at"]
        read_only_fields = ["id", "created_at"]


class SpoolmanFilamentMappingSerializer(serializers.ModelSerializer):
    """Read-only lookup of a Spoolman filament mapping so a client can pick a valid id.

    Exposes the identifying id plus the cached display name and color, which is all an
    API client needs to set a part's ``spoolman_filament_id`` to a valid value.
    """

    class Meta:
        model = SpoolmanFilamentMapping
        fields = ["id", "spoolman_filament_id", "spoolman_filament_name", "spoolman_color_hex"]
        read_only_fields = fields


class OrcaPrintPresetSerializer(serializers.ModelSerializer):
    """Read-only lookup of an instantiable print preset (id + name) for ``part.print_preset``."""

    class Meta:
        model = OrcaPrintPreset
        fields = ["id", "name"]
        read_only_fields = fields


class ProjectTreeSerializer(serializers.ModelSerializer):
    """Nested read-only view of an assembly: child modules, direct parts and hardware.

    Traverses the composition edges (``child_modules``/``direct_parts``/
    ``hardware_assignments``) for orientation. A path-local visited set guards against a
    corrupt persisted cycle so rendering always terminates.
    """

    parts = serializers.SerializerMethodField()
    components = serializers.SerializerMethodField()
    hardware = serializers.SerializerMethodField()

    class Meta:
        model = Project
        fields = ["id", "name", "description", "parts", "components", "hardware"]

    def get_parts(self, obj: Project) -> list[dict[str, Any]]:
        """Return ``{part, name, quantity}`` dicts for parts directly attached to ``obj``."""
        return [{"part": part.pk, "name": part.name, "quantity": qty} for part, qty in obj.direct_parts()]

    def get_hardware(self, obj: Project) -> list[dict[str, Any]]:
        """Return ``{hardware_part, name, quantity}`` dicts for hardware assigned to ``obj``."""
        return [
            {"hardware_part": hw.hardware_part_id, "name": hw.hardware_part.name, "quantity": hw.quantity}
            for hw in obj.hardware_assignments.select_related("hardware_part").all()
        ]

    def get_components(self, obj: Project) -> list[dict[str, Any]]:
        """Return ``{child, quantity}`` dicts, recursing into each child module."""
        path: set[int] = set(self.context.get("_path", set()))
        if obj.pk in path:
            return []
        child_context = dict(self.context)
        child_context["_path"] = path | {obj.pk}
        return [
            {"child": ProjectTreeSerializer(child, context=child_context).data, "quantity": qty}
            for child, qty in obj.child_modules()
        ]
