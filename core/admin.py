"""Django admin registrations for the core app."""

from collections.abc import Iterable
from typing import Any

from django import forms
from django.contrib import admin
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.db.models import Model, Q, QuerySet
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


def _contents_frozen(job: PrintJob) -> bool:
    """Return whether the admin must not change the job's parts / plates / machine profile.

    Only drafts are edited in the admin: a sliced job's plates would go stale
    (the web UI discards them via ``PrintJob.discard_slicing``), and a job with
    print attempts is print history.
    """
    return job.status != PrintJob.STATUS_DRAFT or job.print_started


class PrintJobPartInline(admin.TabularInline):
    """Parts of a job; editable only while the job is a draft (see :func:`_contents_frozen`)."""

    model = PrintJobPart
    extra = 0

    def has_add_permission(self, request: HttpRequest, obj: PrintJob | None = None) -> bool:
        """Refuse adding parts to a job that is not a draft."""
        if obj is not None and _contents_frozen(obj):
            return False
        return super().has_add_permission(request, obj)

    def has_change_permission(self, request: HttpRequest, obj: PrintJob | None = None) -> bool:
        """Refuse changing the parts of a job that is not a draft."""
        if obj is not None and _contents_frozen(obj):
            return False
        return super().has_change_permission(request, obj)

    def has_delete_permission(self, request: HttpRequest, obj: PrintJob | None = None) -> bool:
        """Refuse removing parts from a job that is not a draft."""
        if obj is not None and _contents_frozen(obj):
            return False
        return super().has_delete_permission(request, obj)


class PrintJobPlateInline(admin.TabularInline):
    model = PrintJobPlate
    extra = 0
    readonly_fields = ("gcode_file", "filament_used_grams", "print_time_estimate")

    def has_add_permission(self, request: HttpRequest, obj: PrintJob | None = None) -> bool:
        """Refuse adding plates by hand: plates come from the slicer only."""
        return False

    def has_delete_permission(self, request: HttpRequest, obj: PrintJob | None = None) -> bool:
        """Refuse deleting plates of a job with print attempts (the cascade would delete them)."""
        if obj is not None and obj.print_started:
            return False
        return super().has_delete_permission(request, obj)


class _PrintHistoryDeleteGuardMixin:
    """Refuse admin deletes of objects whose cascade would delete print history.

    Subclasses implement :meth:`_history_attempts` for the objects to delete.
    """

    def _history_attempts(self, objs: Iterable[Model]) -> QuerySet[PrintQueue]:
        raise NotImplementedError

    def has_delete_permission(self, request: HttpRequest, obj: Model | None = None) -> bool:
        """Hide the delete action for an object with print history."""
        if obj is not None and self._history_attempts([obj]).exists():
            return False
        return super().has_delete_permission(request, obj)

    def delete_model(self, request: HttpRequest, obj: Model) -> None:
        """Delete one object after re-checking the guard in the same transaction."""
        with transaction.atomic():
            if self._history_attempts([obj]).exists():
                raise PermissionDenied("This object has print history and cannot be deleted.")
            super().delete_model(request, obj)

    def delete_queryset(self, request: HttpRequest, queryset: QuerySet) -> None:
        """Bulk-delete after re-checking the guard in the same transaction."""
        with transaction.atomic():
            if self._history_attempts(queryset).exists():
                raise PermissionDenied("The selection contains print history and cannot be deleted.")
            super().delete_queryset(request, queryset)


class _UnfinishedJobDeleteGuardMixin:
    """Refuse admin deletes while print jobs still contain the affected parts.

    Same rule as the web and API deletes: an unfinished job blocks the delete, and
    a job with print attempts is print history that must be kept (every finished
    job has attempts, so any job containing the parts blocks).  The blocking jobs
    are listed as "protected" objects on the confirmation page, which disables the
    delete there; the check runs again inside the delete transaction.
    """

    def _blocking_jobs(self, objs: Iterable[Model]) -> QuerySet[PrintJob]:
        """Return the jobs (unfinished or with print history) that block deleting ``objs``."""
        raise NotImplementedError

    def _blocking_messages(self, objs: Iterable[Model]) -> list[str]:
        """Describe each blocking job for the confirmation page."""
        messages = []
        for job in self._blocking_jobs(objs).with_display_status().order_by("-created_at"):
            if job.print_started:
                reason = f"has print history ({job.get_display_status_display()}), which must be kept"
            else:
                reason = f"unfinished print job ({job.get_display_status_display()}); finish or remove it first"
            messages.append(f"{job} — {reason}")
        return messages

    def get_deleted_objects(self, objs: Iterable[Model], request: HttpRequest) -> tuple[Any, ...]:
        """Add the blocking jobs to the protected objects so the admin refuses the delete."""
        deleted, model_count, perms_needed, protected = super().get_deleted_objects(objs, request)
        blocking = self._blocking_messages(objs)
        if blocking:
            # The blocking jobs explain the refusal; a "missing permission" for their frozen job
            # entries would hide that reason (the admin shows perms_needed before protected).
            perms_needed = set()
        return deleted, model_count, perms_needed, [*protected, *blocking]

    def _refuse_if_blocked(self, objs: Iterable[Model]) -> None:
        """Raise :class:`PermissionDenied` if an unfinished job appeared since the confirmation."""
        if self._blocking_jobs(objs).exists():
            raise PermissionDenied(
                "Print jobs (unfinished or print history) still contain parts that would be deleted."
            )

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
        """Jobs containing a part of any of the projects' subtrees."""
        # One breadth-first walk over all selected roots (one query per tree level).
        ids: set[int] = {project.pk for project in objs}
        frontier = set(ids)
        while frontier:
            frontier = set(Project.objects.filter(parent_id__in=frontier).values_list("pk", flat=True)) - ids
            ids |= frontier
        return PrintJob.objects.filter(
            pk__in=PrintJobPart.objects.filter(part__project_id__in=ids).values("print_job_id")
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
        """Jobs containing any of the parts."""
        return PrintJob.objects.filter(
            pk__in=PrintJobPart.objects.filter(part__in=self._part_ids(objs)).values("print_job_id")
        )


@admin.register(PrinterProfile)
class PrinterProfileAdmin(admin.ModelAdmin):
    list_display = ("name", "orca_machine_profile", "created_by")
    list_filter = ("created_by",)
    search_fields = ("name", "description")


@admin.register(PrintJob)
class PrintJobAdmin(_PrintHistoryDeleteGuardMixin, admin.ModelAdmin):
    list_display = (
        "name",
        "machine_profile",
        "status",
        "display_status_label",
        "total_part_count",
        "plate_count",
        "created_at",
    )
    list_filter = ("status", "machine_profile", "created_at")
    search_fields = ("name",)
    inlines = [PrintJobPartInline, PrintJobPlateInline]

    def get_queryset(self, request: HttpRequest) -> QuerySet[PrintJob]:
        """Annotate the derived display status for the change list."""
        return super().get_queryset(request).with_display_status()

    @admin.display(description="Print status")
    def display_status_label(self, obj: PrintJob) -> str:
        """Return the derived display status."""
        return obj.get_display_status_display()

    def get_readonly_fields(self, request: HttpRequest, obj: PrintJob | None = None) -> tuple[str, ...]:
        """Freeze the machine profile unless the job is a draft, and the slicing status once printed.

        A changed machine profile would leave the plates of a sliced job stale. The
        slicing status is a slicer fact; editing it on a printed job could make its
        plates unqueueable or (``pending``) let the slicer replace them.
        """
        readonly = tuple(super().get_readonly_fields(request, obj))
        if obj is not None and _contents_frozen(obj):
            readonly += ("machine_profile",)
        if obj is not None and obj.print_started:
            readonly += ("status",)
        return readonly

    def _history_attempts(self, objs: Iterable[PrintJob]) -> QuerySet[PrintQueue]:
        # Any attempt counts: a job with print attempts is not deletable (same as the web delete).
        return PrintQueue.objects.filter(plate__print_job__in=[o.pk for o in objs])


class PrintJobPartAdminForm(forms.ModelForm):
    """Refuse putting an entry into a job that has print attempts (add or re-point)."""

    class Meta:
        model = PrintJobPart
        fields = ["print_job", "part", "quantity"]

    def clean_print_job(self) -> PrintJob:
        """Reject a target job that is not a draft (stale plates) or has print attempts (history)."""
        job = self.cleaned_data["print_job"]
        if job is not None and _contents_frozen(job):
            raise forms.ValidationError(
                "Only draft jobs can change their parts here; its parts cannot change "
                "(use the job page, which re-slices)."
            )
        return job


@admin.register(PrintJobPart)
class PrintJobPartAdmin(admin.ModelAdmin):
    """Job parts; frozen once their job has print attempts (printed quantities depend on them)."""

    form = PrintJobPartAdminForm
    list_display = ("print_job", "part", "quantity")
    list_filter = ("print_job",)

    @staticmethod
    def _frozen(queryset: QuerySet[PrintJobPart]) -> QuerySet[PrintJobPart]:
        """Return the entries whose job is not a draft or has print attempts."""
        return queryset.filter(
            ~Q(print_job__status=PrintJob.STATUS_DRAFT) | Q(print_job__plates__queue_entries__isnull=False)
        )

    @staticmethod
    def _job_started(request: HttpRequest, obj: PrintJobPart) -> bool:
        """Return whether the entry's job is frozen (cached per request and job).

        The part/project delete confirmation asks this for every protected entry.
        """
        cache = request.__dict__.setdefault("_printjob_frozen_cache", {})
        if obj.print_job_id not in cache:
            cache[obj.print_job_id] = _contents_frozen(PrintJob.objects.get(pk=obj.print_job_id))
        return cache[obj.print_job_id]

    def has_change_permission(self, request: HttpRequest, obj: PrintJobPart | None = None) -> bool:
        """Refuse changing an entry of a job with print attempts."""
        if obj is not None and self._job_started(request, obj):
            return False
        return super().has_change_permission(request, obj)

    def has_delete_permission(self, request: HttpRequest, obj: PrintJobPart | None = None) -> bool:
        """Refuse deleting an entry of a job with print attempts."""
        if obj is not None and self._job_started(request, obj):
            return False
        return super().has_delete_permission(request, obj)

    def save_model(self, request: HttpRequest, obj: PrintJobPart, form: Any, change: bool) -> None:
        """Save after re-checking (in the same transaction) that neither job has print attempts."""
        with transaction.atomic():
            job_ids = {obj.print_job_id}
            if change:
                job_ids.add(PrintJobPart.objects.filter(pk=obj.pk).values_list("print_job_id", flat=True).first())
            if any(_contents_frozen(job) for job in PrintJob.objects.filter(pk__in=job_ids - {None})):
                raise PermissionDenied("Only entries of draft jobs without print attempts can change.")
            super().save_model(request, obj, form, change)

    def delete_model(self, request: HttpRequest, obj: PrintJobPart) -> None:
        """Delete one entry after re-checking the guard in the same transaction."""
        self.delete_queryset(request, PrintJobPart.objects.filter(pk=obj.pk))

    def delete_queryset(self, request: HttpRequest, queryset: QuerySet[PrintJobPart]) -> None:
        """Bulk-delete; refuse if an entry's job has print attempts."""
        with transaction.atomic():
            if self._frozen(queryset).exists():
                raise PermissionDenied("Only entries of draft jobs without print attempts can be deleted.")
            super().delete_queryset(request, queryset)


@admin.register(PrintJobPlate)
class PrintJobPlateAdmin(_PrintHistoryDeleteGuardMixin, admin.ModelAdmin):
    list_display = (
        "print_job",
        "plate_number",
        "state_label",
        "filament_used_grams",
        "print_time_estimate",
    )
    list_filter = ("print_job",)

    def get_queryset(self, request: HttpRequest) -> QuerySet[PrintJobPlate]:
        """Annotate the derived plate state for the change list."""
        return super().get_queryset(request).with_state()

    @admin.display(description="State")
    def state_label(self, obj: PrintJobPlate) -> str:
        """Return the derived plate state."""
        return obj.get_state_display()

    def has_add_permission(self, request: HttpRequest) -> bool:
        """Refuse adding plates by hand: plates come from the slicer only."""
        return False

    def get_readonly_fields(self, request: HttpRequest, obj: PrintJobPlate | None = None) -> tuple[str, ...]:
        """Keep a plate on its job and number (re-pointing would move print history between jobs)."""
        readonly = tuple(super().get_readonly_fields(request, obj))
        if obj is not None:
            readonly += ("print_job", "plate_number")
        return readonly

    def _history_attempts(self, objs: Iterable[PrintJobPlate]) -> QuerySet[PrintQueue]:
        # Any attempt counts: deleting the plate would cascade its attempts.
        return PrintQueue.objects.filter(plate__in=[o.pk for o in objs])


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


class PrintQueueAdminForm(forms.ModelForm):
    """Admin form that validates "one open attempt per plate" itself.

    ``result`` is read-only in the admin, so Django skips the conditional
    unique constraint (its condition references an excluded field) — without
    this check a duplicate would only fail in the database (500).
    """

    class Meta:
        model = PrintQueue
        fields = [
            "plate",
            "printer",
            "priority",
            "position",
            "max_retries",
            "result",
            "resolved_at",
            "is_retry",
            "started_at",
            "completed_at",
            "progress",
            "status_updated_at",
            "last_error",
            "start_claimed_at",
        ]

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        if "printer" in self.fields:
            # Deactivated printers get no new attempts (same as the queue form).
            self.fields["printer"].queryset = PrinterProfile.objects.active()

    def clean_plate(self) -> PrintJobPlate | None:
        """Reject a plate that already has another open attempt."""
        plate = self.cleaned_data.get("plate")
        if plate is not None and not self.instance.result:
            others = PrintQueue.objects.open().filter(plate=plate).exclude(pk=self.instance.pk)
            if others.exists():
                raise forms.ValidationError("This plate already has an open print attempt.")
        return plate


@admin.register(PrintQueue)
class PrintQueueAdmin(admin.ModelAdmin):
    form = PrintQueueAdminForm
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

    #: Set only by the model transitions (``mark_started``, ``mark_finished``,
    #: ``resolve``, ``remove``) and by the worker / print start; editing them
    #: here (or saving a stale form over them) would bypass the transitions.
    MANAGED_FIELDS = (
        "result",
        "resolved_at",
        "is_retry",
        "started_at",
        "completed_at",
        "start_claimed_at",
        "progress",
        "status_updated_at",
        "last_error",
    )
    #: Frozen after creation: moving an attempt would break the retry cycle and
    #: queue order; remove it and enqueue it again instead.
    IDENTITY_FIELDS = ("plate", "printer")
    #: Planning fields, additionally frozen once the attempt is history or starting.
    PLANNING_FIELDS = ("priority", "position", "max_retries")

    def get_readonly_fields(self, request: HttpRequest, obj: PrintQueue | None = None) -> tuple[str, ...]:
        """Allow a manual enqueue (add) and re-planning of open attempts; everything else is read-only."""
        readonly = tuple(super().get_readonly_fields(request, obj)) + self.MANAGED_FIELDS
        if obj is None:
            return readonly
        readonly += self.IDENTITY_FIELDS
        if obj.is_history or obj.has_active_start_claim:
            readonly += self.PLANNING_FIELDS
        return readonly

    def has_delete_permission(self, request: HttpRequest, obj: PrintQueue | None = None) -> bool:
        """Refuse deleting history and attempts whose print start is in progress."""
        if obj is not None and (obj.is_history or obj.has_active_start_claim):
            return False
        return super().has_delete_permission(request, obj)

    def delete_model(self, request: HttpRequest, obj: PrintQueue) -> None:
        """Delete one never-started attempt (re-checked under lock)."""
        self.delete_queryset(request, PrintQueue.objects.filter(pk=obj.pk))

    def delete_queryset(self, request: HttpRequest, queryset: QuerySet) -> None:
        """Bulk-delete only never-started attempts; refuse if the selection contains protected ones."""
        with transaction.atomic():
            if queryset.filter(PrintQueue.protected_q()).exists():
                raise PermissionDenied(
                    "Started or resolved attempts (print history) and starting prints cannot be deleted."
                )
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
