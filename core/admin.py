from django.contrib import admin

from .models import (
    CostProfile,
    FileVersion,
    HardwarePart,
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
    SpoolmanFilamentMapping,
)


class PartInline(admin.TabularInline):
    model = Part
    extra = 0
    fields = ("name", "quantity", "color", "material", "stl_file")


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


@admin.register(Project)
class ProjectAdmin(admin.ModelAdmin):
    list_display = ("name", "created_by", "created_at", "updated_at")
    list_filter = ("created_by", "created_at")
    search_fields = ("name", "description")
    inlines = [PartInline, ProjectHardwareInline]


@admin.register(Part)
class PartAdmin(admin.ModelAdmin):
    list_display = ("name", "project", "quantity", "color", "material")
    list_filter = ("color", "material", "project")
    search_fields = ("name", "project__name")


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
        "gcode_flavor",
        "printer_model",
        "first_nozzle_diameter",
        "created_by",
    )
    list_filter = ("state", "gcode_flavor", "printer_structure", "created_by")
    search_fields = ("name", "orca_name", "description", "printer_model")
    readonly_fields = (
        "state",
        "uploaded_json",
        "extra_settings",
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
        "extra_settings",
        "created_at",
        "updated_at",
    )


@admin.register(OrcaPrintPreset)
class OrcaPrintPresetAdmin(admin.ModelAdmin):
    list_display = (
        "name",
        "orca_name",
        "state",
        "layer_height",
        "sparse_infill_density",
        "enable_support",
        "created_by",
    )
    list_filter = ("state", "created_by")
    search_fields = ("name", "orca_name", "description")
    readonly_fields = (
        "state",
        "uploaded_json",
        "extra_settings",
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
        "status",
        "priority",
        "position",
        "retry_count",
        "started_at",
        "completed_at",
        "added_at",
    )
    list_filter = ("printer", "priority", "status")
    ordering = ["-priority", "position"]


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
