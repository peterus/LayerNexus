"""URL configuration for the core app."""

from django.urls import path

from . import views

app_name = "core"

urlpatterns = [
    # Dashboard
    path("", views.DashboardView.as_view(), name="dashboard"),
    # Farm Dashboard
    path("farm/", views.FarmDashboardView.as_view(), name="farm_dashboard"),
    # Statistics
    path("statistics/", views.StatisticsView.as_view(), name="statistics"),
    # Admin Dashboard
    path("admin-dashboard/", views.AdminDashboardView.as_view(), name="admin_dashboard"),
    # Projects
    path("projects/", views.ProjectListView.as_view(), name="project_list"),
    path("projects/new/", views.ProjectCreateView.as_view(), name="project_create"),
    path("projects/<int:pk>/", views.ProjectDetailView.as_view(), name="project_detail"),
    path(
        "projects/<int:pk>/edit/",
        views.ProjectUpdateView.as_view(),
        name="project_update",
    ),
    path(
        "projects/<int:pk>/delete/",
        views.ProjectDeleteView.as_view(),
        name="project_delete",
    ),
    path(
        "projects/<int:pk>/cost/",
        views.ProjectCostView.as_view(),
        name="project_cost",
    ),
    path(
        "projects/<int:parent_pk>/subprojects/new/",
        views.SubProjectCreateView.as_view(),
        name="subproject_create",
    ),
    # Parts
    path(
        "projects/<int:project_pk>/parts/new/",
        views.PartCreateView.as_view(),
        name="part_create",
    ),
    path("parts/<int:pk>/", views.PartDetailView.as_view(), name="part_detail"),
    path("parts/<int:pk>/edit/", views.PartUpdateView.as_view(), name="part_update"),
    path("parts/<int:pk>/delete/", views.PartDeleteView.as_view(), name="part_delete"),
    path("parts/<int:pk>/re-estimate/", views.PartReEstimateView.as_view(), name="part_re_estimate"),
    path(
        "projects/<int:pk>/re-estimate/",
        views.ProjectReEstimateView.as_view(),
        name="project_re_estimate",
    ),
    path(
        "projects/<int:pk>/create-jobs/",
        views.CreateJobsFromProjectView.as_view(),
        name="project_create_jobs",
    ),
    # Project Documents
    path(
        "projects/<int:project_pk>/documents/new/",
        views.ProjectDocumentCreateView.as_view(),
        name="document_create",
    ),
    path(
        "documents/<int:pk>/delete/",
        views.ProjectDocumentDeleteView.as_view(),
        name="document_delete",
    ),
    path(
        "documents/<int:pk>/download/",
        views.ProjectDocumentDownloadView.as_view(),
        name="document_download",
    ),
    # Project Hardware
    path(
        "projects/<int:project_pk>/hardware/new/",
        views.ProjectHardwareCreateView.as_view(),
        name="hardware_create",
    ),
    path(
        "hardware/<int:pk>/edit/",
        views.ProjectHardwareUpdateView.as_view(),
        name="hardware_update",
    ),
    path(
        "hardware/<int:pk>/delete/",
        views.ProjectHardwareDeleteView.as_view(),
        name="hardware_delete",
    ),
    # Printer Profiles
    path("printers/", views.PrinterProfileListView.as_view(), name="printerprofile_list"),
    path(
        "printers/new/",
        views.PrinterProfileCreateView.as_view(),
        name="printerprofile_create",
    ),
    path(
        "printers/<int:pk>/edit/",
        views.PrinterProfileUpdateView.as_view(),
        name="printerprofile_update",
    ),
    path(
        "printers/<int:pk>/delete/",
        views.PrinterProfileDeleteView.as_view(),
        name="printerprofile_delete",
    ),
    path(
        "printers/<int:printer_pk>/cost/",
        views.CostProfileUpdateView.as_view(),
        name="costprofile_update",
    ),
    # Print Jobs
    path("jobs/", views.PrintJobListView.as_view(), name="printjob_list"),
    path("jobs/new/", views.PrintJobCreateView.as_view(), name="printjob_create"),
    path(
        "jobs/<int:pk>/",
        views.PrintJobDetailView.as_view(),
        name="printjob_detail",
    ),
    path(
        "jobs/<int:pk>/edit/",
        views.PrintJobUpdateView.as_view(),
        name="printjob_update",
    ),
    path(
        "jobs/<int:pk>/delete/",
        views.PrintJobDeleteView.as_view(),
        name="printjob_delete",
    ),
    path(
        "jobs/<int:pk>/slice/",
        views.PrintJobSliceView.as_view(),
        name="printjob_slice",
    ),
    path(
        "jobs/<int:job_pk>/remove-part/<int:job_part_pk>/",
        views.RemovePartFromJobView.as_view(),
        name="printjob_remove_part",
    ),
    path(
        "parts/<int:part_pk>/add-to-job/",
        views.AddPartToJobView.as_view(),
        name="add_part_to_job",
    ),
    # Print Queue
    path("queue/", views.PrintQueueListView.as_view(), name="printqueue_list"),
    path("queue/add/", views.PrintQueueCreateView.as_view(), name="printqueue_create"),
    path(
        "queue/<int:pk>/delete/",
        views.PrintQueueDeleteView.as_view(),
        name="printqueue_delete",
    ),
    path(
        "queue/run/<int:printer_pk>/",
        views.RunNextQueueView.as_view(),
        name="run_queue",
    ),
    path(
        "queue/run-all/",
        views.RunAllQueuesView.as_view(),
        name="run_all_queues",
    ),
    path(
        "queue/<int:pk>/review/",
        views.QueueEntryReviewView.as_view(),
        name="printqueue_review",
    ),
    path(
        "api/queue/<int:pk>/check-status/",
        views.QueueCheckPrinterStatusView.as_view(),
        name="queue_check_printer_status",
    ),
    path(
        "queue/<int:pk>/cancel/",
        views.CancelQueueEntryView.as_view(),
        name="printqueue_cancel",
    ),
    # OrcaSlicer Profiles
    # Machine Profiles (new structured import)
    path(
        "orca-machine-profiles/",
        views.OrcaMachineProfileListView.as_view(),
        name="orcamachineprofile_list",
    ),
    path(
        "orca-machine-profiles/import/",
        views.OrcaMachineProfileImportView.as_view(),
        name="orcamachineprofile_import",
    ),
    path(
        "orca-machine-profiles/<int:pk>/",
        views.OrcaMachineProfileDetailView.as_view(),
        name="orcamachineprofile_detail",
    ),
    path(
        "orca-machine-profiles/<int:pk>/delete/",
        views.OrcaMachineProfileDeleteView.as_view(),
        name="orcamachineprofile_delete",
    ),
    # Filament Profiles (structured import)
    path(
        "orca-filament-profiles/",
        views.OrcaFilamentProfileListView.as_view(),
        name="orcafilamentprofile_list",
    ),
    path(
        "orca-filament-profiles/import/",
        views.OrcaFilamentProfileImportView.as_view(),
        name="orcafilamentprofile_import",
    ),
    path(
        "orca-filament-profiles/<int:pk>/",
        views.OrcaFilamentProfileDetailView.as_view(),
        name="orcafilamentprofile_detail",
    ),
    path(
        "orca-filament-profiles/<int:pk>/delete/",
        views.OrcaFilamentProfileDeleteView.as_view(),
        name="orcafilamentprofile_delete",
    ),
    # Print Presets (structured import)
    path(
        "orca-print-presets/",
        views.OrcaPrintPresetListView.as_view(),
        name="orcaprintpreset_list",
    ),
    path(
        "orca-print-presets/import/",
        views.OrcaPrintPresetImportView.as_view(),
        name="orcaprintpreset_import",
    ),
    path(
        "orca-print-presets/<int:pk>/",
        views.OrcaPrintPresetDetailView.as_view(),
        name="orcaprintpreset_detail",
    ),
    path(
        "orca-print-presets/<int:pk>/delete/",
        views.OrcaPrintPresetDeleteView.as_view(),
        name="orcaprintpreset_delete",
    ),
    # Materials (Spoolman-backed) with inline profile mapping
    path(
        "materials/",
        views.MaterialProfileListView.as_view(),
        name="materialprofile_list",
    ),
    path(
        "materials/save-mapping/",
        views.SaveFilamentMappingView.as_view(),
        name="save_filament_mapping",
    ),
    # Service integrations
    path(
        "jobs/<int:pk>/slice-status/",
        views.SliceJobStatusView.as_view(),
        name="slice_job_status",
    ),
    path(
        "printers/<int:printer_pk>/spools/",
        views.SpoolmanSpoolsView.as_view(),
        name="spoolman_spools",
    ),
    path(
        "api/spoolman/filaments/",
        views.SpoolmanFilamentsAPIView.as_view(),
        name="spoolman_filaments_api",
    ),
    path(
        "printers/<int:printer_pk>/status/",
        views.PrinterStatusView.as_view(),
        name="printer_status",
    ),
    path(
        "plates/<int:plate_pk>/upload/",
        views.UploadToPrinterView.as_view(),
        name="upload_to_printer",
    ),
    # Auth
    path("register/", views.RegisterView.as_view(), name="register"),
    path("profile/", views.ProfileView.as_view(), name="profile"),
    # User Management (Admin)
    path("users/", views.UserListView.as_view(), name="user_list"),
    path("users/create/", views.UserCreateView.as_view(), name="user_create"),
    path("users/<int:pk>/edit/", views.UserUpdateView.as_view(), name="user_update"),
    path(
        "users/<int:pk>/delete/",
        views.UserDeleteView.as_view(),
        name="user_delete",
    ),
]
