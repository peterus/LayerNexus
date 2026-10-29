"""Remove media files from storage together with the rows that reference them.

When a row in :data:`MEDIA_FIELDS` is deleted (also through CASCADE) or one of these
files is replaced or cleared on save, the old file is removed **after the transaction
commits**, and only if no row in any of these fields still references the same storage
name (a ``FileVersion`` may share its part's path). A rolled-back transaction keeps its
files. Print-job files (G-code, plate thumbnails) are job history and are not covered.

Files that were left behind anyway (older deletes, crashes, a failing deep-copy save)
are found by ``manage.py cleanup_orphan_media``.
"""

from __future__ import annotations

import logging
from functools import partial
from typing import Any

from django.core.files.storage import Storage
from django.db import transaction
from django.db.models import Model
from django.db.models.signals import post_delete, post_save, pre_save

from core.models import FileVersion, Part, Project, ProjectDocument

logger = logging.getLogger(__name__)

__all__ = ["MEDIA_FIELDS", "file_is_referenced", "referenced_names"]

#: Every (model, file field) whose files are removed with their rows.
MEDIA_FIELDS: list[tuple[type[Model], str]] = [
    (Part, "stl_file"),
    (FileVersion, "file"),
    (ProjectDocument, "file"),
    (Project, "image"),
]

#: Instance attribute that carries the replaced file names from ``pre_save`` to ``post_save``.
_OLD_NAMES_ATTR = "_media_replaced_names"


def file_is_referenced(name: str) -> bool:
    """Return whether any row in :data:`MEDIA_FIELDS` still stores ``name``.

    Args:
        name: Storage name (path relative to the storage root).

    Returns:
        ``True`` if at least one row references the file.
    """
    return any(model.objects.filter(**{field: name}).exists() for model, field in MEDIA_FIELDS)


def referenced_names() -> set[str]:
    """Return every storage name referenced by a row in :data:`MEDIA_FIELDS`."""
    names: set[str] = set()
    for model, field in MEDIA_FIELDS:
        names.update(
            model.objects.exclude(**{f"{field}__isnull": True}).exclude(**{field: ""}).values_list(field, flat=True)
        )
    return names


def _delete_if_unreferenced(storage: Storage, name: str) -> None:
    """Delete ``name`` from ``storage`` unless a row still references it.

    Storage errors are logged, never raised: the row is already gone and the file is
    picked up by ``cleanup_orphan_media`` later.
    """
    if file_is_referenced(name):
        return
    try:
        storage.delete(name)
    except OSError:
        logger.warning("Could not delete media file %s", name, exc_info=True)


def _schedule_delete(storage: Storage, name: str) -> None:
    """Delete ``name`` after the current transaction commits (dropped on rollback)."""
    transaction.on_commit(partial(_delete_if_unreferenced, storage, name))


def _on_delete(sender: type[Model], instance: Model, *, field: str, **kwargs: Any) -> None:
    """Schedule removal of a deleted row's file."""
    file = getattr(instance, field)
    if file:
        _schedule_delete(file.storage, file.name)


def _remember_replaced(sender: type[Model], instance: Model, *, field: str, **kwargs: Any) -> None:
    """Before an update, remember the stored file name if the field now holds another file."""
    if kwargs.get("raw") or instance.pk is None:
        return
    update_fields = kwargs.get("update_fields")
    if update_fields is not None and field not in update_fields:
        return
    old = sender._default_manager.filter(pk=instance.pk).values_list(field, flat=True).first()
    current = getattr(instance, field)
    if old and old != (current.name if current else None):
        instance.__dict__.setdefault(_OLD_NAMES_ATTR, {})[field] = (current.storage, old)


def _remove_replaced(sender: type[Model], instance: Model, *, field: str, **kwargs: Any) -> None:
    """After the update, schedule removal of the replaced file."""
    replaced = instance.__dict__.get(_OLD_NAMES_ATTR, {}).pop(field, None)
    if replaced is not None:
        _schedule_delete(*replaced)


def connect() -> None:
    """Connect the delete/replace receivers for every model in :data:`MEDIA_FIELDS`."""
    for model, field in MEDIA_FIELDS:
        uid = f"core.media.{model.__name__}.{field}"
        post_delete.connect(partial(_on_delete, field=field), sender=model, weak=False, dispatch_uid=f"{uid}.delete")
        pre_save.connect(partial(_remember_replaced, field=field), sender=model, weak=False, dispatch_uid=f"{uid}.pre")
        post_save.connect(partial(_remove_replaced, field=field), sender=model, weak=False, dispatch_uid=f"{uid}.post")
