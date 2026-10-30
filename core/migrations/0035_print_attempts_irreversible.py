"""Stop a rollback below the print-attempt migrations with a clear message.

Reversing ``0034`` would re-add ``PrintQueue.status`` with its ``waiting``
default and the old "one waiting entry per plate" constraint: plates with
several attempts abort the rollback halfway, and a single kept attempt would
come back as a waiting entry the old code prints again.  The print history
cannot be mapped back to the old model, so rolling back requires restoring the
pre-deploy database backup instead.  A database without started or resolved
attempts (e.g. a fresh test database) can still be rolled back.
"""

from django.db import migrations
from django.db.migrations.exceptions import IrreversibleError


def refuse_rollback(apps, schema_editor) -> None:
    """Refuse reversing the print-attempt migrations while print history exists."""
    PrintQueue = apps.get_model("core", "PrintQueue")
    history = PrintQueue.objects.exclude(result="", started_at__isnull=True, completed_at__isnull=True)
    if not history.exists():
        return
    raise IrreversibleError(
        "The print-attempt migrations (0032-0034) cannot be reversed: the print history has no "
        "representation in the old queue model. Restore the pre-deploy database backup instead."
    )


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0034_drop_queue_status"),
    ]

    operations = [
        migrations.RunPython(migrations.RunPython.noop, refuse_rollback),
    ]
