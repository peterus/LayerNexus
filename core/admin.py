"""Django admin registrations for the core app."""

from collections.abc import Iterable
from typing import Any

from django.contrib import admin
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.db.models import Model, QuerySet
from django.http import HttpRequest

from .models import (
    CostProfile,
    FileVersion,
    HardwarePart,
    HardwareShoppingItem,
    OrcaFilamentProfile,
    OrcaMachineProfile,
    OrcaPrintPreset,
    Part,
    PrinterProfile,
    PrintJob,
    PrintJobPart,
    PrintJobPlate,
    PrintQueue,
    PrintTimeEstimate,
    Project,
    ProjectDocument,
    ProjectHardware,
    ScheduledQueueStart,
    SpoolmanFilamentMapping,
)


class ProjectHardwareInline(admin.TabularInline):
    model = ProjectHardware
    extra = 0
    fields = ("hardware_part", "quantity", "notes")
    autocomplete_fields = ("hardware_part",)


class PrintJobPartInline(admin.TabularInline):
    model = PrintJobPart
    extra = 0


class PrintJobPlateInline(admin.TabularInline):
    model = PrintJobPlate
    extra = 0
    readonly_fields = ("gcode_file", "filament_used_grams", "print_time_estimate")


class _UnfinishedJobDeleteGuardMixin:
    """Refuse admin deletes while unfinished print jobs still contain the affected parts.

    Same rule as the web and API deletes. The blocking jobs are listed as "protected"
    objects on the confirmation page, which disables the delete there; the check runs
    again inside the delete transaction.
    """

    def _blocking_jobs(self, objs: Iterable[Model]) -> QuerySet[PrintJob]:
        """Return the unfinished jobs that block deleting ``objs``."""
        raise NotImplementedError

    def _blocking_messages(self, objs: Iterable[Model]) -> list[str]:
        """Describe each blocking job for the confirmation page."""
        return [
            f"{job} — unfinished print job ({job.get_status_display()}); finish, cancel or remove it first"
            for job in self._blocking_jobs(objs).order_by("-created_at")
        ]

    def get_deleted_objects(self, objs: Iterable[Model], request: HttpRequest) -> tuple[Any, ...]:
        """Add the blocking jobs to the protected objects so the admin refuses the delete."""
        deleted, model_count, perms_needed, protected = super().get_deleted_objects(objs, request)
        return deleted, model_count, perms_needed, [*protected, *self._blocking_messages(objs)]

    def _refuse_if_blocked(self, objs: Iterable[Model]) -> None:
        """Raise :class:`PermissionDenied` if an unfinished job appeared since the confirmation."""
        if self._blocking_jobs(objs).exists():
            raise PermissionDenied("Unfinished print jobs still contain parts that would be deleted.")

    def delete_model(self, request: HttpRequest, obj: Model) -> None:
        """Delete one object after re-checking the guard in the same transaction."""
        with transaction.atomic():
            self._refuse_if_blocked([obj])
            super().delete_model(request, obj)

    def delete_queryset(self, request: HttpRequest, queryset: QuerySet) -> None:
        """Bulk-delete after re-checking the guard in the same transaction."""
        with transaction.atomic():
            self._refuse_if_blocked(queryset)
            super().delete_queryset(request, queryset)


@admin.register(Project)
class ProjectAdmin(_UnfinishedJobDeleteGuardMixin, admin.ModelAdmin):
    list_display = ("name", "parent", "quantity", "created_by", "created_at", "updated_at")
    list_filter = ("parent", "created_by", "created_at")
    search_fields = ("name", "description")
    inlines = [ProjectHardwareInline]

    def _blocking_jobs(self, objs: Iterable[Project]) -> QuerySet[PrintJob]:
        """Unfinished jobs containing a part of any of the projects' subtrees."""
        # One breadth-first walk over all selected roots (one query per tree level).
        ids: set[int] = {project.pk for project in objs}
        frontier = set(ids)
        while frontier:
            frontier = set(Project.objects.filter(parent_id__in=frontier).values_list("pk", flat=True)) - ids
            ids |= frontier
        return (
            PrintJob.objects.filter(job_parts__part__project_id__in=ids)
            .exclude(status__in=PrintJob.TERMINAL_STATUSES)
            .distinct()
        )


@admin.register(Part)
class PartAdmin(_UnfinishedJobDeleteGuardMixin, admin.ModelAdmin):
    list_display = ("name", "project", "quantity", "color", "material", "created_at")
    list_filter = ("color", "material")
    search_fields = ("name",)

    @staticmethod
    def _part_ids(objs: Iterable[Part]) -> QuerySet | list[int]:
        """Return a subquery for a queryset (bulk action), else the PKs (one bind parameter each)."""
        return objs.values("pk") if isinstance(objs, QuerySet) else [part.pk for part in objs]

    def _blocking_jobs(self, objs: Iterable[Part]) -> QuerySet[PrintJob]:
        """Unfinished jobs containing any of the parts."""
        return (
            PrintJob.objects.filter(job_parts__part__in=self._part_ids(objs))
            .exclude(status__in=PrintJob.TERMINAL_STATUSES)
            .distinct()
        )


@admin.register(PrinterProfile)
class PrinterProfileAdmin(admin.ModelAdmin):
    list_display = ("name", "orca_machine_profile", "created_by")
    list_filter = ("created_by",)
    search_fields = ("name", "description")


@admin.register(PrintJob)
class PrintJobAdmin(admin.ModelAdmin):
    list_display = (
        "name",
        "machine_profile",
        "printer",
        "status",
        "total_part_count",
        "plate_count",
        "created_at",
    )
    list_filter = ("status", "machine_profile", "printer", "created_at")
    search_fields = ("name",)
    inlines = [PrintJobPartInline, PrintJobPlateInline]


@admin.register(PrintJobPart)
class PrintJobPartAdmin(admin.ModelAdmin):
    list_display = ("print_job", "part", "quantity")
    list_filter = ("print_job",)


@admin.register(PrintJobPlate)
class PrintJobPlateAdmin(admin.ModelAdmin):
    list_display = (
        "print_job",
        "plate_number",
        "status",
        "filament_used_grams",
        "print_time_estimate",
    )
    list_filter = ("status", "print_job")


@admin.register(OrcaMachineProfile)
class OrcaMachineProfileAdmin(admin.ModelAdmin):
    list_display = (
        "name",
        "orca_name",
        "state",
        "first_nozzle_diameter",
        "created_by",
    )
    list_filter = ("state", "created_by")
    search_fields = ("name", "orca_name", "description")
    readonly_fields = (
        "state",
        "uploaded_json",
        "settings",
        "created_at",
        "updated_at",
    )


@admin.register(OrcaFilamentProfile)
class OrcaFilamentProfileAdmin(admin.ModelAdmin):
    list_display = (
        "name",
        "orca_name",
        "state",
        "first_filament_type",
        "first_nozzle_temperature",
        "first_bed_temperature",
        "created_by",
    )
    list_filter = ("state", "created_by")
    search_fields = ("name", "orca_name", "description")
    readonly_fields = (
        "state",
        "uploaded_json",
        "settings",
        "created_at",
        "updated_at",
    )


@admin.register(OrcaPrintPreset)
class OrcaPrintPresetAdmin(admin.ModelAdmin):
    list_display = (
        "name",
        "orca_name",
        "state",
        "infill_density_display",
        "supports_enabled",
        "created_by",
    )
    list_filter = ("state", "created_by")
    search_fields = ("name", "orca_name", "description")
    readonly_fields = (
        "state",
        "uploaded_json",
        "settings",
        "created_at",
        "updated_at",
    )


@admin.register(CostProfile)
class CostProfileAdmin(admin.ModelAdmin):
    list_display = ("printer", "electricity_cost_per_kwh", "printer_power_watts")


@admin.register(PrintTimeEstimate)
class PrintTimeEstimateAdmin(admin.ModelAdmin):
    list_display = ("part", "printer", "estimated_time", "actual_time", "created_at")
    list_filter = ("printer",)


@admin.register(PrintQueue)
class PrintQueueAdmin(admin.ModelAdmin):
    list_display = (
        "plate",
        "printer",
        "state_display",
        "priority",
        "position",
        "is_retry",
        "started_at",
        "completed_at",
        "resolved_at",
        "added_at",
    )
    list_filter = ("printer", "priority", "result", "is_retry")
    ordering = ["-priority", "position"]

    @admin.display(description="State")
    def state_display(self, obj: PrintQueue) -> str:
        """Return the derived attempt state."""
        return obj.get_state_display()

    def has_delete_permission(self, request: HttpRequest, obj: PrintQueue | None = None) -> bool:
        """Refuse deleting started or resolved attempts — they are print history."""
        if obj is not None and (obj.result or obj.started_at is not None or obj.completed_at is not None):
            return False
        return super().has_delete_permission(request, obj)

    def delete_queryset(self, request: HttpRequest, queryset: QuerySet) -> None:
        """Bulk-delete only never-started attempts; refuse if the selection contains history."""
        with transaction.atomic():
            if queryset.exclude(result="", started_at__isnull=True, completed_at__isnull=True).exists():
                raise PermissionDenied("Started or resolved print attempts are print history and cannot be deleted.")
            super().delete_queryset(request, queryset)


@admin.register(ScheduledQueueStart)
class ScheduledQueueStartAdmin(admin.ModelAdmin):
    list_display = ("printer", "start_at", "status", "executed_at", "created_by")
    list_filter = ("printer", "status")


@admin.register(FileVersion)
class FileVersionAdmin(admin.ModelAdmin):
    list_display = (
        "part",
        "version",
        "file_type",
        "file_size",
        "uploaded_by",
        "created_at",
    )
    list_filter = ("file_type",)


@admin.register(SpoolmanFilamentMapping)
class SpoolmanFilamentMappingAdmin(admin.ModelAdmin):
    list_display = (
        "spoolman_filament_id",
        "spoolman_filament_name",
        "orca_filament_profile",
        "created_by",
    )
    list_filter = ("created_by",)
    search_fields = ("spoolman_filament_name",)


@admin.register(ProjectDocument)
class ProjectDocumentAdmin(admin.ModelAdmin):
    list_display = ("name", "project", "uploaded_by", "created_at")
    list_filter = ("created_at",)
    search_fields = ("name", "project__name")


@admin.register(HardwarePart)
class HardwarePartAdmin(admin.ModelAdmin):
    list_display = ("name", "category", "unit_price", "url", "created_by")
    list_filter = ("category", "created_by")
    search_fields = ("name",)


@admin.register(ProjectHardware)
class ProjectHardwareAdmin(admin.ModelAdmin):
    list_display = ("project", "hardware_part", "quantity")
    list_filter = ("project",)
    search_fields = ("hardware_part__name", "project__name")


@admin.register(HardwareShoppingItem)
class HardwareShoppingItemAdmin(admin.ModelAdmin):
    list_select_related = ["project", "hardware_part"]
    list_display = ["project", "hardware_part", "status", "quantity", "updated_at"]
    list_filter = ["status"]
