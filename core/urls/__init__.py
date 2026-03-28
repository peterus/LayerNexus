"""URL configuration for the core app."""

from .auth import urlpatterns as auth_urls
from .bambuauth import urlpatterns as bambuauth_urls
from .dashboard import urlpatterns as dashboard_urls
from .documents import urlpatterns as document_urls
from .hardware import urlpatterns as hardware_urls
from .materials import urlpatterns as material_urls
from .orca_profiles import urlpatterns as orca_profile_urls
from .parts import urlpatterns as part_urls
from .print_jobs import urlpatterns as print_job_urls
from .printers import urlpatterns as printer_urls
from .projects import urlpatterns as project_urls
from .queue import urlpatterns as queue_urls
from .services import urlpatterns as service_urls

app_name = "core"

urlpatterns = (
    dashboard_urls
    + project_urls
    + part_urls
    + print_job_urls
    + printer_urls
    + queue_urls
    + orca_profile_urls
    + material_urls
    + service_urls
    + document_urls
    + hardware_urls
    + auth_urls
    + bambuauth_urls
)
