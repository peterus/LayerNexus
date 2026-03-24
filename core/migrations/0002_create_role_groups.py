"""Data migration to create role groups and assign permissions.

Creates the three role groups (Admin, Operator, Designer) with the
correct permissions.  This consolidates the original migrations
0023, 0024, 0027, 0028, and 0029 into a single idempotent migration.
"""

from django.apps import apps as global_apps
from django.db import migrations


def _ensure_permissions_exist():
    """Force Django to create all permissions from model Meta.

    In a data migration the ``post_migrate`` signal has not yet fired, so
    custom permissions declared in ``Meta.permissions`` may not exist.
    Calling ``create_permissions`` explicitly fixes this.
    """
    from django.contrib.auth.management import create_permissions

    for app_config in global_apps.get_app_configs():
        create_permissions(app_config, verbosity=0)


def create_groups_and_permissions(apps, schema_editor):
    """Create the three role groups and assign permissions.

    Roles:
        Admin: All permissions (full system access).
        Operator: Printers, profiles, queue control, slicing,
                  filament mappings, read projects/parts.
        Designer: Projects, parts, STL upload, slicing, queue
                  add/remove, read printers/profiles.
    """
    _ensure_permissions_exist()

    Group = apps.get_model("auth", "Group")
    Permission = apps.get_model("auth", "Permission")

    # Create groups
    admin_group, _ = Group.objects.get_or_create(name="Admin")
    operator_group, _ = Group.objects.get_or_create(name="Operator")
    designer_group, _ = Group.objects.get_or_create(name="Designer")

    def get_perm(app_label, codename):
        """Get a permission by app_label and codename, return None if missing."""
        try:
            return Permission.objects.get(
                content_type__app_label=app_label,
                codename=codename,
            )
        except Permission.DoesNotExist:
            return None

    def assign_perms(group, perm_list):
        """Assign a list of (app_label, codename) tuples to a group."""
        for app_label, codename in perm_list:
            perm = get_perm(app_label, codename)
            if perm:
                group.permissions.add(perm)

    # ── Admin gets ALL permissions ──
    admin_group.permissions.set(Permission.objects.all())

    # ── Operator permissions ──
    operator_perms = [
        # Printers: full CRUD
        ("core", "add_printerprofile"),
        ("core", "change_printerprofile"),
        ("core", "delete_printerprofile"),
        ("core", "view_printerprofile"),
        ("core", "can_manage_printers"),
        ("core", "can_control_printer"),
        # OrcaSlicer profiles: full CRUD
        ("core", "add_orcamachineprofile"),
        ("core", "change_orcamachineprofile"),
        ("core", "delete_orcamachineprofile"),
        ("core", "view_orcamachineprofile"),
        ("core", "add_orcafilamentprofile"),
        ("core", "change_orcafilamentprofile"),
        ("core", "delete_orcafilamentprofile"),
        ("core", "view_orcafilamentprofile"),
        ("core", "can_manage_orca_profiles"),
        ("core", "add_orcaprintpreset"),
        ("core", "change_orcaprintpreset"),
        ("core", "delete_orcaprintpreset"),
        ("core", "view_orcaprintpreset"),
        # Filament mappings: full CRUD
        ("core", "add_spoolmanfilamentmapping"),
        ("core", "change_spoolmanfilamentmapping"),
        ("core", "delete_spoolmanfilamentmapping"),
        ("core", "view_spoolmanfilamentmapping"),
        ("core", "can_manage_filament_mappings"),
        # Print queue: full control
        ("core", "add_printqueue"),
        ("core", "change_printqueue"),
        ("core", "delete_printqueue"),
        ("core", "view_printqueue"),
        ("core", "can_manage_print_queue"),
        ("core", "can_dequeue_job"),
        # Print jobs: full CRUD
        ("core", "add_printjob"),
        ("core", "change_printjob"),
        ("core", "delete_printjob"),
        ("core", "view_printjob"),
        # Projects/parts: read only
        ("core", "view_project"),
        ("core", "view_part"),
        # Cost profiles
        ("core", "view_costprofile"),
        ("core", "change_costprofile"),
        ("core", "add_costprofile"),
        # File versions (read)
        ("core", "view_fileversion"),
    ]
    assign_perms(operator_group, operator_perms)

    # ── Designer permissions ──
    designer_perms = [
        # Projects: full CRUD
        ("core", "add_project"),
        ("core", "change_project"),
        ("core", "delete_project"),
        ("core", "view_project"),
        ("core", "can_manage_projects"),
        # Parts: full CRUD
        ("core", "add_part"),
        ("core", "change_part"),
        ("core", "delete_part"),
        ("core", "view_part"),
        # Print jobs: full CRUD
        ("core", "add_printjob"),
        ("core", "change_printjob"),
        ("core", "delete_printjob"),
        ("core", "view_printjob"),
        # Print queue: add + view + dequeue
        ("core", "add_printqueue"),
        ("core", "view_printqueue"),
        ("core", "can_manage_print_queue"),
        ("core", "can_dequeue_job"),
        # Printers/profiles: read only
        ("core", "view_printerprofile"),
        ("core", "view_orcamachineprofile"),
        ("core", "view_orcafilamentprofile"),
        ("core", "view_orcaprintpreset"),
        ("core", "view_spoolmanfilamentmapping"),
        # Cost profiles (read)
        ("core", "view_costprofile"),
        # File versions
        ("core", "add_fileversion"),
        ("core", "view_fileversion"),
    ]
    assign_perms(designer_group, designer_perms)


def remove_groups(apps, schema_editor):
    """Remove the role groups (reverse migration)."""
    Group = apps.get_model("auth", "Group")
    Group.objects.filter(name__in=["Admin", "Operator", "Designer"]).delete()


class Migration(migrations.Migration):
    """Create role-based groups with appropriate permissions."""

    dependencies = [
        ("core", "0001_initial"),
        ("auth", "0012_alter_user_first_name_max_length"),
        ("contenttypes", "0002_remove_content_type_name"),
    ]

    operations = [
        migrations.RunPython(create_groups_and_permissions, remove_groups),
    ]
