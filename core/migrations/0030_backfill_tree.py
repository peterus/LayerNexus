"""Backfill the strict project tree from the composition edges.

Copies every ``ProjectPart`` edge onto ``Part.project`` / ``Part.quantity`` and every
``ProjectComponent`` edge onto ``Project.parent`` / ``Project.quantity``. A project without
a parent edge becomes a root (its unreliable legacy ``parent`` and ``quantity`` are reset).

The migration aborts with a list of offending rows when the edges do not form a tree:
a part linked into several projects, a project with several parents, a part that
belongs to no project, or a parent cycle. Roots without a ``default_print_preset`` are only reported.

The logic is self-contained and uses historical models only, so later changes to the
runtime models can never break replaying it. ``.update()`` is used throughout so no
runtime ``save()`` side effects run.
"""

import sys

from django.db import migrations
from django.db.models import Count


def collect_tree_conflicts(part_model, part_link_model, component_model) -> list[str]:
    """Return a human-readable list of rows that prevent a strict tree.

    Reports parts in several projects, projects with several parents, parts in no
    project, and parent cycles (which would leave a tree without a root).

    Args:
        part_model: Historical ``Part`` model.
        part_link_model: Historical ``ProjectPart`` model.
        component_model: Historical ``ProjectComponent`` model.

    Returns:
        One message per conflict; empty when the edges form a tree.
    """
    problems: list[str] = []
    for row in part_link_model.objects.values("part_id").annotate(n=Count("id")).filter(n__gt=1):
        ids = list(part_link_model.objects.filter(part_id=row["part_id"]).values_list("project_id", flat=True))
        problems.append(f"Part {row['part_id']} is in {row['n']} projects: {ids}")
    for row in component_model.objects.values("child_project_id").annotate(n=Count("id")).filter(n__gt=1):
        ids = list(
            component_model.objects.filter(child_project_id=row["child_project_id"]).values_list(
                "parent_project_id", flat=True
            )
        )
        problems.append(f"Project {row['child_project_id']} has {row['n']} parents: {ids}")
    linked = part_link_model.objects.values("part_id")
    for part in part_model.objects.exclude(pk__in=linked).only("pk", "name"):
        problems.append(f"Part {part.pk} '{part.name}' belongs to no project")
    parent_of = dict(component_model.objects.values_list("child_project_id", "parent_project_id"))
    in_cycle: set[int] = set()
    for start in parent_of:
        seen: set[int] = set()
        node = start
        while node in parent_of and node not in seen:
            seen.add(node)
            node = parent_of[node]
        if node == start and start not in in_cycle:
            in_cycle |= seen
            problems.append(f"Project {start} is in a parent cycle: {sorted(seen)}")
    return problems


def forwards(apps, schema_editor) -> None:
    """Write the single parent edge of every node onto its tree fields.

    Raises:
        RuntimeError: When the composition edges do not form a strict tree.
    """
    Project, Part = apps.get_model("core", "Project"), apps.get_model("core", "Part")
    ProjectPart, ProjectComponent = apps.get_model("core", "ProjectPart"), apps.get_model("core", "ProjectComponent")
    problems = collect_tree_conflicts(Part, ProjectPart, ProjectComponent)
    if problems:
        details = "\n".join(problems)
        raise RuntimeError(f"Tree migration aborted — shared data must be resolved first:\n{details}")
    for link in ProjectPart.objects.all().iterator():
        Part.objects.filter(pk=link.part_id).update(project_id=link.project_id, quantity=link.quantity)
    edges = {e.child_project_id: e for e in ProjectComponent.objects.all()}
    for project_id in Project.objects.values_list("pk", flat=True):
        edge = edges.get(project_id)
        if edge is None:
            # Overwrite the unreliable legacy parent and its quantity (roots ignore quantity).
            Project.objects.filter(pk=project_id).update(parent_id=None, quantity=1)
        else:
            Project.objects.filter(pk=project_id).update(parent_id=edge.parent_project_id, quantity=edge.quantity)
    for root in Project.objects.filter(parent__isnull=True, default_print_preset__isnull=True):
        sys.stdout.write(f"WARNING: root project {root.pk} '{root.name}' has no default_print_preset\n")


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0029_part_project_quantity"),
    ]

    operations = [
        migrations.RunPython(forwards, migrations.RunPython.noop),
    ]
