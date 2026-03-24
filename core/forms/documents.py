"""Project document forms."""

from typing import Any

from django import forms
from django.core.files.uploadedfile import UploadedFile

from core.models import ProjectDocument

__all__ = [
    "ProjectDocumentForm",
]

ALLOWED_DOCUMENT_EXTENSIONS = {
    ".pdf",
    ".txt",
    ".md",
    ".png",
    ".jpg",
    ".jpeg",
    ".svg",
    ".step",
    ".dxf",
}

MAX_DOCUMENT_SIZE = 75 * 1024 * 1024  # 75 MB


class ProjectDocumentForm(forms.ModelForm):
    """Form for uploading a document to a project."""

    class Meta:
        model = ProjectDocument
        fields = ["name", "file"]

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.fields["name"].required = False

    def clean_file(self) -> UploadedFile | None:
        """Validate uploaded document for extension and size.

        Returns:
            The validated file.

        Raises:
            forms.ValidationError: If extension is not allowed or size exceeds 75 MB.
        """
        uploaded = self.cleaned_data.get("file")
        if uploaded:
            import os

            _, ext = os.path.splitext(uploaded.name)
            if ext.lower() not in ALLOWED_DOCUMENT_EXTENSIONS:
                allowed = ", ".join(sorted(ALLOWED_DOCUMENT_EXTENSIONS))
                raise forms.ValidationError(f"File type '{ext}' is not allowed. Allowed types: {allowed}")
            if uploaded.size > MAX_DOCUMENT_SIZE:
                raise forms.ValidationError("File size must be under 75 MB.")
        return uploaded

    def clean(self) -> dict:
        """Auto-fill name from the uploaded filename if left empty."""
        cleaned_data = super().clean()
        name = cleaned_data.get("name", "").strip()
        if not name:
            uploaded = cleaned_data.get("file")
            if uploaded and hasattr(uploaded, "name"):
                from pathlib import PurePosixPath

                cleaned_data["name"] = PurePosixPath(uploaded.name).stem
            else:
                self.add_error("name", "Name is required when no file is uploaded.")
        return cleaned_data
