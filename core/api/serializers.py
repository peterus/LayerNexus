"""DRF serializers for the iterative build API.

Projects form a strict tree: a sub-project has a ``parent`` and a ``quantity`` within
it, and every part belongs to exactly one ``project`` with its own ``quantity``. The
print preset is required on top-level projects and inherited bottom-up otherwise.
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
    ProjectDocument,
    ProjectHardware,
    SpoolmanFilamentMapping,
)


class ProjectSerializer(serializers.ModelSerializer):
    """Serialize a project, including its place in the tree.

    ``parent`` is ``null`` for top-level projects; ``quantity`` is how many of this
    sub-project the parent needs (ignored on top-level projects). ``default_print_preset``
    is required on top-level projects and optional (inherited) on sub-projects.
    """

    parent = serializers.PrimaryKeyRelatedField(queryset=Project.objects.all(), allow_null=True, required=False)
    default_print_preset = serializers.PrimaryKeyRelatedField(
        queryset=OrcaPrintPreset.objects.all(),
        allow_null=True,
        required=False,
    )

    class Meta:
        model = Project
        fields = [
            "id",
            "name",
            "description",
            "parent",
            "quantity",
            "default_print_preset",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "created_at", "updated_at"]

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        """Require a preset on top-level projects and reject cyclic re-parenting.

        Args:
            attrs: The validated field values (partial on ``PATCH``).

        Returns:
            The unchanged ``attrs``.

        Raises:
            serializers.ValidationError: When a top-level project would have no
                preset, or the new parent lies in the project's own subtree.
        """
        parent = attrs["parent"] if "parent" in attrs else getattr(self.instance, "parent", None)
        preset = (
            attrs["default_print_preset"]
            if "default_print_preset" in attrs
            else getattr(self.instance, "default_print_preset", None)
        )
        if parent is None and preset is None:
            raise serializers.ValidationError({"default_print_preset": ["Required for top-level projects."]})
        if self.instance is not None and parent is not None and parent.pk in self.instance.subtree_ids():
            raise serializers.ValidationError(
                {"parent": ["A project cannot be a sub-project of itself or one of its descendants."]}
            )
        return attrs


class PartSerializer(serializers.ModelSerializer):
    """Serialize a part's writable scalar fields plus read-only estimation results.

    ``project`` is required: every part belongs to exactly one project. ``stl_file`` is
    read-only here — it is uploaded through the dedicated multipart
    ``POST /parts/{id}/stl/`` endpoint so ordinary create/update stays JSON.
    """

    class Meta:
        model = Part
        fields = [
            "id",
            "project",
            "name",
            "quantity",
            "stl_file",
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
    """Serialize a hardware assignment (project supplied by the URL)."""

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
    """Nested read-only view of a project: its parts, hardware and sub-projects (recursive)."""

    parts = serializers.SerializerMethodField()
    subprojects = serializers.SerializerMethodField()
    hardware = serializers.SerializerMethodField()

    class Meta:
        model = Project
        fields = ["id", "name", "description", "quantity", "parts", "subprojects", "hardware"]

    def get_parts(self, obj: Project) -> list[dict[str, Any]]:
        """Return ``{part, name, quantity}`` dicts for the parts directly in ``obj``."""
        return [{"part": part.pk, "name": part.name, "quantity": part.quantity} for part in obj.parts.all()]

    def get_hardware(self, obj: Project) -> list[dict[str, Any]]:
        """Return ``{hardware_part, name, quantity}`` dicts for hardware assigned to ``obj``."""
        return [
            {"hardware_part": hw.hardware_part_id, "name": hw.hardware_part.name, "quantity": hw.quantity}
            for hw in obj.hardware_assignments.select_related("hardware_part").all()
        ]

    def get_subprojects(self, obj: Project) -> list[dict[str, Any]]:
        """Return the nested tree of each direct sub-project."""
        return [ProjectTreeSerializer(sub, context=self.context).data for sub in obj.subprojects.all()]
