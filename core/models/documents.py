"""FileVersion and ProjectDocument models for the LayerNexus application."""

from __future__ import annotations

from django.conf import settings
from django.db import models


class FileVersion(models.Model):
    """Version tracking for STL and G-code files attached to parts."""

    part = models.ForeignKey("core.Part", on_delete=models.CASCADE, related_name="file_versions")
    version = models.PositiveIntegerField(default=1)
    file = models.FileField(upload_to="file_versions/")
    file_type = models.CharField(max_length=10, choices=[("stl", "STL"), ("gcode", "G-code")])
    file_hash = models.CharField(max_length=64, blank=True, help_text="SHA-256 hash of the file")
    file_size = models.PositiveIntegerField(default=0, help_text="File size in bytes")
    uploaded_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True)
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-version"]
        unique_together = ["part", "version", "file_type"]

    def __str__(self) -> str:
        return f"{self.part.name} v{self.version} ({self.file_type})"


class ProjectDocument(models.Model):
    """A file attachment (PDF, TXT, image, CAD, etc.) linked to a project."""

    project = models.ForeignKey(
        "core.Project",
        on_delete=models.CASCADE,
        related_name="documents",
    )
    name = models.CharField(max_length=255, blank=True)
    file = models.FileField(upload_to="project_documents/")
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return self.name

    @property
    def file_extension(self) -> str:
        """Return the lowercase file extension without the leading dot."""
        import os

        _, ext = os.path.splitext(self.file.name)
        return ext.lstrip(".").lower()
