"""File duplication helpers for deep-copying projects and parts."""

from __future__ import annotations

import logging
from pathlib import PurePosixPath

from django.core.files import File
from django.core.files.storage import Storage
from django.db.models.fields.files import FieldFile

logger = logging.getLogger(__name__)

__all__ = ["CopiedFiles", "duplicate_field_file"]


class CopiedFiles:
    """Tracks the files written during one deep copy so a failure can remove them."""

    def __init__(self) -> None:
        """Start with no recorded files."""
        self._files: list[tuple[Storage, str]] = []

    def add(self, storage: Storage, name: str) -> None:
        """Record a newly written file.

        Args:
            storage: Storage backend the file was saved to.
            name: Storage name of the new file.
        """
        self._files.append((storage, name))

    def delete_all(self) -> None:
        """Delete every recorded file, ignoring files that are already gone."""
        for storage, name in self._files:
            try:
                storage.delete(name)
            except OSError:
                logger.warning("deep_copy: could not remove %s during cleanup", name)
        self._files.clear()


def duplicate_field_file(field_file: FieldFile, tracker: CopiedFiles) -> str | None:
    """Copy the file behind ``field_file`` to a new, unique storage name.

    ``Storage.save`` resolves name clashes via ``get_available_name``, so the copy
    never shares a path with the original.

    Args:
        field_file: The source file field value (may be empty).
        tracker: Records the new file for cleanup if the surrounding copy fails.

    Returns:
        The new storage name, or ``None`` if the source is empty or missing on disk.

    Raises:
        OSError: If the copy cannot be written (only a *missing source* is tolerated).
    """
    if not field_file:
        return None
    storage = field_file.storage
    name = field_file.name
    try:
        source = storage.open(name, "rb")
    except FileNotFoundError:
        logger.warning("deep_copy: missing file %s", name)
        return None
    # Write errors (disk full, permissions) propagate so the whole copy fails and cleans up.
    with source as fh:
        new_name = storage.save(
            field_file.field.generate_filename(None, PurePosixPath(name).name),
            File(fh),
            max_length=field_file.field.max_length,
        )
    tracker.add(storage, new_name)
    return new_name
