"""Backfill composition edges from the legacy Part.project / Project.parent FKs."""

from django.db import migrations

from core.models.composition import rebuild_composition_edges


def forwards(apps, schema_editor):
    """Create edges mirroring the current FK graph."""
    rebuild_composition_edges(
        apps.get_model("core", "Project"),
        apps.get_model("core", "Part"),
        apps.get_model("core", "ProjectComponent"),
        apps.get_model("core", "ProjectPart"),
    )


def backwards(apps, schema_editor):
    """Remove all composition edges (the FKs remain the source of truth)."""
    apps.get_model("core", "ProjectPart").objects.all().delete()
    apps.get_model("core", "ProjectComponent").objects.all().delete()


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0018_projectcomponent_projectpart"),
    ]
    operations = [
        migrations.RunPython(forwards, backwards),
    ]
