"""List or delete media files that no database row references any more.

Scans the upload directories of :data:`core.signals.MEDIA_FIELDS` (part model files,
file versions, project documents, project images). Print-job files are job history
and are never scanned. Dry run by default; ``--delete`` removes the orphans.
"""

from __future__ import annotations

import os
import posixpath
from collections.abc import Iterator
from datetime import timedelta
from typing import Any

from django.core.files.storage import Storage
from django.core.management.base import BaseCommand, CommandParser
from django.utils import timezone

from core.signals import MEDIA_FIELDS, file_is_referenced, referenced_names

#: Default for ``--min-age`` (minutes): younger files may belong to an upload or copy in progress.
DEFAULT_MIN_AGE_MINUTES = 60


def _is_symlink(storage: Storage, name: str) -> bool:
    """Return whether ``name`` is a symlink on a local storage (never for remote storages)."""
    try:
        return os.path.islink(storage.path(name))
    except NotImplementedError:
        return False


def _walk(storage: Storage, directory: str) -> Iterator[str]:
    """Yield the storage name of every file below ``directory`` (recursively).

    Symlinks (files and directories) are skipped, so the scan never leaves the
    upload directories, e.g. into a linked archive outside ``MEDIA_ROOT``.

    Args:
        storage: Storage backend to list.
        directory: Directory name relative to the storage root.

    Yields:
        Storage names such as ``"stl_files/part.stl"``.
    """
    try:
        subdirs, files = storage.listdir(directory)
    except FileNotFoundError:
        return
    for name in files:
        path = posixpath.join(directory, name)
        if not _is_symlink(storage, path):
            yield path
    for subdir in subdirs:
        path = posixpath.join(directory, subdir)
        if not _is_symlink(storage, path):
            yield from _walk(storage, path)


class Command(BaseCommand):
    """Find media files without a referencing row; delete them with ``--delete``."""

    help = (
        "List media files (part models, file versions, project documents, project images) "
        "that no database row references. Dry run by default; use --delete to remove them."
    )

    def add_arguments(self, parser: CommandParser) -> None:
        """Add ``--delete`` and ``--min-age``."""
        parser.add_argument("--delete", action="store_true", help="Delete the orphaned files (default: dry run).")
        parser.add_argument(
            "--min-age",
            type=int,
            default=DEFAULT_MIN_AGE_MINUTES,
            metavar="MINUTES",
            help=(
                "Skip files modified within the last MINUTES minutes; they may belong to an upload "
                f"or copy still in progress (default: {DEFAULT_MIN_AGE_MINUTES})."
            ),
        )

    def handle(self, *args: Any, **options: Any) -> None:
        """Scan the upload directories, then report or delete the orphans."""
        delete: bool = options["delete"]
        cutoff = timezone.now() - timedelta(minutes=max(options["min_age"], 0))
        referenced = referenced_names()

        directories: dict[str, Storage] = {}
        for model, field_name in MEDIA_FIELDS:
            field = model._meta.get_field(field_name)
            directories.setdefault(str(field.upload_to).strip("/"), field.storage)

        orphans: list[tuple[Storage, str, int]] = []
        too_new = 0
        for directory, storage in sorted(directories.items()):
            for name in _walk(storage, directory):
                if name in referenced:
                    continue
                try:
                    modified, size = storage.get_modified_time(name), storage.size(name)
                except FileNotFoundError:
                    continue  # removed while scanning
                if modified > cutoff:
                    too_new += 1
                    continue
                orphans.append((storage, name, size))

        for _storage, name, _size in orphans:
            self.stdout.write(name)
        total_mb = sum(size for _storage, _name, size in orphans) / (1024 * 1024)
        self.stdout.write(f"{len(orphans)} orphaned files, {total_mb:.1f} MB")
        if too_new:
            self.stdout.write(f"Skipped {too_new} newer file(s) (younger than --min-age).")

        if not delete:
            if orphans:
                self.stdout.write("Dry run — nothing deleted. Re-run with --delete to remove them.")
            return

        deleted = 0
        for storage, name, _size in orphans:
            # Re-check: a row may have started to reference the file since the scan.
            if file_is_referenced(name):
                continue
            try:
                storage.delete(name)
            except OSError as exc:
                self.stderr.write(f"Could not delete {name}: {exc}")
                continue
            deleted += 1
        self.stdout.write(self.style.SUCCESS(f"Deleted {deleted} orphaned files."))
