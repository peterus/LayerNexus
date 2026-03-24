"""Role-based access control mixins for LayerNexus views.

Provides reusable mixins that check Django permissions assigned via
the Admin / Operator / Designer group system.  Views include one of
these mixins instead of manually filtering by ``created_by``.
"""

from django.contrib.auth.mixins import LoginRequiredMixin, PermissionRequiredMixin


class RoleRequiredMixin(LoginRequiredMixin, PermissionRequiredMixin):
    """Base mixin that requires login and a specific permission.

    Subclasses (or concrete views) must set ``permission_required`` to
    the codename string (e.g. ``"core.can_manage_projects"``) or a
    tuple of codenames.  Unauthenticated users are redirected to login;
    authenticated users without the permission get a 403 page.
    """

    raise_exception = True


class AdminRequiredMixin(RoleRequiredMixin):
    """Restrict access to users in the Admin group (or superusers).

    Used for user-management views.  The permission checked does not
    need to exist as a model permission — we use ``auth.change_user``
    which only the Admin group has.
    """

    permission_required = "auth.change_user"


class ProjectManageMixin(RoleRequiredMixin):
    """Require permission to create/edit/delete projects and parts."""

    permission_required = "core.can_manage_projects"


class PrinterManageMixin(RoleRequiredMixin):
    """Require permission to create/edit/delete printers."""

    permission_required = "core.can_manage_printers"


class PrinterControlMixin(RoleRequiredMixin):
    """Require permission to start prints and cancel running prints."""

    permission_required = "core.can_control_printer"


class OrcaProfileManageMixin(RoleRequiredMixin):
    """Require permission to import/edit/delete OrcaSlicer profiles."""

    permission_required = "core.can_manage_orca_profiles"


class FilamentMappingManageMixin(RoleRequiredMixin):
    """Require permission to manage Spoolman filament mappings."""

    permission_required = "core.can_manage_filament_mappings"


class QueueManageMixin(RoleRequiredMixin):
    """Require permission to add/remove jobs from the print queue."""

    permission_required = "core.can_manage_print_queue"


class QueueDequeueMixin(RoleRequiredMixin):
    """Require permission to remove waiting jobs from the queue.

    Both Operator and Designer have this permission, but Views should
    additionally check that the queue entry status is ``waiting`` before
    allowing a Designer to dequeue.
    """

    permission_required = "core.can_dequeue_job"
