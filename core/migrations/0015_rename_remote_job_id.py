from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0014_alter_orcamachineprofile_orca_name_and_more"),
    ]

    operations = [
        migrations.RenameField(
            model_name="printjobplate",
            old_name="klipper_job_id",
            new_name="remote_job_id",
        ),
        migrations.AlterField(
            model_name="printjobplate",
            name="remote_job_id",
            field=models.CharField(
                blank=True,
                help_text="Filename or job ID on the remote printer after upload",
                max_length=255,
            ),
        ),
    ]
