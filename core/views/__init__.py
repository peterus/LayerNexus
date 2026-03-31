"""Views package for the LayerNexus application."""

from .auth import (
    RegisterView,
    ProfileView,
    UserListView,
    UserCreateView,
    UserUpdateView,
    UserDeleteView,
)
from .dashboard import (
    DashboardView,
    FarmDashboardView,
    StatisticsView,
    AdminDashboardView,
)
from .documents import (
    ProjectDocumentCreateView,
    ProjectDocumentDeleteView,
    ProjectDocumentDownloadView,
)
from .hardware import (
    ProjectHardwareCreateView,
    ProjectHardwareUpdateView,
    ProjectHardwareDeleteView,
)
from .materials import (
    SpoolmanSpoolsView,
    SpoolmanFilamentsAPIView,
    MaterialProfileListView,
    SaveFilamentMappingView,
)
from .orca_profiles import (
    OrcaMachineProfileListView,
    OrcaMachineProfileDetailView,
    OrcaMachineProfileImportView,
    OrcaMachineProfileDeleteView,
    OrcaFilamentProfileListView,
    OrcaFilamentProfileDetailView,
    OrcaFilamentProfileImportView,
    OrcaFilamentProfileDeleteView,
    OrcaPrintPresetListView,
    OrcaPrintPresetDetailView,
    OrcaPrintPresetImportView,
    OrcaPrintPresetDeleteView,
)
from .parts import (
    PartDetailView,
    PartCreateView,
    PartUpdateView,
    PartDeleteView,
    PartReEstimateView,
)
from .print_jobs import (
    PrintJobListView,
    PrintJobCreateView,
    PrintJobDetailView,
    PrintJobUpdateView,
    PrintJobDeleteView,
    AddPartToJobView,
    RemovePartFromJobView,
    CreateJobsFromProjectView,
    PrintJobSliceView,
    SliceJobStatusView,
)
from .printers import (
    PrinterProfileListView,
    PrinterProfileCreateView,
    PrinterProfileUpdateView,
    PrinterProfileDeleteView,
    CostProfileUpdateView,
    PrinterStatusView,
    UploadToPrinterView,
)
from .projects import (
    ProjectListView,
    ProjectDetailView,
    ProjectCreateView,
    SubProjectCreateView,
    ProjectUpdateView,
    ProjectDeleteView,
    ProjectCostView,
    ProjectReEstimateView,
)
from .queue import (
    PrintQueueListView,
    PrintQueueCreateView,
    PrintQueueDeleteView,
    RunNextQueueView,
    RunAllQueuesView,
    QueueEntryReviewView,
    QueueCheckPrinterStatusView,
    CancelQueueEntryView,
)
