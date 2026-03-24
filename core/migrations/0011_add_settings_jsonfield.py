"""Add settings JSONField to OrcaSlicer profile models."""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0010_add_project_image"),
    ]

    operations = [
        migrations.AddField(
            model_name="orcamachineprofile",
            name="settings",
            field=models.JSONField(
                blank=True,
                default=dict,
                help_text="All resolved settings",
            ),
        ),
        migrations.AddField(
            model_name="orcafilamentprofile",
            name="settings",
            field=models.JSONField(
                blank=True,
                default=dict,
                help_text="All resolved settings",
            ),
        ),
        migrations.AddField(
            model_name="orcaprintpreset",
            name="settings",
            field=models.JSONField(
                blank=True,
                default=dict,
                help_text="All resolved settings",
            ),
        ),
    ]
