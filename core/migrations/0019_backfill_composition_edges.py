"""Backfill composition edges from the legacy Part.project / Project.parent FKs.

The backfill logic is inlined here (rather than importing the runtime
``rebuild_composition_edges`` helper) so this data migration stays frozen and
self-contained: a later phase that rewrites or removes the helper can never break
replaying this migration on a fresh install. It uses only ``apps.get_model()``
historical models.
"""

from django.db import migrations


def forwards(apps, schema_editor):
    """Create one edge per legacy FK relation, copying the node ``quantity``."""
    project_model = apps.get_model("core", "Project")
    part_model = apps.get_model("core", "Part")
    component_model = apps.get_model("core", "ProjectComponent")
    part_link_model = apps.get_model("core", "ProjectPart")

    for part in part_model.objects.filter(project__isnull=False).iterator():
        part_link_model.objects.update_or_create(
            project_id=part.project_id,
            part_id=part.pk,
            defaults={"quantity": part.quantity},
        )
    for child in project_model.objects.filter(parent__isnull=False).iterator():
        component_model.objects.update_or_create(
            parent_project_id=child.parent_id,
            child_project_id=child.pk,
            defaults={"quantity": child.quantity},
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
