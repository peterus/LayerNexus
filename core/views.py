"""Views for the LayerNexus 3D printing project management application."""

import json
import logging
import re
import threading
from collections import defaultdict
from datetime import timedelta
from pathlib import Path
from typing import Any

from django import forms as django_forms
from django.conf import settings
from django.contrib import messages
from django.contrib.auth import login
from django.contrib.auth.mixins import LoginRequiredMixin
from django.contrib.auth.models import Group, User
from django.db import IntegrityError
from django.db.models import Count, Max, QuerySet, Sum
from django.http import FileResponse, HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse, reverse_lazy
from django.utils import timezone
from django.views import View
from django.views.generic import (
    CreateView,
    DeleteView,
    DetailView,
    FormView,
    ListView,
    TemplateView,
    UpdateView,
)

from .forms import (
    AddPartToJobForm,
    CostProfileForm,
    OrcaFilamentProfileImportForm,
    OrcaMachineProfileImportForm,
    OrcaPrintPresetImportForm,
    PartForm,
    PrinterProfileForm,
    PrintJobForm,
    PrintJobUpdateForm,
    PrintQueueForm,
    ProfileUpdateForm,
    ProjectDocumentForm,
    ProjectEditForm,
    ProjectForm,
    ProjectHardwareForm,
    ProjectHardwareUpdateForm,
    SubProjectForm,
    UserManagementForm,
    UserRegistrationForm,
)
from .mixins import (
    AdminRequiredMixin,
    FilamentMappingManageMixin,
    OrcaProfileManageMixin,
    PrinterControlMixin,
    PrinterManageMixin,
    ProjectManageMixin,
    QueueDequeueMixin,
    QueueManageMixin,
    RoleRequiredMixin,
)
from .models import (
    CostProfile,
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
from .services.moonraker import MoonrakerClient, MoonrakerError
from .services.orcaslicer import OrcaSlicerAPIClient, OrcaSlicerError
from .services.spoolman import SpoolmanClient, SpoolmanError
from .services.threemf import ThreeMFError, create_3mf_bundle

logger = logging.getLogger(__name__)


def _user_projects_qs(user: User) -> QuerySet:
    """Return all projects (global access in single-tenant mode).

    Args:
        user: User instance (kept for API compatibility but not used for filtering).

    Returns:
        QuerySet of all Project instances.
    """
    return Project.objects.all()


def _build_slicer_kwargs(
    machine_profile: OrcaMachineProfile | None,
    print_preset: OrcaPrintPreset | None,
    filament_profile: OrcaFilamentProfile | None,
) -> dict[str, Any]:
    """Build keyword arguments for OrcaSlicerAPIClient.slice() from profiles.

    All three profile types use the structured approach – the full
    flattened settings are reconstructed via ``to_orca_json()`` /
    ``filament_to_orca_json()`` / ``process_to_orca_json()``.

    Args:
        machine_profile: Resolved OrcaMachineProfile instance (or None).
        print_preset: Resolved OrcaPrintPreset instance (or None).
        filament_profile: Resolved OrcaFilamentProfile instance (or None).

    Returns:
        Dictionary with profile arguments for the slicer API.
    """
    import json

    from .services.profile_import import (
        filament_to_orca_json,
        process_to_orca_json,
        to_orca_json,
    )

    kwargs: dict[str, Any] = {}
    logger.debug(
        f"Building slicer kwargs from profiles - Machine: {machine_profile.name if machine_profile else 'None'}, "
        f"Preset: {print_preset.name if print_preset else 'None'}, "
        f"Filament: {filament_profile.name if filament_profile else 'None'}"
    )

    # ── Machine profile (structured approach) ─────────────────────────
    if machine_profile:
        try:
            orca_json = to_orca_json(machine_profile)

            # Inject thumbnail settings if not already present.
            # OrcaSlicer system profiles for Klipper set these internally,
            # but they are often missing from exported user-profile JSONs.
            # Without them the slicer won't embed preview images in G-code.
            if "thumbnails" not in orca_json:
                orca_json["thumbnails"] = ["32x32", "400x300"]
                logger.debug("Machine: Injected default thumbnails setting")
            if "thumbnails_format" not in orca_json:
                orca_json["thumbnails_format"] = "PNG"
                logger.debug("Machine: Injected default thumbnails_format=PNG")

            kwargs["printer_profile_json"] = json.dumps(orca_json).encode("utf-8")
            logger.debug(f"Machine: Using reconstructed JSON for '{machine_profile.orca_name}'")
        except ValueError as exc:
            logger.warning(f"Machine profile not usable: {exc}")

    # ── Filament profile (structured approach) ────────────────────────
    if filament_profile:
        try:
            fil_json = filament_to_orca_json(filament_profile)
            kwargs["filament_profile_json"] = json.dumps(fil_json).encode("utf-8")
            logger.debug(f"Filament: Using reconstructed JSON for '{filament_profile.orca_name}'")
        except ValueError as exc:
            logger.warning(f"Filament profile not usable: {exc}")

    # ── Process / print preset (structured approach) ──────────────────
    if print_preset:
        try:
            proc_json = process_to_orca_json(print_preset)

            # Ensure the machine profile's *system* name is listed in the
            # process preset's ``compatible_printers``.  The OrcaSlicer
            # CLI reads the machine's ``inherits`` value as the "system
            # printer name" and checks it against this list.  If the
            # user's machine profile inherits from a system profile that
            # is already listed, no injection is needed.  As a safety
            # net we add both the machine's ``inherits_name`` (system
            # name) and the machine's ``orca_name`` (leaf name) so the
            # check passes in every case.
            if machine_profile:
                compat = proc_json.get("compatible_printers")
                if isinstance(compat, list):
                    names_to_add = []
                    if machine_profile.inherits_name and machine_profile.inherits_name not in compat:
                        names_to_add.append(machine_profile.inherits_name)
                    if machine_profile.orca_name not in compat:
                        names_to_add.append(machine_profile.orca_name)
                    if names_to_add:
                        proc_json["compatible_printers"] = compat + names_to_add
                        logger.debug(f"Process: Added {names_to_add} to compatible_printers")

            kwargs["preset_profile_json"] = json.dumps(proc_json).encode("utf-8")
            logger.debug(f"Process: Using reconstructed JSON for '{print_preset.orca_name}'")
        except ValueError as exc:
            logger.warning(f"Process profile not usable: {exc}")

    logger.debug(f"Final slicer kwargs: {list(kwargs.keys())}")

    return kwargs


def _estimate_part_in_background(part_pk: int) -> None:
    """Slice a single copy of a part to obtain per-copy filament/time estimates.

    Called by the estimation worker thread.  Picks a compatible machine
    profile automatically based on the part's print preset.  Results are
    stored directly on the Part instance (``filament_used_grams``,
    ``filament_used_meters``, ``estimated_print_time``).

    The caller is responsible for managing the DB connection lifecycle.

    Args:
        part_pk: Primary key of the Part to estimate.
    """
    from pathlib import Path as FSPath

    try:
        part = Part.objects.select_related(
            "project__default_print_preset",
            "project__parent__default_print_preset",
            "print_preset",
        ).get(pk=part_pk)

        if not part.stl_file:
            logger.debug("estimate_part(%s): no STL file, skipping", part_pk)
            Part.objects.filter(pk=part_pk).update(
                estimation_status=Part.ESTIMATION_NONE,
            )
            return

        # Resolve profiles (traverses parent project hierarchy)
        print_preset = part.effective_print_preset
        if not print_preset:
            logger.debug("estimate_part(%s): no print preset, skipping", part_pk)
            Part.objects.filter(pk=part_pk).update(
                estimation_status=Part.ESTIMATION_NONE,
            )
            return

        # Find a compatible machine profile for this preset
        machine_profile = _find_compatible_machine(print_preset)
        if not machine_profile:
            logger.warning("estimate_part(%s): no compatible machine profile found", part_pk)
            return

        # Resolve filament profile from Spoolman mapping
        filament_profile = None
        if part.spoolman_filament_id:
            mapping = (
                SpoolmanFilamentMapping.objects.filter(
                    spoolman_filament_id=part.spoolman_filament_id,
                )
                .select_related("orca_filament_profile")
                .first()
            )
            if mapping and mapping.orca_filament_profile:
                filament_profile = mapping.orca_filament_profile

        # Build 3MF with single copy
        stl_path = FSPath(part.stl_file.path)
        threemf_content = create_3mf_bundle([(stl_path, 1)])

        slice_kwargs = _build_slicer_kwargs(
            machine_profile=machine_profile,
            print_preset=print_preset,
            filament_profile=filament_profile,
        )

        # Status already set to ESTIMATING by the worker loop (atomic claim)
        slicer = OrcaSlicerAPIClient(settings.ORCASLICER_API_URL)
        result = slicer.slice_bundle(threemf_content, **slice_kwargs)

        # Re-fetch part to avoid stale state
        part = Part.objects.get(pk=part_pk)

        updated: list[str] = []
        total_g = result.total_filament_grams
        total_mm = result.total_filament_mm
        total_time = result.total_print_time_seconds

        if total_g is not None:
            part.filament_used_grams = round(total_g, 2)
            updated.append("filament_used_grams")
        if total_mm is not None:
            part.filament_used_meters = round(total_mm / 1000.0, 4)
            updated.append("filament_used_meters")
        if total_time is not None:
            part.estimated_print_time = timedelta(seconds=total_time)
            updated.append("estimated_print_time")

        if updated:
            part.estimation_status = Part.ESTIMATION_SUCCESS
            part.estimation_error = ""
            updated.extend(["estimation_status", "estimation_error"])
            part.save(update_fields=updated)
            logger.info(
                "estimate_part(%s): saved estimates — %sg, %sm, %ss",
                part_pk,
                part.filament_used_grams,
                part.filament_used_meters,
                total_time,
            )
        else:
            Part.objects.filter(pk=part_pk).update(
                estimation_status=Part.ESTIMATION_ERROR,
                estimation_error="Slicing returned no filament or time estimates.",
            )
            logger.warning("estimate_part(%s): slicing returned no estimates", part_pk)

    except (OrcaSlicerError, FileNotFoundError) as exc:
        Part.objects.filter(pk=part_pk).update(
            estimation_status=Part.ESTIMATION_ERROR,
            estimation_error=str(exc),
        )
        logger.warning("estimate_part(%s): slicing failed — %s", part_pk, exc)
    except Part.DoesNotExist:
        logger.debug("estimate_part(%s): part no longer exists", part_pk)
    except Exception as exc:
        Part.objects.filter(pk=part_pk).update(
            estimation_status=Part.ESTIMATION_ERROR,
            estimation_error=f"Unexpected error: {exc}",
        )
        logger.exception("estimate_part(%s): unexpected error", part_pk)


# ---------------------------------------------------------------------------
# OrcaSlicer worker – unified sequential queue for estimation & slicing
# ---------------------------------------------------------------------------

_orcaslicer_worker_lock = threading.Lock()
_orcaslicer_worker_active = False


def _start_orcaslicer_worker() -> None:
    """Start the OrcaSlicer worker thread if not already running.

    The worker processes pending estimation parts and pending slicing
    jobs sequentially, one at a time.  Only one worker thread is active
    at any given time to avoid overloading the OrcaSlicer API.
    """
    global _orcaslicer_worker_active
    with _orcaslicer_worker_lock:
        if _orcaslicer_worker_active:
            return
        _orcaslicer_worker_active = True

    thread = threading.Thread(
        target=_orcaslicer_worker_loop,
        daemon=True,
    )
    thread.start()
    logger.info("Started OrcaSlicer worker thread")


def _orcaslicer_worker_loop() -> None:
    """Process pending estimation and slicing jobs sequentially.

    Each iteration picks either the next Part with
    ``estimation_status='pending'`` or the next PrintJob with
    ``status='pending'``, processes it via the OrcaSlicer API, and
    repeats until no more pending work remains.  Estimation parts are
    Slicing jobs are prioritised over estimations because users are
    actively waiting for sliced G-code.
    """
    global _orcaslicer_worker_active
    from django.db import connection

    try:
        while True:
            # 1. Check for pending slicing jobs (higher priority)
            #    Atomically claim the job: PENDING → SLICING
            next_job_pk = (
                PrintJob.objects.filter(
                    status=PrintJob.STATUS_PENDING,
                )
                .order_by("pk")
                .values_list("pk", flat=True)
                .first()
            )

            if next_job_pk is not None:
                claimed = PrintJob.objects.filter(
                    pk=next_job_pk,
                    status=PrintJob.STATUS_PENDING,
                ).update(
                    status=PrintJob.STATUS_SLICING,
                    slicing_started_at=timezone.now(),
                    slicing_error="",
                )
                if claimed:
                    _slice_job_in_background(next_job_pk)
                continue

            # 2. Check for pending estimations (lower priority)
            #    Atomically claim the part: PENDING → ESTIMATING
            next_estimation_pk = (
                Part.objects.filter(
                    estimation_status=Part.ESTIMATION_PENDING,
                )
                .order_by("pk")
                .values_list("pk", flat=True)
                .first()
            )

            if next_estimation_pk is not None:
                claimed = Part.objects.filter(
                    pk=next_estimation_pk,
                    estimation_status=Part.ESTIMATION_PENDING,
                ).update(estimation_status=Part.ESTIMATION_ESTIMATING)
                if claimed:
                    _estimate_part_in_background(next_estimation_pk)
                continue

            # Nothing left to process
            logger.info("OrcaSlicer worker: no more pending work, stopping")
            break
    finally:
        # Re-check for work inside the lock to prevent a second worker
        # from being spawned between clearing the flag and re-checking.
        with _orcaslicer_worker_lock:
            has_pending = (
                Part.objects.filter(
                    estimation_status=Part.ESTIMATION_PENDING,
                ).exists()
                or PrintJob.objects.filter(
                    status=PrintJob.STATUS_PENDING,
                ).exists()
            )
            if has_pending:
                # Keep the worker active; loop again on a new thread
                pass
            else:
                _orcaslicer_worker_active = False

        connection.close()

        if has_pending:
            # Start a fresh thread (flag is still True, so
            # _start_orcaslicer_worker would bail out — call the
            # loop directly on a new daemon thread).
            thread = threading.Thread(
                target=_orcaslicer_worker_loop,
                daemon=True,
            )
            thread.start()
            logger.info("OrcaSlicer worker: restarted for newly queued work")


def _find_compatible_machine(
    print_preset: OrcaPrintPreset,
) -> OrcaMachineProfile | None:
    """Find a resolved OrcaMachineProfile compatible with the given preset.

    Checks the preset's ``compatible_printers`` list against machine
    profiles (by ``orca_name`` or ``inherits_name``).  Falls back to
    the first available resolved machine profile.

    Args:
        print_preset: The OrcaPrintPreset to match against.

    Returns:
        A compatible OrcaMachineProfile, or None if none found.
    """
    # Try to match via compatible_printers in the preset's settings
    compat_printers = print_preset.settings.get("compatible_printers", [])

    if compat_printers:
        # Try exact orca_name match first
        machine = OrcaMachineProfile.objects.filter(
            state=OrcaMachineProfile.STATE_RESOLVED,
            instantiation=True,
            orca_name__in=compat_printers,
        ).first()
        if machine:
            return machine

        # Try inherits_name match (system profile name)
        machine = OrcaMachineProfile.objects.filter(
            state=OrcaMachineProfile.STATE_RESOLVED,
            instantiation=True,
            inherits_name__in=compat_printers,
        ).first()
        if machine:
            return machine

    # Fallback: any resolved machine profile
    return OrcaMachineProfile.objects.filter(
        state=OrcaMachineProfile.STATE_RESOLVED,
        instantiation=True,
    ).first()


def _trigger_part_estimation(part: Part) -> None:
    """Queue a part for background estimation if it has STL + preset.

    Sets the part's estimation_status to 'pending' and ensures the
    unified OrcaSlicer worker thread is running.  The worker processes
    all OrcaSlicer work (estimations and slicing) sequentially.

    Args:
        part: The Part instance to estimate.
    """
    if not part.stl_file:
        return

    preset = part.effective_print_preset
    if not preset:
        return

    Part.objects.filter(pk=part.pk).update(
        estimation_status=Part.ESTIMATION_PENDING,
        estimation_error="",
    )

    _start_orcaslicer_worker()
    logger.info("Queued estimation for part %s (%s)", part.pk, part.name)


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------


class DashboardView(LoginRequiredMixin, TemplateView):
    """Home page showing an overview of the user's projects and recent activity."""

    template_name = "core/dashboard.html"

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        """Build context with project statistics and recent activity.

        Returns:
            Dictionary with template context including projects, stats, and recent jobs.
        """
        context = super().get_context_data(**kwargs)

        projects = Project.objects.all()
        context["projects"] = projects[:5]
        context["recent_jobs"] = PrintJob.objects.all()[:10]
        context["printer_profiles"] = PrinterProfile.objects.all()

        # Dashboard statistics
        all_jobs = PrintJob.objects.all()
        context["total_projects"] = projects.count()
        context["total_parts"] = (
            Part.objects.filter(project__in=projects).aggregate(total=Sum("quantity"))["total"] or 0
        )
        context["total_jobs"] = all_jobs.count()
        context["completed_jobs"] = all_jobs.filter(status="completed").count()
        context["active_jobs"] = all_jobs.filter(status__in=["slicing", "uploading", "uploaded", "printing"]).count()
        context["failed_jobs"] = all_jobs.filter(status="failed").count()

        # Filament statistics
        context["total_filament_grams"] = (
            Part.objects.filter(project__in=projects, filament_used_grams__isnull=False).aggregate(
                total=Sum("filament_used_grams")
            )["total"]
            or 0
        )

        # Queue with entry statuses
        user_queue = PrintQueue.objects.all()
        context["queue_entries"] = user_queue.select_related("plate__print_job", "printer")[:5]
        context["queue_waiting"] = user_queue.filter(
            status=PrintQueue.STATUS_WAITING,
        ).count()
        context["queue_printing"] = user_queue.filter(
            status=PrintQueue.STATUS_PRINTING,
        ).count()
        context["queue_review"] = user_queue.filter(
            status=PrintQueue.STATUS_AWAITING_REVIEW,
        ).count()
        context["queue_total"] = user_queue.count()

        # Farm overview
        printers = PrinterProfile.objects.all()
        context["total_printers"] = printers.count()
        active_printer_ids = (
            user_queue.filter(
                status__in=[
                    PrintQueue.STATUS_PRINTING,
                    PrintQueue.STATUS_AWAITING_REVIEW,
                ],
            )
            .values_list("printer_id", flat=True)
            .distinct()
        )
        context["busy_printers"] = active_printer_ids.count()
        context["idle_printers"] = (
            printers.exclude(
                pk__in=active_printer_ids,
            )
            .filter(moonraker_url__gt="")
            .count()
        )

        # Today's completed
        today_start = timezone.now().replace(
            hour=0,
            minute=0,
            second=0,
            microsecond=0,
        )
        context["completed_today"] = all_jobs.filter(
            status=PrintJob.STATUS_COMPLETED,
            completed_at__gte=today_start,
        ).count()

        # Total completed plates
        context["total_quantity_completed"] = PrintJobPlate.objects.filter(
            status=PrintJobPlate.STATUS_COMPLETED,
        ).count()

        return context


class FarmDashboardView(LoginRequiredMixin, TemplateView):
    """Real-time overview of the entire print farm.

    Shows each printer as a card with its current state (idle / printing /
    awaiting review) plus the active queue entry details.  Also provides
    aggregate statistics and quick-action buttons.
    """

    template_name = "core/farm_dashboard.html"

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        """Build context with per-printer state and farm-wide stats."""
        context = super().get_context_data(**kwargs)

        printers = PrinterProfile.objects.all().select_related("orca_machine_profile")

        # Pre-fetch active queue entries (printing / awaiting_review)
        active_entries = PrintQueue.objects.filter(
            status__in=[
                PrintQueue.STATUS_PRINTING,
                PrintQueue.STATUS_AWAITING_REVIEW,
            ],
        ).select_related("plate__print_job", "printer")
        active_map: dict[int, PrintQueue] = {e.printer_id: e for e in active_entries}

        # Per-printer info
        printer_cards: list[dict[str, Any]] = []
        for p in printers:
            entry = active_map.get(p.pk)
            waiting_count = PrintQueue.objects.filter(
                printer=p,
                status=PrintQueue.STATUS_WAITING,
            ).count()

            if entry and entry.status == PrintQueue.STATUS_PRINTING:
                state = "printing"
            elif entry and entry.status == PrintQueue.STATUS_AWAITING_REVIEW:
                state = "awaiting_review"
            else:
                state = "idle"

            printer_cards.append(
                {
                    "printer": p,
                    "state": state,
                    "active_entry": entry,
                    "waiting_count": waiting_count,
                    "has_moonraker": bool(p.moonraker_url),
                }
            )

        context["printer_cards"] = printer_cards

        # Farm-wide statistics
        all_entries = PrintQueue.objects.all()
        context["total_waiting"] = all_entries.filter(
            status=PrintQueue.STATUS_WAITING,
        ).count()
        context["total_printing"] = all_entries.filter(
            status=PrintQueue.STATUS_PRINTING,
        ).count()
        context["total_awaiting_review"] = all_entries.filter(
            status=PrintQueue.STATUS_AWAITING_REVIEW,
        ).count()
        context["total_printers"] = printers.count()
        context["idle_printers"] = sum(1 for c in printer_cards if c["state"] == "idle" and c["has_moonraker"])

        # Today's completed count
        today_start = timezone.now().replace(hour=0, minute=0, second=0, microsecond=0)
        context["completed_today"] = PrintJobPlate.objects.filter(
            status=PrintJobPlate.STATUS_COMPLETED,
            completed_at__gte=today_start,
        ).count()

        return context


# ---------------------------------------------------------------------------
# Project views
# ---------------------------------------------------------------------------


class ProjectListView(LoginRequiredMixin, ListView):
    """List all top-level projects (sub-projects are excluded)."""

    model = Project
    template_name = "core/project_list.html"
    context_object_name = "projects"

    def get_queryset(self) -> QuerySet:
        """Return only root-level projects (no parent)."""
        return Project.objects.filter(parent__isnull=True)


class ProjectDetailView(LoginRequiredMixin, DetailView):
    """Show details of a single project."""

    model = Project
    template_name = "core/project_detail.html"
    context_object_name = "project"

    def get_context_data(self, **kwargs) -> dict:
        """Add filament requirements, sub-projects, breadcrumb ancestors, and filament name lookup to context."""
        context = super().get_context_data(**kwargs)
        context["filament_requirements"] = self.object.filament_requirements()
        context["subprojects"] = self.object.subprojects.all()
        context["ancestors"] = self.object.get_ancestors()

        # Build filament name and color lookups for part display
        parts = self.object.parts.all()
        filament_ids = {p.spoolman_filament_id for p in parts if p.spoolman_filament_id}
        filament_names: dict[int, str] = {}
        filament_colors: dict[int, str] = {}
        if filament_ids:
            for m in SpoolmanFilamentMapping.objects.filter(
                spoolman_filament_id__in=filament_ids,
            ):
                if m.spoolman_filament_name:
                    filament_names[m.spoolman_filament_id] = m.spoolman_filament_name
                if m.spoolman_color_hex:
                    filament_colors[m.spoolman_filament_id] = m.spoolman_color_hex
        context["filament_names"] = filament_names
        context["filament_colors"] = filament_colors

        # Effective default print preset (own or inherited from parent)
        project = self.object
        effective_preset = project.effective_default_print_preset
        context["effective_default_print_preset"] = effective_preset
        context["preset_inherited"] = effective_preset is not None and project.default_print_preset_id is None

        # Documents (own + aggregated from sub-projects)
        context["documents"] = project.documents.all()
        context["all_documents"] = project._collect_documents()

        # Hardware (own + aggregated from sub-projects)
        context["hardware_assignments"] = project.hardware_assignments.select_related("hardware_part").all()
        context["hardware_requirements"] = project.hardware_requirements()
        context["total_hardware_cost"] = project.total_hardware_cost

        return context


class ProjectCreateView(ProjectManageMixin, CreateView):
    """Create a new project."""

    model = Project
    form_class = ProjectForm
    template_name = "core/project_form.html"

    def get_form(self, form_class: type[ProjectForm] | None = None) -> ProjectForm:
        """Return form instance with queryset filtered to resolved slicer profiles."""
        form = super().get_form(form_class)
        form.fields["default_print_preset"].queryset = OrcaPrintPreset.objects.filter(
            state=OrcaPrintPreset.STATE_RESOLVED,
            instantiation=True,
        )
        return form

    def form_valid(self, form: ProjectForm) -> HttpResponse:
        """Set created_by to current user and save project."""
        form.instance.created_by = self.request.user
        response = super().form_valid(form)
        messages.success(self.request, "Project created successfully.")
        if "_save_and_add_another" in self.request.POST:
            return redirect(reverse("core:project_create"))
        return response

    def get_success_url(self) -> str:
        """Redirect to the newly created project's detail page."""
        return reverse("core:project_detail", kwargs={"pk": self.object.pk})


class SubProjectCreateView(ProjectManageMixin, CreateView):
    """Create a new sub-project under a parent project."""

    model = Project
    form_class = SubProjectForm
    template_name = "core/subproject_form.html"

    def get_parent(self) -> Project:
        """Return the parent project from the URL."""
        return get_object_or_404(Project, pk=self.kwargs["parent_pk"])

    def get_context_data(self, **kwargs) -> dict:
        """Add parent project and breadcrumb ancestors to template context."""
        context = super().get_context_data(**kwargs)
        parent = self.get_parent()
        context["parent"] = parent
        context["ancestors"] = parent.get_ancestors() + [parent]
        return context

    def get_form(self, form_class=None) -> SubProjectForm:
        """Return form with resolved print preset choices."""
        form = super().get_form(form_class)
        form.fields["default_print_preset"].queryset = OrcaPrintPreset.objects.filter(
            state=OrcaPrintPreset.STATE_RESOLVED,
            instantiation=True,
        )
        return form

    def form_valid(self, form: SubProjectForm) -> HttpResponse:
        """Set parent, created_by, and save sub-project."""
        form.instance.parent = self.get_parent()
        form.instance.created_by = self.request.user
        messages.success(self.request, "Sub-project created successfully.")
        return super().form_valid(form)

    def get_success_url(self) -> str:
        """Redirect to the parent project detail page after creation."""
        return reverse("core:project_detail", kwargs={"pk": self.kwargs["parent_pk"]})


class ProjectUpdateView(ProjectManageMixin, UpdateView):
    """Update an existing project."""

    model = Project
    form_class = ProjectEditForm
    template_name = "core/project_form.html"

    def get_context_data(self, **kwargs) -> dict:
        """Add breadcrumb ancestors to template context."""
        context = super().get_context_data(**kwargs)
        context["ancestors"] = self.object.get_ancestors()
        return context

    def get_form(self, form_class=None) -> ProjectEditForm:
        """Return form with filtered parent and print-preset querysets.

        The parent queryset excludes the project itself and all its
        descendants to prevent circular references.
        """
        form = super().get_form(form_class)
        form.fields["default_print_preset"].queryset = OrcaPrintPreset.objects.filter(
            state=OrcaPrintPreset.STATE_RESOLVED,
            instantiation=True,
        )
        excluded_ids = {self.object.pk} | self.object.get_descendant_ids()
        form.fields["parent"].queryset = Project.objects.exclude(
            pk__in=excluded_ids,
        )
        return form

    def form_valid(self, form: ProjectEditForm) -> HttpResponse:
        """Save updated project with success message."""
        messages.success(self.request, "Project updated successfully.")
        return super().form_valid(form)

    def get_success_url(self) -> str:
        """Redirect to the edited project's detail page."""
        return reverse("core:project_detail", kwargs={"pk": self.object.pk})


class ProjectDeleteView(ProjectManageMixin, DeleteView):
    """Delete a project."""

    model = Project
    template_name = "core/project_confirm_delete.html"

    def get_context_data(self, **kwargs) -> dict:
        """Add breadcrumb ancestors to template context."""
        context = super().get_context_data(**kwargs)
        context["ancestors"] = self.object.get_ancestors()
        return context

    def get_success_url(self) -> str:
        """Redirect to parent project if sub-project, otherwise to project list."""
        if self.object.is_subproject:
            return reverse("core:project_detail", kwargs={"pk": self.object.parent_id})
        return reverse_lazy("core:project_list")

    def form_valid(self, form: django_forms.Form) -> HttpResponse:
        """Delete project with success message."""
        messages.success(self.request, "Project deleted.")
        return super().form_valid(form)


# ---------------------------------------------------------------------------
# Part views
# ---------------------------------------------------------------------------


class _SpoolmanFilamentMixin:
    """Shared logic for populating Spoolman filament dropdown on Part forms."""

    _spoolman_configured = False
    _spoolman_filaments: dict = {}
    _spoolman_colors: dict = {}

    def populate_spoolman_filaments(self, form):
        """Populate the spoolman_filament_id field with Spoolman filament types."""
        spoolman_url = settings.SPOOLMAN_URL
        if not spoolman_url:
            self._spoolman_configured = False
            self._spoolman_filaments = {}
            self._spoolman_colors = {}
            return
        try:
            client = SpoolmanClient(spoolman_url)
            filaments = client.get_filaments()
            choices = [("", "— Select Filament Type —")]
            colors: dict[str, str] = {}
            for f in filaments:
                vendor = f.get("vendor", {}) or {}
                vendor_name = vendor.get("name", "")
                name = f.get("name", f"Filament #{f['id']}")
                material = f.get("material", "")
                color_hex = (f.get("color_hex") or "")[:7]
                label = f"{vendor_name} - {name}" if vendor_name else name
                if material:
                    label += f" ({material})"
                choices.append((f["id"], label))
                if color_hex:
                    colors[str(f["id"])] = color_hex
            form.fields["spoolman_filament_id"].widget = django_forms.Select(
                choices=choices,
                attrs={"class": "form-select"},
            )
            self._spoolman_configured = True
            self._spoolman_filaments = {f["id"]: f for f in filaments}
            self._spoolman_colors = colors
        except SpoolmanError:
            self._spoolman_configured = False
            self._spoolman_filaments = {}
            self._spoolman_colors = {}

    def apply_spoolman_filament(self, form):
        """Auto-fill color/material from the selected Spoolman filament.

        Also updates the cached color on the SpoolmanFilamentMapping so that
        the filament requirements display always reflects the current Spoolman
        color, even for parts created before a color change.
        """
        filament_id = form.cleaned_data.get("spoolman_filament_id")
        if filament_id and self._spoolman_filaments:
            fil = self._spoolman_filaments.get(filament_id, {})
            if fil:
                color_hex = (fil.get("color_hex") or "")[:7]
                form.instance.color = color_hex
                form.instance.material = fil.get("material", "")
                # Keep the mapping's cached color in sync
                SpoolmanFilamentMapping.objects.filter(
                    spoolman_filament_id=filament_id,
                ).update(spoolman_color_hex=color_hex)


class PartDetailView(LoginRequiredMixin, DetailView):
    """Show details of a single part."""

    model = Part
    template_name = "core/part_detail.html"
    context_object_name = "part"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        part = self.object
        context["ancestors"] = part.project.get_ancestors()

        context["print_presets"] = OrcaPrintPreset.objects.filter(
            state=OrcaPrintPreset.STATE_RESOLVED,
            instantiation=True,
        )

        # Draft jobs the user can add this part to — only those whose
        # existing parts share the same effective preset and filament.
        effective_preset_id = part.effective_print_preset_id
        effective_filament_id = part.spoolman_filament_id

        draft_jobs = PrintJob.objects.filter(
            status=PrintJob.STATUS_DRAFT,
        ).prefetch_related("job_parts__part__project")

        compatible_jobs = []
        for job in draft_jobs:
            job_parts = job.job_parts.all()
            if not job_parts:
                # Empty job is always compatible
                compatible_jobs.append(job)
                continue
            compatible = all(
                jp.part.effective_print_preset_id == effective_preset_id
                and jp.part.spoolman_filament_id == effective_filament_id
                for jp in job_parts
            )
            if compatible:
                compatible_jobs.append(job)

        context["draft_jobs"] = compatible_jobs

        # Spoolman filament name lookup
        spoolman_filament_name = ""
        if part.spoolman_filament_id:
            mapping = SpoolmanFilamentMapping.objects.filter(
                spoolman_filament_id=part.spoolman_filament_id,
            ).first()
            if mapping and mapping.spoolman_filament_name:
                spoolman_filament_name = mapping.spoolman_filament_name
        context["spoolman_filament_name"] = spoolman_filament_name

        return context


class PartCreateView(_SpoolmanFilamentMixin, ProjectManageMixin, CreateView):
    """Create a new part within a project."""

    model = Part
    form_class = PartForm
    template_name = "core/part_form.html"

    def dispatch(self, request, *args, **kwargs):
        self.project = get_object_or_404(Project, pk=kwargs["project_pk"])
        return super().dispatch(request, *args, **kwargs)

    def get_form(self, form_class=None):
        form = super().get_form(form_class)
        # Print presets
        form.fields["print_preset"].queryset = OrcaPrintPreset.objects.filter(
            state=OrcaPrintPreset.STATE_RESOLVED,
            instantiation=True,
        )
        # Spoolman filament choices
        self.populate_spoolman_filaments(form)
        return form

    def form_valid(self, form):
        form.instance.project = self.project
        self.apply_spoolman_filament(form)

        response = super().form_valid(form)
        messages.success(self.request, "Part added successfully.")

        # Trigger background estimation slicing
        _trigger_part_estimation(self.object)

        if "_save_and_add_another" in self.request.POST:
            return redirect(reverse("core:part_create", kwargs={"project_pk": self.project.pk}))
        return response

    def get_success_url(self) -> str:
        """Redirect to the newly created part's detail page."""
        return reverse("core:part_detail", kwargs={"pk": self.object.pk})

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["project"] = self.project
        context["ancestors"] = self.project.get_ancestors()
        context["spoolman_configured"] = self._spoolman_configured
        context["spoolman_colors"] = self._spoolman_colors
        context["spoolman_colors_json"] = json.dumps(self._spoolman_colors)
        return context


class PartUpdateView(_SpoolmanFilamentMixin, ProjectManageMixin, UpdateView):
    """Update an existing part."""

    model = Part
    form_class = PartForm
    template_name = "core/part_form.html"

    def get_form(self, form_class=None):
        form = super().get_form(form_class)
        form.fields["print_preset"].queryset = OrcaPrintPreset.objects.filter(
            state=OrcaPrintPreset.STATE_RESOLVED,
            instantiation=True,
        )
        # Spoolman filament choices
        self.populate_spoolman_filaments(form)
        return form

    def get_success_url(self):
        return reverse_lazy("core:part_detail", kwargs={"pk": self.object.pk})

    def form_valid(self, form):
        self.apply_spoolman_filament(form)

        # Check if STL or preset changed — re-estimate if so
        stl_changed = "stl_file" in form.changed_data
        preset_changed = "print_preset" in form.changed_data
        filament_changed = "spoolman_filament_id" in form.changed_data
        needs_re_estimate = stl_changed or preset_changed or filament_changed

        messages.success(self.request, "Part updated successfully.")
        response = super().form_valid(form)

        if needs_re_estimate:
            # Clear old estimates so new ones are written
            Part.objects.filter(pk=self.object.pk).update(
                filament_used_grams=None,
                filament_used_meters=None,
                estimated_print_time=None,
                estimation_status=Part.ESTIMATION_NONE,
                estimation_error="",
            )
            _trigger_part_estimation(self.object)

        return response

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["ancestors"] = self.object.project.get_ancestors()
        context["spoolman_configured"] = self._spoolman_configured
        context["spoolman_colors"] = self._spoolman_colors
        context["spoolman_colors_json"] = json.dumps(self._spoolman_colors)
        return context


class PartDeleteView(ProjectManageMixin, DeleteView):
    """Delete a part."""

    model = Part
    template_name = "core/part_confirm_delete.html"

    def get_context_data(self, **kwargs):
        """Add breadcrumb ancestors to template context."""
        context = super().get_context_data(**kwargs)
        context["ancestors"] = self.object.project.get_ancestors()
        return context

    def get_success_url(self):
        return reverse_lazy("core:project_detail", kwargs={"pk": self.object.project.pk})

    def form_valid(self, form):
        messages.success(self.request, "Part deleted.")
        return super().form_valid(form)


class PartReEstimateView(ProjectManageMixin, View):
    """Re-trigger estimation for a single part.

    Clears existing estimates and starts a background estimation thread.
    Redirects back to the part detail page.
    """

    http_method_names = ["post"]

    def post(self, request: HttpRequest, pk: int) -> HttpResponse:
        """Handle POST request to re-estimate a single part.

        Args:
            request: The incoming HTTP request.
            pk: Primary key of the Part to re-estimate.

        Returns:
            Redirect to the part detail page.
        """
        part = get_object_or_404(Part, pk=pk)

        if not part.stl_file:
            messages.warning(request, "No STL file — cannot estimate.")
            return redirect("core:part_detail", pk=part.pk)

        preset = part.effective_print_preset
        if not preset:
            messages.warning(request, "No print preset configured — cannot estimate.")
            return redirect("core:part_detail", pk=part.pk)

        Part.objects.filter(pk=part.pk).update(
            filament_used_grams=None,
            filament_used_meters=None,
            estimated_print_time=None,
            estimation_status=Part.ESTIMATION_NONE,
            estimation_error="",
        )
        _trigger_part_estimation(part)
        messages.info(request, f"Re-estimation started for '{part.name}'.")
        return redirect("core:part_detail", pk=part.pk)


class ProjectReEstimateView(ProjectManageMixin, View):
    """Re-trigger estimation for all parts in a project (including sub-projects).

    Clears existing estimates and starts background estimation threads
    for every part that has an STL file and a print preset.
    Redirects back to the project detail page.
    """

    http_method_names = ["post"]

    def post(self, request: HttpRequest, pk: int) -> HttpResponse:
        """Handle POST request to re-estimate all parts in a project.

        Args:
            request: The incoming HTTP request.
            pk: Primary key of the Project to re-estimate.

        Returns:
            Redirect to the project detail page.
        """
        project = get_object_or_404(Project, pk=pk)
        parts = [p for p, _mult in project._collect_parts_with_multiplier()]

        count = 0
        for part in parts:
            if not part.stl_file:
                continue
            preset = part.effective_print_preset
            if not preset:
                continue

            Part.objects.filter(pk=part.pk).update(
                filament_used_grams=None,
                filament_used_meters=None,
                estimated_print_time=None,
                estimation_status=Part.ESTIMATION_NONE,
                estimation_error="",
            )
            _trigger_part_estimation(part)
            count += 1

        if count:
            messages.info(request, f"Re-estimation started for {count} part(s).")
        else:
            messages.warning(request, "No parts eligible for estimation.")
        return redirect("core:project_detail", pk=project.pk)


# ---------------------------------------------------------------------------
# Printer Profile views
# ---------------------------------------------------------------------------


class PrinterProfileListView(LoginRequiredMixin, ListView):
    """List all printer profiles."""

    model = PrinterProfile
    template_name = "core/printerprofile_list.html"
    context_object_name = "printer_profiles"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        # Check real Moonraker connectivity for each printer
        statuses = {}
        for printer in context["printer_profiles"]:
            if printer.moonraker_url:
                try:
                    client = MoonrakerClient(printer.moonraker_url, printer.moonraker_api_key)
                    client.get_printer_status()
                    statuses[printer.pk] = "online"
                except MoonrakerError:
                    statuses[printer.pk] = "offline"
            else:
                statuses[printer.pk] = "unconfigured"
        context["printer_statuses"] = statuses
        return context


class PrinterProfileCreateView(PrinterManageMixin, CreateView):
    """Create a new printer profile."""

    model = PrinterProfile
    form_class = PrinterProfileForm
    template_name = "core/printerprofile_form.html"
    success_url = reverse_lazy("core:printerprofile_list")

    def get_form(self, form_class=None):
        form = super().get_form(form_class)
        form.fields["orca_machine_profile"].queryset = OrcaMachineProfile.objects.filter(
            state=OrcaMachineProfile.STATE_RESOLVED,
            instantiation=True,
        )
        return form

    def form_valid(self, form):
        form.instance.created_by = self.request.user
        response = super().form_valid(form)
        messages.success(self.request, "Printer profile created.")
        if "_save_and_add_another" in self.request.POST:
            return redirect(reverse("core:printerprofile_create"))
        return response


class PrinterProfileUpdateView(PrinterManageMixin, UpdateView):
    """Update an existing printer profile."""

    model = PrinterProfile
    form_class = PrinterProfileForm
    template_name = "core/printerprofile_form.html"
    success_url = reverse_lazy("core:printerprofile_list")

    def get_form(self, form_class=None):
        form = super().get_form(form_class)
        form.fields["orca_machine_profile"].queryset = OrcaMachineProfile.objects.filter(
            state=OrcaMachineProfile.STATE_RESOLVED,
            instantiation=True,
        )
        return form

    def form_valid(self, form):
        messages.success(self.request, "Printer profile updated.")
        return super().form_valid(form)


class PrinterProfileDeleteView(PrinterManageMixin, DeleteView):
    """Delete a printer profile."""

    model = PrinterProfile
    template_name = "core/printerprofile_confirm_delete.html"
    success_url = reverse_lazy("core:printerprofile_list")

    def form_valid(self, form):
        messages.success(self.request, "Printer profile deleted.")
        return super().form_valid(form)


# ---------------------------------------------------------------------------
# Print Job views
# ---------------------------------------------------------------------------


class PrintJobListView(LoginRequiredMixin, ListView):
    """List all print jobs."""

    model = PrintJob
    template_name = "core/printjob_list.html"
    context_object_name = "print_jobs"

    def get_queryset(self):
        return PrintJob.objects.prefetch_related("job_parts__part", "plates")


class PrintJobCreateView(RoleRequiredMixin, CreateView):
    """Create a new draft print job.

    The user provides a name and machine profile.  Parts are added
    separately via :class:`AddPartToJobView`.
    """

    permission_required = "core.add_printjob"

    model = PrintJob
    form_class = PrintJobForm
    template_name = "core/printjob_form.html"

    def get_form(self, form_class=None):
        form = super().get_form(form_class)
        form.fields["machine_profile"].queryset = OrcaMachineProfile.objects.filter(
            state=OrcaMachineProfile.STATE_RESOLVED,
            instantiation=True,
        )
        return form

    def form_valid(self, form):
        form.instance.created_by = self.request.user
        form.instance.status = PrintJob.STATUS_DRAFT
        response = super().form_valid(form)
        messages.success(self.request, f"Draft job '{self.object}' created.")
        if "_save_and_add_another" in self.request.POST:
            return redirect(reverse("core:printjob_create"))
        return response

    def get_success_url(self):
        return reverse("core:printjob_detail", kwargs={"pk": self.object.pk})


class PrintJobDetailView(LoginRequiredMixin, DetailView):
    """Show details of a print job including its parts and plates."""

    model = PrintJob
    template_name = "core/printjob_detail.html"
    context_object_name = "job"

    def get_queryset(self):
        return PrintJob.objects.prefetch_related("job_parts__part__project", "plates")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        job = self.object
        context["job_parts"] = job.job_parts.select_related("part__project")
        context["plates"] = job.plates.all()
        has_parts = job.job_parts.exists()
        parts_valid = has_parts and all(jp.part.stl_file for jp in job.job_parts.select_related("part"))
        has_machine = job.machine_profile is not None
        context["can_slice"] = job.status == PrintJob.STATUS_DRAFT and has_machine and parts_valid
        context["can_reslice"] = (
            job.status in (PrintJob.STATUS_SLICED, PrintJob.STATUS_FAILED) and has_machine and parts_valid
        )
        context["missing_machine_profile"] = job.status == PrintJob.STATUS_DRAFT and not has_machine

        # Resolve effective print preset and filament profile from first part
        first_jp = job.job_parts.select_related(
            "part__print_preset",
            "part__project__default_print_preset",
            "part__project__parent__default_print_preset",
        ).first()
        if first_jp:
            context["effective_print_preset"] = first_jp.part.effective_print_preset
            if first_jp.part.spoolman_filament_id:
                mapping = (
                    SpoolmanFilamentMapping.objects.filter(
                        spoolman_filament_id=first_jp.part.spoolman_filament_id,
                    )
                    .select_related("orca_filament_profile")
                    .first()
                )
                if mapping:
                    context["spoolman_filament_name"] = mapping.spoolman_filament_name or ""
                    context["filament_profile"] = (
                        mapping.orca_filament_profile if mapping.orca_filament_profile else None
                    )

        return context


class PrintJobUpdateView(RoleRequiredMixin, UpdateView):
    """Update an existing print job (only in draft status)."""

    permission_required = "core.change_printjob"

    model = PrintJob
    form_class = PrintJobUpdateForm
    template_name = "core/printjob_form.html"

    def get_form(self, form_class=None):
        form = super().get_form(form_class)
        form.fields["machine_profile"].queryset = OrcaMachineProfile.objects.filter(
            state=OrcaMachineProfile.STATE_RESOLVED,
            instantiation=True,
        )
        return form

    def form_valid(self, form):
        messages.success(self.request, "Print job updated.")
        return super().form_valid(form)

    def get_success_url(self):
        return reverse("core:printjob_detail", kwargs={"pk": self.object.pk})


class PrintJobDeleteView(RoleRequiredMixin, DeleteView):
    """Delete a print job.

    Related queue entries and plates are deleted automatically via CASCADE.
    """

    permission_required = "core.delete_printjob"

    model = PrintJob
    template_name = "core/printjob_confirm_delete.html"
    success_url = reverse_lazy("core:printjob_list")

    def form_valid(self, form):
        job = self.get_object()
        messages.success(
            self.request,
            f"Print job '{job}' deleted (including plates and queue entries).",
        )
        return super().form_valid(form)


# ---------------------------------------------------------------------------
# Add/Remove Parts to/from PrintJob
# ---------------------------------------------------------------------------


class AddPartToJobView(RoleRequiredMixin, View):
    """Add a part to an existing draft job or create a new one.

    POST parameters:
        - job: PK of existing draft job, or empty for new job.
        - quantity: Number of copies to add.
    """

    permission_required = "core.add_printjob"

    def post(self, request: HttpRequest, part_pk: int) -> HttpResponse:
        """Handle adding a part to a print job."""
        part = get_object_or_404(Part, pk=part_pk)

        if not part.stl_file:
            messages.error(request, f"Part '{part.name}' has no STL file.")
            return redirect("core:part_detail", pk=part.pk)

        form = AddPartToJobForm(request.POST, user=request.user)
        if not form.is_valid():
            messages.error(request, "Invalid form data.")
            return redirect("core:part_detail", pk=part.pk)

        job = form.cleaned_data.get("job")
        quantity = form.cleaned_data["quantity"]

        if not job:
            # Create a new draft job
            job = PrintJob.objects.create(
                name=f"Job with {part.name}",
                status=PrintJob.STATUS_DRAFT,
                created_by=request.user,
            )
            messages.info(request, f"New draft job '{job}' created.")

        if job.status != PrintJob.STATUS_DRAFT:
            messages.error(request, "Can only add parts to draft jobs.")
            return redirect("core:part_detail", pk=part.pk)

        # Validate preset/filament compatibility with existing parts
        existing_parts = job.job_parts.select_related("part__project").all()
        if existing_parts:
            effective_preset_id = part.effective_print_preset_id
            effective_filament_id = part.spoolman_filament_id
            incompatible = any(
                jp.part.effective_print_preset_id != effective_preset_id
                or jp.part.spoolman_filament_id != effective_filament_id
                for jp in existing_parts
            )
            if incompatible:
                messages.error(
                    request,
                    f"Part '{part.name}' has a different preset or filament "
                    f"than the existing parts in '{job}'. "
                    f"All parts in a job must use the same preset and filament.",
                )
                return redirect("core:part_detail", pk=part.pk)

        # Add or update the part in the job
        job_part, created = PrintJobPart.objects.get_or_create(
            print_job=job,
            part=part,
            defaults={"quantity": quantity},
        )
        if not created:
            job_part.quantity += quantity
            job_part.save(update_fields=["quantity"])
            messages.success(
                request,
                f"Updated '{part.name}' quantity to {job_part.quantity} in '{job}'.",
            )
        else:
            messages.success(request, f"Added {quantity}× '{part.name}' to '{job}'.")

        return redirect("core:printjob_detail", pk=job.pk)


class RemovePartFromJobView(RoleRequiredMixin, View):
    """Remove a part from a draft print job."""

    permission_required = "core.change_printjob"

    def post(self, request: HttpRequest, job_pk: int, job_part_pk: int) -> HttpResponse:
        """Remove a PrintJobPart entry."""
        job = get_object_or_404(PrintJob, pk=job_pk)
        if job.status != PrintJob.STATUS_DRAFT:
            messages.error(request, "Can only remove parts from draft jobs.")
            return redirect("core:printjob_detail", pk=job.pk)

        job_part = get_object_or_404(PrintJobPart, pk=job_part_pk, print_job=job)
        part_name = job_part.part.name
        job_part.delete()
        messages.success(request, f"Removed '{part_name}' from job.")
        return redirect("core:printjob_detail", pk=job.pk)


class CreateJobsFromProjectView(RoleRequiredMixin, View):
    """Bulk-create draft print jobs from all eligible parts in a project.

    Groups parts by ``(effective_print_preset_id, spoolman_filament_id)``
    so that each job contains only compatible parts.  Parts without an
    STL file or with ``remaining_quantity == 0`` are skipped.

    Each job is named ``"<Project> — <Material / Preset>"``.
    """

    permission_required = "core.add_printjob"

    def post(self, request: HttpRequest, pk: int) -> HttpResponse:
        """Create one draft job per preset/filament group."""
        project = get_object_or_404(Project, pk=pk)

        parts = list(project.parts.select_related("project").all())
        eligible = [p for p in parts if p.stl_file and p.remaining_quantity > 0]

        if not eligible:
            messages.warning(request, "No eligible parts found (all printed or missing STL).")
            return redirect("core:project_detail", pk=project.pk)

        # Group by (effective_print_preset_id, spoolman_filament_id)
        groups: dict[tuple[int | None, int | None], list[Part]] = defaultdict(list)
        for part in eligible:
            key = (part.effective_print_preset_id, part.spoolman_filament_id)
            groups[key].append(part)

        jobs_created = 0
        parts_added = 0

        for (_preset_id, filament_id), group_parts in groups.items():
            # Build a descriptive job name
            label_parts: list[str] = [project.name]
            if filament_id:
                # Use filament name from Spoolman mapping, fall back to material
                mapping = SpoolmanFilamentMapping.objects.filter(
                    spoolman_filament_id=filament_id,
                ).first()
                filament_label = (
                    (mapping.spoolman_filament_name if mapping and mapping.spoolman_filament_name else None)
                    or next((p.material for p in group_parts if p.material), None)
                    or f"Filament #{filament_id}"
                )
                label_parts.append(filament_label)
            if len(groups) > 1:
                label_parts.append(f"Group {jobs_created + 1}")

            job_name = " — ".join(label_parts)

            job = PrintJob.objects.create(
                name=job_name,
                status=PrintJob.STATUS_DRAFT,
                created_by=request.user,
            )

            for part in group_parts:
                PrintJobPart.objects.create(
                    print_job=job,
                    part=part,
                    quantity=part.remaining_quantity,
                )
                parts_added += 1

            jobs_created += 1

        if jobs_created == 1:
            job_pk = PrintJob.objects.filter(created_by=request.user).order_by("-created_at").first().pk
            messages.success(
                request,
                f"Created draft job with {parts_added} part(s).",
            )
            return redirect("core:printjob_detail", pk=job_pk)

        messages.success(
            request,
            f"Created {jobs_created} draft jobs with {parts_added} part(s) total.",
        )
        return redirect("core:project_detail", pk=project.pk)


# ---------------------------------------------------------------------------
# Print Job Slicing
# ---------------------------------------------------------------------------


class PrintJobSliceView(RoleRequiredMixin, View):
    """Trigger slicing for a draft print job.

    Collects all STL files from the job's parts, creates a 3MF bundle,
    and starts background slicing via OrcaSlicer.
    """

    permission_required = "core.change_printjob"

    def post(self, request: HttpRequest, pk: int) -> HttpResponse:
        """Validate job and queue for background slicing."""
        job = get_object_or_404(
            PrintJob.objects.prefetch_related("job_parts__part"),
            pk=pk,
        )

        allowed = {
            PrintJob.STATUS_DRAFT,
            PrintJob.STATUS_SLICED,
            PrintJob.STATUS_FAILED,
        }
        if job.status not in allowed:
            messages.error(request, "This job cannot be (re-)sliced right now.")
            return redirect("core:printjob_detail", pk=pk)

        if not job.machine_profile:
            messages.error(
                request,
                "Please set a machine profile on the job before slicing.",
            )
            return redirect("core:printjob_detail", pk=pk)

        job_parts = job.job_parts.select_related("part")
        if not job_parts.exists():
            messages.error(request, "Job has no parts to slice.")
            return redirect("core:printjob_detail", pk=pk)

        # Validate all parts have STL files
        missing_stl = [jp.part.name for jp in job_parts if not jp.part.stl_file]
        if missing_stl:
            messages.error(
                request,
                f"Parts without STL files: {', '.join(missing_stl)}",
            )
            return redirect("core:printjob_detail", pk=pk)

        # Queue for slicing via the unified OrcaSlicer worker
        job.status = PrintJob.STATUS_PENDING
        job.slicing_error = ""
        job.save(update_fields=["status", "slicing_error"])

        # Delete any existing plates from a previous slice attempt
        job.plates.all().delete()

        _start_orcaslicer_worker()
        logger.info("Queued slicing for job %s (%s)", job.pk, job)

        messages.info(
            request,
            f"Slicing queued for job '{job}' ({job.total_part_count} parts).",
        )
        return redirect("core:printjob_detail", pk=pk)


# ---------------------------------------------------------------------------
# OrcaSlicer Profile views
# ---------------------------------------------------------------------------


# ── OrcaSlicer Machine Profiles (structured import with inheritance) ─────


class OrcaMachineProfileListView(LoginRequiredMixin, ListView):
    """List all OrcaSlicer machine profiles."""

    model = OrcaMachineProfile
    template_name = "core/orcamachineprofile_list.html"
    context_object_name = "profiles"


class OrcaMachineProfileDetailView(LoginRequiredMixin, DetailView):
    """Show details of a resolved OrcaSlicer machine profile."""

    model = OrcaMachineProfile
    template_name = "core/orcamachineprofile_detail.html"
    context_object_name = "profile"

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        """Add resolved settings to context for display."""
        context = super().get_context_data(**kwargs)
        profile = self.object
        if profile.is_resolved:
            from .services.profile_import import MACHINE_FIELD_MAP

            key_settings = []
            for field_name, (orca_key, _type_tag) in MACHINE_FIELD_MAP.items():
                value = profile.settings.get(field_name)
                if value is not None and value != "" and value != []:
                    key_settings.append(
                        {
                            "key": orca_key,
                            "value": value,
                            "field_name": field_name,
                        }
                    )
            context["key_settings"] = key_settings
            known_fields = set(MACHINE_FIELD_MAP.keys())
            context["extra_settings_count"] = len([k for k in profile.settings if k not in known_fields])
            context["extra_settings"] = {k: v for k, v in profile.settings.items() if k not in known_fields}
        return context


class OrcaMachineProfileImportView(OrcaProfileManageMixin, View):
    """Import an OrcaSlicer machine profile from a JSON file.

    Handles the inheritance chain: if the profile's parent is missing,
    the user is informed which file to upload next.
    """

    template_name = "core/orcamachineprofile_import.html"

    def get(self, request: HttpRequest) -> HttpResponse:
        """Show the import form."""
        from django.template.response import TemplateResponse

        form = OrcaMachineProfileImportForm()
        pending = OrcaMachineProfile.objects.filter(
            state=OrcaMachineProfile.STATE_PENDING,
        )
        return TemplateResponse(
            request,
            self.template_name,
            {"form": form, "pending_profiles": pending},
        )

    def post(self, request: HttpRequest) -> HttpResponse:
        """Process an uploaded profile JSON file."""
        from django.template.response import TemplateResponse

        from .services.profile_import import import_machine_profile_json

        form = OrcaMachineProfileImportForm(request.POST, request.FILES)
        if not form.is_valid():
            pending = OrcaMachineProfile.objects.filter(
                state=OrcaMachineProfile.STATE_PENDING,
            )
            return TemplateResponse(
                request,
                self.template_name,
                {"form": form, "pending_profiles": pending},
            )

        json_data = form.cleaned_data["profile_file"]
        display_name = form.cleaned_data.get("display_name") or None

        try:
            result = import_machine_profile_json(json_data, request.user, display_name)
        except ValueError as exc:
            form.add_error(None, str(exc))
            pending = OrcaMachineProfile.objects.filter(
                state=OrcaMachineProfile.STATE_PENDING,
            )
            return TemplateResponse(
                request,
                self.template_name,
                {"form": form, "pending_profiles": pending},
            )

        if result.is_resolved:
            msg = f"Profile '{result.profile.name}' imported and resolved."
            if result.auto_resolved_children:
                names = ", ".join(result.auto_resolved_children)
                msg += f" Additionally auto-resolved: {names}."
            messages.success(request, msg)
            return redirect("core:orcamachineprofile_detail", pk=result.profile.pk)
        else:
            messages.warning(
                request,
                f"Profile '{result.profile.name}' saved as pending. "
                f"Please upload the parent profile '{result.missing_parent}' next.",
            )
            return redirect("core:orcamachineprofile_import")


class OrcaMachineProfileDeleteView(OrcaProfileManageMixin, DeleteView):
    """Delete an OrcaSlicer machine profile."""

    model = OrcaMachineProfile
    template_name = "core/orcamachineprofile_confirm_delete.html"
    success_url = reverse_lazy("core:orcamachineprofile_list")

    def form_valid(self, form):
        messages.success(self.request, "Machine profile deleted.")
        return super().form_valid(form)


# Filament Profiles
class OrcaFilamentProfileListView(LoginRequiredMixin, ListView):
    """List all filament profiles."""

    model = OrcaFilamentProfile
    template_name = "core/orcafilamentprofile_list.html"
    context_object_name = "filament_profiles"


class OrcaFilamentProfileDetailView(LoginRequiredMixin, DetailView):
    """Show details of a resolved OrcaSlicer filament profile."""

    model = OrcaFilamentProfile
    template_name = "core/orcafilamentprofile_detail.html"
    context_object_name = "profile"

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        """Add resolved settings to context for display."""
        context = super().get_context_data(**kwargs)
        profile = self.object
        if profile.is_resolved:
            from .services.profile_import import FILAMENT_FIELD_MAP

            key_settings = []
            for field_name, (orca_key, _type_tag) in FILAMENT_FIELD_MAP.items():
                value = profile.settings.get(field_name)
                if value is not None and value != "" and value != []:
                    key_settings.append(
                        {
                            "key": orca_key,
                            "value": value,
                            "field_name": field_name,
                        }
                    )
            context["key_settings"] = key_settings
            known_fields = set(FILAMENT_FIELD_MAP.keys())
            context["extra_settings_count"] = len([k for k in profile.settings if k not in known_fields])
            context["extra_settings"] = {k: v for k, v in profile.settings.items() if k not in known_fields}
        return context


class OrcaFilamentProfileImportView(OrcaProfileManageMixin, View):
    """Import an OrcaSlicer filament profile from a JSON file.

    Handles the inheritance chain: if the profile's parent is missing,
    the user is informed which file to upload next.
    """

    template_name = "core/orcafilamentprofile_import.html"

    def get(self, request: HttpRequest) -> HttpResponse:
        """Show the import form."""
        from django.template.response import TemplateResponse

        form = OrcaFilamentProfileImportForm()
        pending = OrcaFilamentProfile.objects.filter(
            state=OrcaFilamentProfile.STATE_PENDING,
        )
        return TemplateResponse(
            request,
            self.template_name,
            {"form": form, "pending_profiles": pending},
        )

    def post(self, request: HttpRequest) -> HttpResponse:
        """Process an uploaded filament profile JSON file."""
        from django.template.response import TemplateResponse

        from .services.profile_import import import_filament_profile_json

        form = OrcaFilamentProfileImportForm(request.POST, request.FILES)
        if not form.is_valid():
            pending = OrcaFilamentProfile.objects.filter(
                state=OrcaFilamentProfile.STATE_PENDING,
            )
            return TemplateResponse(
                request,
                self.template_name,
                {"form": form, "pending_profiles": pending},
            )

        json_data = form.cleaned_data["profile_file"]
        display_name = form.cleaned_data.get("display_name") or None

        try:
            result = import_filament_profile_json(json_data, request.user, display_name)
        except ValueError as exc:
            form.add_error(None, str(exc))
            pending = OrcaFilamentProfile.objects.filter(
                state=OrcaFilamentProfile.STATE_PENDING,
            )
            return TemplateResponse(
                request,
                self.template_name,
                {"form": form, "pending_profiles": pending},
            )

        if result.is_resolved:
            msg = f"Filament profile '{result.profile.name}' imported and resolved."
            if result.auto_resolved_children:
                names = ", ".join(result.auto_resolved_children)
                msg += f" Additionally auto-resolved: {names}."
            messages.success(request, msg)
            return redirect("core:orcafilamentprofile_detail", pk=result.profile.pk)
        else:
            messages.warning(
                request,
                f"Filament profile '{result.profile.name}' saved as pending. "
                f"Please upload the parent profile '{result.missing_parent}' next.",
            )
            return redirect("core:orcafilamentprofile_import")


class OrcaFilamentProfileDeleteView(OrcaProfileManageMixin, DeleteView):
    """Delete an OrcaSlicer filament profile."""

    model = OrcaFilamentProfile
    template_name = "core/orcafilamentprofile_confirm_delete.html"
    success_url = reverse_lazy("core:orcafilamentprofile_list")

    def form_valid(self, form):
        messages.success(self.request, "Filament profile deleted.")
        return super().form_valid(form)


# Print Presets
class OrcaPrintPresetListView(LoginRequiredMixin, ListView):
    """List all print presets."""

    model = OrcaPrintPreset
    template_name = "core/orcaprintpreset_list.html"
    context_object_name = "print_presets"


class OrcaPrintPresetDetailView(LoginRequiredMixin, DetailView):
    """Show details of a resolved OrcaSlicer process profile."""

    model = OrcaPrintPreset
    template_name = "core/orcaprintpreset_detail.html"
    context_object_name = "profile"

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        """Add resolved settings to context for display."""
        context = super().get_context_data(**kwargs)
        profile = self.object
        if profile.is_resolved:
            from .services.profile_import import PROCESS_FIELD_MAP

            key_settings = []
            for field_name, (orca_key, _type_tag) in PROCESS_FIELD_MAP.items():
                value = profile.settings.get(field_name)
                if value is not None and value != "" and value != []:
                    key_settings.append(
                        {
                            "key": orca_key,
                            "value": value,
                            "field_name": field_name,
                        }
                    )
            context["key_settings"] = key_settings
            known_fields = set(PROCESS_FIELD_MAP.keys())
            context["extra_settings_count"] = len([k for k in profile.settings if k not in known_fields])
            context["extra_settings"] = {k: v for k, v in profile.settings.items() if k not in known_fields}
        return context


class OrcaPrintPresetImportView(OrcaProfileManageMixin, View):
    """Import an OrcaSlicer process profile from a JSON file.

    Handles the inheritance chain: if the profile's parent is missing,
    the user is informed which file to upload next.
    """

    template_name = "core/orcaprintpreset_import.html"

    def get(self, request: HttpRequest) -> HttpResponse:
        """Show the import form."""
        from django.template.response import TemplateResponse

        form = OrcaPrintPresetImportForm()
        pending = OrcaPrintPreset.objects.filter(
            state=OrcaPrintPreset.STATE_PENDING,
        )
        return TemplateResponse(
            request,
            self.template_name,
            {"form": form, "pending_profiles": pending},
        )

    def post(self, request: HttpRequest) -> HttpResponse:
        """Process an uploaded process profile JSON file."""
        from django.template.response import TemplateResponse

        from .services.profile_import import import_process_profile_json

        form = OrcaPrintPresetImportForm(request.POST, request.FILES)
        if not form.is_valid():
            pending = OrcaPrintPreset.objects.filter(
                state=OrcaPrintPreset.STATE_PENDING,
            )
            return TemplateResponse(
                request,
                self.template_name,
                {"form": form, "pending_profiles": pending},
            )

        json_data = form.cleaned_data["profile_file"]
        display_name = form.cleaned_data.get("display_name") or None

        try:
            result = import_process_profile_json(json_data, request.user, display_name)
        except ValueError as exc:
            form.add_error(None, str(exc))
            pending = OrcaPrintPreset.objects.filter(
                state=OrcaPrintPreset.STATE_PENDING,
            )
            return TemplateResponse(
                request,
                self.template_name,
                {"form": form, "pending_profiles": pending},
            )

        if result.is_resolved:
            msg = f"Process profile '{result.profile.name}' imported and resolved."
            if result.auto_resolved_children:
                names = ", ".join(result.auto_resolved_children)
                msg += f" Additionally auto-resolved: {names}."
            messages.success(request, msg)
            return redirect("core:orcaprintpreset_detail", pk=result.profile.pk)
        else:
            messages.warning(
                request,
                f"Process profile '{result.profile.name}' saved as pending. "
                f"Please upload the parent profile '{result.missing_parent}' next.",
            )
            return redirect("core:orcaprintpreset_import")


class OrcaPrintPresetDeleteView(OrcaProfileManageMixin, DeleteView):
    """Delete an OrcaSlicer process profile."""

    model = OrcaPrintPreset
    template_name = "core/orcaprintpreset_confirm_delete.html"
    context_object_name = "profile"
    success_url = reverse_lazy("core:orcaprintpreset_list")

    def form_valid(self, form):
        messages.success(self.request, "Process profile deleted.")
        return super().form_valid(form)


# ---------------------------------------------------------------------------
# Print Job views
# ---------------------------------------------------------------------------


def _slice_job_in_background(job_pk: int) -> None:
    """Slice a PrintJob via OrcaSlicer API.

    Called by the unified OrcaSlicer worker.  Builds the 3MF bundle,
    resolves profiles, calls the slicer, and creates ``PrintJobPlate``
    objects for each plate.  On success the job transitions to *sliced*.

    The caller (worker loop) is responsible for DB connection lifecycle.

    Args:
        job_pk: Primary key of the PrintJob to slice.
    """
    try:
        # Status already set to SLICING by the worker loop (atomic claim)
        job = PrintJob.objects.prefetch_related("job_parts__part__project").get(pk=job_pk)

        job_parts = job.job_parts.select_related(
            "part__project__default_print_preset",
            "part__project__parent__default_print_preset",
            "part__print_preset",
        )

        # Build 3MF bundle from job parts
        stl_list = [(jp.part.stl_file.path, jp.quantity) for jp in job_parts]
        threemf_content = create_3mf_bundle(stl_list)

        # Resolve profiles
        machine_profile = job.machine_profile
        first_part = job_parts.first().part
        filament_profile = None
        if first_part.spoolman_filament_id:
            mapping = (
                SpoolmanFilamentMapping.objects.filter(
                    spoolman_filament_id=first_part.spoolman_filament_id,
                )
                .select_related("orca_filament_profile")
                .first()
            )
            if mapping and mapping.orca_filament_profile:
                filament_profile = mapping.orca_filament_profile

        print_preset = first_part.effective_print_preset

        slice_kwargs = _build_slicer_kwargs(
            machine_profile=machine_profile,
            print_preset=print_preset,
            filament_profile=filament_profile,
        )

        slicer = OrcaSlicerAPIClient(settings.ORCASLICER_API_URL)
        result = slicer.slice_bundle(threemf_content, **slice_kwargs)

        # Re-fetch job to avoid stale state
        job = PrintJob.objects.get(pk=job_pk)

        media_root = Path(settings.MEDIA_ROOT).resolve()
        gcode_dir = media_root / "gcode_jobs"
        gcode_dir.mkdir(parents=True, exist_ok=True)

        for plate_result in result.plates:
            gcode_filename = f"job_{job_pk}_plate_{plate_result.plate_number}.gcode"
            gcode_path = gcode_dir / gcode_filename
            if not gcode_path.resolve().is_relative_to(media_root):
                raise OrcaSlicerError("Output path is outside MEDIA_ROOT")
            gcode_path.write_bytes(plate_result.gcode_content)
            relative = gcode_path.resolve().relative_to(media_root)

            plate_filament = plate_result.filament_used_grams
            plate_time = plate_result.print_time_seconds

            plate = PrintJobPlate(
                print_job=job,
                plate_number=plate_result.plate_number,
                status=PrintJobPlate.STATUS_WAITING,
            )
            plate.gcode_file.name = str(relative)
            if plate_filament is not None:
                plate.filament_used_grams = plate_filament
            if plate_time is not None:
                plate.print_time_estimate = timedelta(seconds=plate_time)

            # Save thumbnail if extracted from G-code or ZIP
            if plate_result.thumbnail_png:
                from django.core.files.base import ContentFile

                thumb_filename = f"job_{job_pk}_plate_{plate_result.plate_number}.png"
                plate.thumbnail.save(
                    thumb_filename,
                    ContentFile(plate_result.thumbnail_png),
                    save=False,
                )
                logger.debug(
                    "Saved thumbnail for plate %d (%d bytes)",
                    plate_result.plate_number,
                    len(plate_result.thumbnail_png),
                )

            plate.save()

        # Update aggregate stats on the job
        job.filament_used_grams = result.total_filament_grams
        job.print_time_estimate = (
            timedelta(seconds=result.total_print_time_seconds) if result.total_print_time_seconds else None
        )
        job.status = PrintJob.STATUS_SLICED
        job.slicing_error = ""
        job.save()
        logger.info(
            "Background slicing completed for job %s (%d plates)",
            job_pk,
            len(result.plates),
        )

        # Back-fill Part estimates with per-copy values.
        job_parts_list = list(job.job_parts.select_related("part"))
        total_copies = sum(jp.quantity for jp in job_parts_list)
        if total_copies > 0:
            total_g = result.total_filament_grams
            total_mm = result.total_filament_mm
            total_time = result.total_print_time_seconds

            per_copy_g = total_g / total_copies if total_g else None
            per_copy_mm = total_mm / total_copies if total_mm else None
            per_copy_time = total_time / total_copies if total_time else None

            for jp in job_parts_list:
                part = jp.part
                updated: list[str] = []
                if per_copy_g is not None and not part.filament_used_grams:
                    part.filament_used_grams = round(per_copy_g, 2)
                    updated.append("filament_used_grams")
                if per_copy_mm is not None and not part.filament_used_meters:
                    part.filament_used_meters = round(per_copy_mm / 1000.0, 4)
                    updated.append("filament_used_meters")
                if per_copy_time is not None and not part.estimated_print_time:
                    part.estimated_print_time = timedelta(seconds=per_copy_time)
                    updated.append("estimated_print_time")
                if updated:
                    part.save(update_fields=updated)

    except (OrcaSlicerError, ThreeMFError, FileNotFoundError) as exc:
        logger.exception("Background slicing failed for job %s", job_pk)
        try:
            job = PrintJob.objects.get(pk=job_pk)
            job.status = PrintJob.STATUS_FAILED
            job.slicing_error = str(exc)
            job.save()
        except PrintJob.DoesNotExist:
            pass
    except PrintJob.DoesNotExist:
        logger.debug("slice_job(%s): job no longer exists", job_pk)
    except Exception as exc:
        logger.exception("Unexpected error during background slicing for job %s", job_pk)
        try:
            job = PrintJob.objects.get(pk=job_pk)
            job.status = PrintJob.STATUS_FAILED
            job.slicing_error = f"Unexpected error: {exc}"
            job.save()
        except PrintJob.DoesNotExist:
            pass


class SliceJobStatusView(LoginRequiredMixin, View):
    """JSON endpoint to poll the current slicing status of a PrintJob."""

    def get(self, request: HttpRequest, pk: int) -> JsonResponse:
        """Return slicing status as JSON for frontend polling.

        Args:
            request: HTTP request.
            pk: Primary key of the PrintJob.

        Returns:
            JsonResponse with status, error, and completion metadata.
        """
        job = get_object_or_404(PrintJob, pk=pk)
        plates = list(job.plates.values("plate_number", "status", "filament_used_grams"))
        data = {
            "status": job.status,
            "error": job.slicing_error,
            "filament_used_grams": job.filament_used_grams,
            "print_time_estimate": (str(job.print_time_estimate) if job.print_time_estimate else None),
            "plate_count": len(plates),
            "plates": plates,
        }
        return JsonResponse(data)


class SpoolmanSpoolsView(LoginRequiredMixin, View):
    """API proxy to list spools from a Spoolman instance."""

    def get(self, request, printer_pk):
        printer = get_object_or_404(PrinterProfile, pk=printer_pk)
        spoolman_url = settings.SPOOLMAN_URL
        if not spoolman_url:
            return JsonResponse({"error": "Spoolman URL not configured."}, status=400)

        client = SpoolmanClient(spoolman_url)
        try:
            spools = client.get_spools()
            return JsonResponse({"spools": spools})
        except SpoolmanError as exc:
            logger.exception("Spoolman error for printer %s", printer.pk)
            return JsonResponse({"error": str(exc)}, status=502)


class SpoolmanFilamentsAPIView(LoginRequiredMixin, View):
    """JSON endpoint returning Spoolman filament types for AJAX dropdowns."""

    def get(self, request):
        spoolman_url = settings.SPOOLMAN_URL
        if not spoolman_url:
            return JsonResponse({"filaments": []})
        client = SpoolmanClient(spoolman_url)
        try:
            filaments = client.get_filaments()
            return JsonResponse({"filaments": filaments})
        except SpoolmanError as exc:
            logger.warning("Spoolman unavailable: %s", exc)
            return JsonResponse({"filaments": [], "error": str(exc)})


class PrinterStatusView(LoginRequiredMixin, View):
    """API proxy to get printer status from Moonraker."""

    def get(self, request, printer_pk):
        printer = get_object_or_404(PrinterProfile, pk=printer_pk)
        if not printer.moonraker_url:
            return JsonResponse({"error": "Moonraker URL not configured."}, status=400)

        client = MoonrakerClient(printer.moonraker_url, printer.moonraker_api_key)
        try:
            status = client.get_printer_status()
            return JsonResponse({"status": status})
        except MoonrakerError as exc:
            logger.exception("Moonraker error for printer %s", printer.pk)
            return JsonResponse({"error": str(exc)}, status=502)


class UploadToPrinterView(PrinterControlMixin, View):
    """Upload a plate's G-code to a Klipper printer via Moonraker."""

    def post(self, request, plate_pk):
        plate = get_object_or_404(
            PrintJobPlate.objects.select_related("print_job__printer"),
            pk=plate_pk,
        )
        job = plate.print_job

        if not plate.gcode_file:
            messages.error(request, "No G-code file for this plate.")
            return redirect("core:printjob_detail", pk=job.pk)

        if not job.printer or not job.printer.moonraker_url:
            messages.error(request, "No printer with Moonraker configured.")
            return redirect("core:printjob_detail", pk=job.pk)

        client = MoonrakerClient(job.printer.moonraker_url, job.printer.moonraker_api_key)
        try:
            client.upload_gcode(plate.gcode_file.path)
            plate.status = PrintJobPlate.STATUS_PRINTING
            plate.save(update_fields=["status"])
            messages.success(request, f"G-code for plate {plate.plate_number} uploaded to printer.")
        except MoonrakerError as exc:
            logger.exception("Upload error for plate %s", plate.pk)
            messages.error(request, f"Upload failed: {exc}")

        return redirect("core:printjob_detail", pk=job.pk)


# ---------------------------------------------------------------------------
# Auth views
# ---------------------------------------------------------------------------


class RegisterView(CreateView):
    """User registration view.

    Registration can be disabled via the ALLOW_REGISTRATION setting.
    The very first user to register is automatically assigned the Admin role.
    """

    form_class = UserRegistrationForm
    template_name = "registration/register.html"
    success_url = reverse_lazy("core:dashboard")

    def dispatch(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        """Block access when registration is disabled."""
        if not getattr(settings, "ALLOW_REGISTRATION", True):
            messages.error(request, "Registration is currently disabled.")
            return redirect("login")
        return super().dispatch(request, *args, **kwargs)

    def form_valid(self, form: UserRegistrationForm) -> HttpResponse:
        """Save user, assign role group, and log in."""
        response = super().form_valid(form)
        user = self.object
        is_first_user = User.objects.count() == 1
        if is_first_user:
            # First user becomes Admin
            group, _created = Group.objects.get_or_create(name="Admin")
            user.groups.add(group)
            user.is_staff = True
            user.is_superuser = True
            user.save(update_fields=["is_staff", "is_superuser"])
            messages.success(
                self.request,
                "Welcome! You are the first user and have been assigned the Admin role.",
            )
        else:
            # Default role for self-registered users: Designer
            group, _created = Group.objects.get_or_create(name="Designer")
            user.groups.add(group)
            messages.success(self.request, "Registration successful. Welcome!")
        login(self.request, user)
        return response


class ProfileView(LoginRequiredMixin, UpdateView):
    """User profile page — allows editing own name and email."""

    model = User
    form_class = ProfileUpdateForm
    template_name = "core/profile.html"
    success_url = reverse_lazy("core:profile")

    def get_object(self, queryset: QuerySet | None = None) -> User:
        """Return the currently logged-in user."""
        return self.request.user

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        """Add statistics to profile context."""
        context = super().get_context_data(**kwargs)
        context["total_parts"] = Part.objects.count()
        context["total_jobs"] = PrintJob.objects.count()
        return context

    def form_valid(self, form: ProfileUpdateForm) -> HttpResponse:
        """Save profile and show success message."""
        messages.success(self.request, "Profile updated.")
        return super().form_valid(form)


# ---------------------------------------------------------------------------
# User Management (Admin only)
# ---------------------------------------------------------------------------


class UserListView(AdminRequiredMixin, ListView):
    """List all users with their roles."""

    model = User
    template_name = "core/user_list.html"
    context_object_name = "users"
    ordering = ["username"]


class UserCreateView(AdminRequiredMixin, CreateView):
    """Create a new user with role assignment."""

    model = User
    form_class = UserManagementForm
    template_name = "core/user_form.html"
    success_url = reverse_lazy("core:user_list")

    def form_valid(self, form: UserManagementForm) -> HttpResponse:
        """Save new user and show success message."""
        response = super().form_valid(form)
        messages.success(self.request, f"User '{form.instance.username}' created.")
        if "_save_and_add_another" in self.request.POST:
            return redirect(reverse("core:user_create"))
        return response


class UserUpdateView(AdminRequiredMixin, UpdateView):
    """Edit an existing user and their role."""

    model = User
    form_class = UserManagementForm
    template_name = "core/user_form.html"
    success_url = reverse_lazy("core:user_list")

    def form_valid(self, form: UserManagementForm) -> HttpResponse:
        """Save user changes and show success message."""
        messages.success(self.request, f"User '{form.instance.username}' updated.")
        return super().form_valid(form)


class UserDeleteView(AdminRequiredMixin, DeleteView):
    """Delete a user account."""

    model = User
    template_name = "core/user_confirm_delete.html"
    success_url = reverse_lazy("core:user_list")

    def form_valid(self, form: Any) -> HttpResponse:
        """Prevent self-deletion and show message."""
        if self.get_object() == self.request.user:
            messages.error(self.request, "You cannot delete your own account.")
            return redirect("core:user_list")
        messages.success(self.request, f"User '{self.get_object().username}' deleted.")
        return super().form_valid(form)


# ---------------------------------------------------------------------------
# Material / Filament views (Spoolman is mandatory)
# ---------------------------------------------------------------------------


class MaterialProfileListView(LoginRequiredMixin, TemplateView):
    """Show filaments and spools from Spoolman with inline profile mapping."""

    template_name = "core/materialprofile_list.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        spoolman_url = settings.SPOOLMAN_URL

        context["spoolman_configured"] = bool(spoolman_url)
        context["spoolman_url"] = (
            spoolman_url.rstrip("/").removesuffix("/api/v1").removesuffix("/api") if spoolman_url else ""
        )
        context["spoolman_spools"] = []
        context["spoolman_filaments"] = []
        context["spoolman_error"] = ""

        if spoolman_url:
            client = SpoolmanClient(spoolman_url)
            try:
                context["spoolman_spools"] = client.get_spools()
                filaments = client.get_filaments()
                context["spoolman_filaments"] = filaments
                # Sync cached colors and names on existing mappings
                self._sync_mapping_cache(filaments)
            except SpoolmanError as exc:
                context["spoolman_error"] = str(exc)
                logger.warning("Spoolman unavailable: %s", exc)
        else:
            context["spoolman_error"] = (
                "SPOOLMAN_URL is not configured. Set it in your environment to connect to your Spoolman instance."
            )

        # Filament profile mappings (keyed by spoolman_filament_id)
        mappings = SpoolmanFilamentMapping.objects.filter().select_related("orca_filament_profile")
        context["mappings_by_filament_id"] = {m.spoolman_filament_id: m for m in mappings}

        # Available filament profiles for the dropdown (only resolved + selectable)
        context["filament_profiles"] = OrcaFilamentProfile.objects.filter(
            state=OrcaFilamentProfile.STATE_RESOLVED,
            instantiation=True,
        )

        return context

    @staticmethod
    def _sync_mapping_cache(filaments: list[dict]) -> None:
        """Update cached color and name on existing SpoolmanFilamentMappings.

        Called every time the materials page loads, since the Spoolman API
        is queried anyway.  Only mappings that already exist are updated —
        no new mappings are created.

        Args:
            filaments: List of filament dicts from SpoolmanClient.get_filaments().
        """
        spoolman_data: dict[int, tuple[str, str]] = {}
        for f in filaments:
            fid = f.get("id")
            if fid is None:
                continue
            color_hex = (f.get("color_hex") or "")[:7]
            vendor = f.get("vendor", {}) or {}
            vendor_name = vendor.get("name", "")
            name = f.get("name", "")
            display_name = f"{vendor_name} - {name}" if vendor_name else name
            spoolman_data[fid] = (display_name, color_hex)

        existing_mappings = SpoolmanFilamentMapping.objects.filter(
            spoolman_filament_id__in=spoolman_data.keys(),
        )
        updated = 0
        for mapping in existing_mappings:
            display_name, color_hex = spoolman_data[mapping.spoolman_filament_id]
            changed = False
            if mapping.spoolman_color_hex != color_hex:
                mapping.spoolman_color_hex = color_hex
                changed = True
            if mapping.spoolman_filament_name != display_name:
                mapping.spoolman_filament_name = display_name
                changed = True
            if changed:
                mapping.save(update_fields=["spoolman_color_hex", "spoolman_filament_name", "updated_at"])
                updated += 1
        if updated:
            logger.info("Synced %d filament mapping(s) from Spoolman", updated)


class SaveFilamentMappingView(FilamentMappingManageMixin, View):
    """Save or remove a Spoolman filament → OrcaSlicer profile mapping (inline POST)."""

    def post(self, request: HttpRequest) -> HttpResponse:
        """Handle inline profile assignment from the materials page.

        Args:
            request: HTTP POST with spoolman_filament_id, filament_name,
                     and orca_filament_profile_id.

        Returns:
            Redirect back to the materials page.
        """
        filament_id_str = request.POST.get("spoolman_filament_id", "")
        profile_id_str = request.POST.get("orca_filament_profile_id", "")
        filament_name = request.POST.get("filament_name", "")

        if not filament_id_str:
            messages.error(request, "Kein Filament-Typ angegeben.")
            return redirect("core:materialprofile_list")

        filament_id = int(filament_id_str)

        if not profile_id_str:
            # Remove existing mapping
            deleted, _ = SpoolmanFilamentMapping.objects.filter(
                spoolman_filament_id=filament_id,
            ).delete()
            if deleted:
                messages.success(request, f"Profil-Zuordnung für '{filament_name}' entfernt.")
            return redirect("core:materialprofile_list")

        profile_id = int(profile_id_str)
        profile = get_object_or_404(OrcaFilamentProfile, pk=profile_id)

        filament_color = request.POST.get("filament_color", "")[:7]

        mapping, created = SpoolmanFilamentMapping.objects.update_or_create(
            spoolman_filament_id=filament_id,
            defaults={
                "orca_filament_profile": profile,
                "spoolman_filament_name": filament_name,
                "spoolman_color_hex": filament_color,
            },
        )
        messages.success(
            request,
            f"'{filament_name}' → '{profile.name}' {'zugeordnet' if created else 'aktualisiert'}.",
        )
        return redirect("core:materialprofile_list")


# ---------------------------------------------------------------------------
# Cost calculation views
# ---------------------------------------------------------------------------


class CostProfileUpdateView(PrinterManageMixin, UpdateView):
    """Update or create cost profile for a printer."""

    model = CostProfile
    form_class = CostProfileForm
    template_name = "core/costprofile_form.html"

    def get_object(self, queryset=None):
        printer = get_object_or_404(PrinterProfile, pk=self.kwargs["printer_pk"])
        obj, _created = CostProfile.objects.get_or_create(printer=printer)
        return obj

    def get_success_url(self):
        return reverse_lazy("core:printerprofile_list")

    def form_valid(self, form):
        messages.success(self.request, "Cost profile updated.")
        return super().form_valid(form)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["printer"] = self.object.printer
        return context


class ProjectCostView(LoginRequiredMixin, DetailView):
    """Show cost breakdown for a project."""

    model = Project
    template_name = "core/project_cost.html"
    context_object_name = "project"

    def get_queryset(self):
        return _user_projects_qs(self.request.user)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["ancestors"] = self.object.get_ancestors()
        project = self.object
        parts = project.parts.all()
        cost_breakdown = []
        total_cost = 0

        for part in parts:
            part_costs = {"part": part, "cost": None}
            if part.filament_used_grams and part.estimated_print_time:
                hours = part.estimated_print_time.total_seconds() / 3600
                # Try to find a cost profile from recent print jobs
                recent_job = part.print_jobs.filter(printer__isnull=False).first()
                if recent_job and hasattr(recent_job.printer, "cost_profile"):
                    cp = recent_job.printer.cost_profile
                    part_costs["cost"] = cp.calculate_print_cost(
                        hours * part.quantity,
                        part.filament_used_grams * part.quantity,
                    )
                    total_cost += part_costs["cost"]["total_cost"]
            cost_breakdown.append(part_costs)

        context["cost_breakdown"] = cost_breakdown
        context["total_cost"] = round(total_cost, 2)

        # Hardware costs
        context["hardware_requirements"] = project.hardware_requirements()
        context["total_hardware_cost"] = project.total_hardware_cost
        context["grand_total"] = round(total_cost + project.total_hardware_cost, 2)

        return context


# ---------------------------------------------------------------------------
# Print Queue views
# ---------------------------------------------------------------------------


class PrintQueueListView(LoginRequiredMixin, ListView):
    """Show the print queue for all printers."""

    model = PrintQueue
    template_name = "core/printqueue_list.html"
    context_object_name = "queue_entries"

    def get_queryset(self):
        return (
            PrintQueue.objects.filter()
            .select_related("plate__print_job", "printer")
            .order_by("printer__name", "-priority", "position", "added_at")
        )


class PrintQueueCreateView(QueueManageMixin, CreateView):
    """Add a plate to the print queue.

    The user selects a plate from a sliced job and assigns it to a printer.
    """

    model = PrintQueue
    form_class = PrintQueueForm
    template_name = "core/printqueue_form.html"
    success_url = reverse_lazy("core:printqueue_list")

    def get_form(self, form_class=None):
        form = super().get_form(form_class)
        # Only plates from sliced jobs that have G-code ready
        form.fields["plate"].queryset = (
            PrintJobPlate.objects.filter(
                print_job__status=PrintJob.STATUS_SLICED,
                gcode_file__isnull=False,
            )
            .exclude(gcode_file="")
            .select_related("print_job")
        )

        # Pre-select plate from query parameter
        plate_pk = self.request.GET.get("plate")
        if plate_pk:
            try:
                selected_plate = PrintJobPlate.objects.get(
                    pk=plate_pk,
                    print_job__status=PrintJob.STATUS_SLICED,
                )
                form.fields["plate"].initial = selected_plate.pk
            except PrintJobPlate.DoesNotExist:
                pass

        # All printers included
        form.fields["printer"].queryset = PrinterProfile.objects.all()
        return form

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        """Add plate→compatible-printers mapping for client-side filtering."""
        import json as _json

        context = super().get_context_data(**kwargs)

        # Build a map: plate_pk → [compatible printer PKs]
        plates = (
            PrintJobPlate.objects.filter(
                print_job__status=PrintJob.STATUS_SLICED,
                gcode_file__isnull=False,
            )
            .exclude(gcode_file="")
            .select_related("print_job__machine_profile")
        )

        printers = PrinterProfile.objects.all()

        plate_printer_map: dict[str, list[int]] = {}
        for plate in plates:
            job = plate.print_job
            if job.machine_profile:
                compatible = [p.pk for p in printers if p.orca_machine_profile_id == job.machine_profile_id]
            else:
                compatible = [p.pk for p in printers]
            plate_printer_map[str(plate.pk)] = compatible

        context["job_printer_map_json"] = _json.dumps(plate_printer_map)
        return context

    def form_valid(self, form):
        """Create the queue entry for the plate."""
        printer = form.cleaned_data["printer"]
        plate = form.cleaned_data["plate"]
        priority = form.cleaned_data["priority"]

        max_pos = PrintQueue.objects.filter(printer=printer).aggregate(m=Max("position"))["m"] or 0

        entry = PrintQueue(
            plate=plate,
            printer=printer,
            priority=priority,
            position=max_pos + 1,
        )
        entry.save()

        messages.success(
            self.request,
            f"Plate {plate.plate_number} of '{plate.print_job}' added to queue on {printer.name}.",
        )
        if "_save_and_add_another" in self.request.POST:
            return redirect(reverse("core:printqueue_create"))
        return redirect(self.success_url)


class PrintQueueDeleteView(QueueDequeueMixin, DeleteView):
    """Remove a job from the queue."""

    model = PrintQueue
    template_name = "core/printqueue_confirm_delete.html"
    success_url = reverse_lazy("core:printqueue_list")

    def get_queryset(self):
        return PrintQueue.objects.all()

    def form_valid(self, form):
        messages.success(self.request, "Removed from queue.")
        return super().form_valid(form)


class RunNextQueueView(PrinterControlMixin, View):
    """Run the next waiting print on a specific printer.

    Checks that the printer is free (no entry currently printing or awaiting
    review), then uploads the G-code via Moonraker and starts the print.
    """

    def post(self, request: HttpRequest, printer_pk: int) -> HttpResponse:
        printer = get_object_or_404(PrinterProfile, pk=printer_pk)
        if not printer.moonraker_url:
            messages.error(request, "No Moonraker URL configured for this printer.")
            return redirect("core:printqueue_list")

        # Check that the printer is free
        busy_entry = PrintQueue.objects.filter(
            printer=printer,
            status__in=[PrintQueue.STATUS_PRINTING, PrintQueue.STATUS_AWAITING_REVIEW],
        ).first()
        if busy_entry:
            status_label = busy_entry.get_status_display()
            messages.warning(
                request,
                f'Printer "{printer.name}" is busy ({status_label}: '
                f'plate {busy_entry.plate.plate_number} of "{busy_entry.print_job}").',
            )
            return redirect("core:printqueue_list")

        # Get the highest-priority waiting entry
        entry = (
            PrintQueue.objects.filter(
                printer=printer,
                status=PrintQueue.STATUS_WAITING,
            )
            .order_by("-priority", "position", "added_at")
            .select_related("plate__print_job")
            .first()
        )

        if not entry:
            messages.warning(request, "No waiting jobs in the queue for this printer.")
            return redirect("core:printqueue_list")

        plate = entry.plate
        job = plate.print_job

        gcode_file = plate.gcode_file
        if not gcode_file:
            messages.error(
                request,
                f"No G-code file for plate {plate.plate_number}. Slice the job first.",
            )
            return redirect("core:printqueue_list")

        # Build a descriptive, filesystem-safe filename for Klipper
        safe_name = re.sub(r"[^\w\-]", "_", str(job))[:50]
        remote_filename = f"LN_{safe_name}_p{plate.plate_number}.gcode"

        client = MoonrakerClient(printer.moonraker_url, printer.moonraker_api_key)
        try:
            # Upload gcode
            client.upload_gcode(gcode_file.path, filename=remote_filename)
            # Start the print
            client.start_print(remote_filename)

            # Update queue entry
            entry.status = PrintQueue.STATUS_PRINTING
            entry.started_at = timezone.now()
            entry.save(update_fields=["status", "started_at"])

            # Update plate status
            plate.status = PrintJobPlate.STATUS_PRINTING
            plate.klipper_job_id = remote_filename
            plate.started_at = timezone.now()
            plate.save(update_fields=["status", "klipper_job_id", "started_at"])

            # Update job status
            job.status = PrintJob.STATUS_PRINTING
            if not job.started_at:
                job.started_at = timezone.now()
            job.save(update_fields=["status", "started_at"])

            messages.success(
                request,
                f'Started printing plate {plate.plate_number} of "{job}" on {printer.name}.',
            )
        except MoonrakerError as exc:
            logger.exception("Failed to run queue entry %s", entry.pk)
            messages.error(request, f"Failed to start print: {exc}")

        return redirect("core:printqueue_list")


class RunAllQueuesView(PrinterControlMixin, View):
    """Start the next waiting job on every free printer.

    Iterates over all printers that have a Moonraker URL.
    For each printer that is free (no printing/awaiting_review entry), it
    picks the highest-priority waiting entry and starts the print.
    """

    def post(self, request: HttpRequest) -> HttpResponse:
        printers = PrinterProfile.objects.filter().exclude(moonraker_url="")

        started = 0
        skipped_busy = 0
        skipped_empty = 0
        errors = 0

        for printer in printers:
            # Check printer is free
            busy = PrintQueue.objects.filter(
                printer=printer,
                status__in=[
                    PrintQueue.STATUS_PRINTING,
                    PrintQueue.STATUS_AWAITING_REVIEW,
                ],
            ).exists()
            if busy:
                skipped_busy += 1
                continue

            # Next waiting entry
            entry = (
                PrintQueue.objects.filter(
                    printer=printer,
                    status=PrintQueue.STATUS_WAITING,
                )
                .order_by("-priority", "position", "added_at")
                .select_related("plate__print_job")
                .first()
            )
            if not entry:
                skipped_empty += 1
                continue

            plate = entry.plate
            job = plate.print_job
            gcode_file = plate.gcode_file
            if not gcode_file:
                logger.warning("Queue entry %s has no G-code file, skipping.", entry.pk)
                skipped_empty += 1
                continue

            safe_name = re.sub(r"[^\w\-]", "_", str(job))[:50]
            remote_filename = f"LN_{safe_name}_p{plate.plate_number}.gcode"

            client = MoonrakerClient(printer.moonraker_url, printer.moonraker_api_key)
            try:
                client.upload_gcode(gcode_file.path, filename=remote_filename)
                client.start_print(remote_filename)

                entry.status = PrintQueue.STATUS_PRINTING
                entry.started_at = timezone.now()
                entry.save(update_fields=["status", "started_at"])

                plate.status = PrintJobPlate.STATUS_PRINTING
                plate.klipper_job_id = remote_filename
                plate.started_at = timezone.now()
                plate.save(update_fields=["status", "klipper_job_id", "started_at"])

                job.status = PrintJob.STATUS_PRINTING
                if not job.started_at:
                    job.started_at = timezone.now()
                job.save(update_fields=["status", "started_at"])

                started += 1
            except MoonrakerError as exc:
                logger.exception("Failed to start print on %s: %s", printer.name, exc)
                errors += 1

        parts = []
        if started:
            parts.append(f"{started} print(s) started")
        if skipped_busy:
            parts.append(f"{skipped_busy} printer(s) busy")
        if skipped_empty:
            parts.append(f"{skipped_empty} printer(s) have no waiting jobs")
        if errors:
            parts.append(f"{errors} error(s)")

        summary = ", ".join(parts) if parts else "No printers configured."
        if errors:
            messages.warning(request, f"Run All: {summary}")
        elif started:
            messages.success(request, f"Run All: {summary}")
        else:
            messages.info(request, f"Run All: {summary}")

        return redirect("core:printqueue_list")


class QueueEntryReviewView(PrinterControlMixin, View):
    """Confirm a finished print as Pass (success) or Fail (retry/discard).

    GET  – renders a review page showing the queue entry details.
    POST – processes the ``action`` parameter (``pass`` or ``fail``).

    **Pass:** Marks the plate as completed.  If all plates of the job are
    done, the job status is set to *completed*.

    **Fail:** Increments ``retry_count`` and resets the entry to *waiting*
    so it will be picked up again.  If ``retry_count >= max_retries`` the
    entry is removed and a warning is shown.
    """

    def _get_entry(self, request: HttpRequest, pk: int) -> PrintQueue:
        """Fetch the queue entry or 404."""
        return get_object_or_404(
            PrintQueue.objects.select_related("plate__print_job", "printer"),
            pk=pk,
            status=PrintQueue.STATUS_AWAITING_REVIEW,
        )

    def get(self, request: HttpRequest, pk: int) -> HttpResponse:
        """Show the review confirmation page."""
        from django.template.response import TemplateResponse

        entry = self._get_entry(request, pk)
        return TemplateResponse(
            request,
            "core/printqueue_review.html",
            {"entry": entry},
        )

    def post(self, request: HttpRequest, pk: int) -> HttpResponse:
        """Process pass or fail action."""
        entry = self._get_entry(request, pk)
        action = request.POST.get("action", "")
        plate = entry.plate
        job = plate.print_job
        plate_label = f"Plate {plate.plate_number} of '{job}'"

        if action == "pass":
            # Success – mark plate as completed, remove queue entry
            plate.status = PrintJobPlate.STATUS_COMPLETED
            plate.completed_at = timezone.now()
            plate.save(update_fields=["status", "completed_at"])
            entry.delete()

            # Check if all plates of the job are completed
            if job.all_plates_completed:
                job.status = PrintJob.STATUS_COMPLETED
                job.completed_at = timezone.now()
                job.save(update_fields=["status", "completed_at"])

            messages.success(
                request,
                f"{plate_label} passed! "
                f"({job.plates.filter(status=PrintJobPlate.STATUS_COMPLETED).count()}"
                f"/{job.plate_count} plates completed)",
            )

        elif action == "fail":
            entry.retry_count += 1
            if entry.retry_count >= entry.max_retries:
                plate.status = PrintJobPlate.STATUS_FAILED
                plate.save(update_fields=["status"])
                entry.delete()
                messages.error(
                    request,
                    f"{plate_label} failed and max retries ({entry.max_retries}) exceeded — removed from queue.",
                )
            else:
                entry.status = PrintQueue.STATUS_WAITING
                entry.started_at = None
                entry.completed_at = None
                entry.save(
                    update_fields=[
                        "status",
                        "retry_count",
                        "started_at",
                        "completed_at",
                    ]
                )
                plate.status = PrintJobPlate.STATUS_WAITING
                plate.save(update_fields=["status"])
                messages.warning(
                    request,
                    f"{plate_label} failed — requeued (retry {entry.retry_count}/{entry.max_retries}).",
                )
        else:
            messages.error(request, "Invalid action.")

        return redirect("core:printqueue_list")


class QueueCheckPrinterStatusView(LoginRequiredMixin, View):
    """JSON endpoint: poll Moonraker for a printing queue entry.

    Returns ``{"status": "printing"|"awaiting_review"|"error", ...}``.
    If the printer reports the job as complete the queue entry is moved
    to *awaiting_review*.
    """

    def get(self, request: HttpRequest, pk: int) -> JsonResponse:
        entry = get_object_or_404(
            PrintQueue.objects.select_related("print_job", "printer"),
            pk=pk,
        )

        if entry.status != PrintQueue.STATUS_PRINTING:
            return JsonResponse({"status": entry.status})

        printer = entry.printer
        if not printer.moonraker_url:
            return JsonResponse({"status": "error", "message": "No Moonraker URL"})

        client = MoonrakerClient(printer.moonraker_url, printer.moonraker_api_key)
        try:
            data = client.get_job_status()
            status_data = data.get("result", {}).get("status", {})
            print_stats = status_data.get("print_stats", {})
            virtual_sd = status_data.get("virtual_sdcard", {})
            state = print_stats.get("state", "unknown")
            progress = virtual_sd.get("progress", 0)

            if state == "complete":
                entry.status = PrintQueue.STATUS_AWAITING_REVIEW
                entry.completed_at = timezone.now()
                entry.save(update_fields=["status", "completed_at"])
                return JsonResponse(
                    {
                        "status": "awaiting_review",
                        "message": "Print finished — awaiting review.",
                    }
                )

            if state in ("error", "cancelled", "standby"):
                # standby = printer idle after cancel or after finishing.
                # cancelled / error = explicit failure states.
                # All mean the print is no longer running → operator review.
                entry.status = PrintQueue.STATUS_AWAITING_REVIEW
                entry.completed_at = timezone.now()
                entry.save(update_fields=["status", "completed_at"])
                return JsonResponse(
                    {
                        "status": "awaiting_review",
                        "message": f"Printer reported: {state}",
                    }
                )

            return JsonResponse(
                {
                    "status": "printing",
                    "printer_state": state,
                    "progress": progress,
                }
            )

        except MoonrakerError as exc:
            logger.warning("Moonraker poll failed for entry %s: %s", pk, exc)
            return JsonResponse({"status": "error", "message": str(exc)})


class CancelQueueEntryView(PrinterControlMixin, View):
    """Cancel a running print and move the queue entry to awaiting_review.

    Sends a cancel command to Moonraker and sets the entry status so
    the operator can then Pass (discard the failed print) or Fail
    (retry).
    """

    def post(self, request: HttpRequest, pk: int) -> HttpResponse:
        entry = get_object_or_404(
            PrintQueue.objects.select_related("plate__print_job", "printer"),
            pk=pk,
            status=PrintQueue.STATUS_PRINTING,
        )
        printer = entry.printer
        plate_label = f"Plate {entry.plate.plate_number} of '{entry.print_job}'"

        if printer.moonraker_url:
            client = MoonrakerClient(printer.moonraker_url, printer.moonraker_api_key)
            try:
                client.cancel_print()
            except MoonrakerError as exc:
                logger.warning("Cancel command failed for entry %s: %s", pk, exc)
                messages.warning(
                    request,
                    f"Moonraker cancel command failed ({exc}), but entry marked for review.",
                )

        entry.status = PrintQueue.STATUS_AWAITING_REVIEW
        entry.completed_at = timezone.now()
        entry.save(update_fields=["status", "completed_at"])

        messages.info(
            request,
            f"{plate_label} on {printer.name} cancelled — please review.",
        )
        return redirect("core:printqueue_list")


# ---------------------------------------------------------------------------
# Statistics / Dashboard API
# ---------------------------------------------------------------------------


class StatisticsView(LoginRequiredMixin, TemplateView):
    """Dashboard statistics and charts page."""

    template_name = "core/statistics.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)

        # Project stats
        projects = Project.objects.all()
        context["total_projects"] = projects.count()
        context["total_parts"] = (
            Part.objects.filter(project__in=projects).aggregate(total=Sum("quantity"))["total"] or 0
        )

        # Job stats
        all_jobs = PrintJob.objects.all()
        context["total_jobs"] = all_jobs.count()
        context["completed_jobs"] = all_jobs.filter(status="completed").count()
        context["failed_jobs"] = all_jobs.filter(status="failed").count()
        context["active_jobs"] = all_jobs.filter(status__in=["slicing", "uploading", "uploaded", "printing"]).count()

        # Plate stats
        all_plates = PrintJobPlate.objects.all()
        context["total_plates"] = all_plates.count()
        context["total_plates_completed"] = all_plates.filter(
            status=PrintJobPlate.STATUS_COMPLETED,
        ).count()
        context["total_quantity_completed"] = context["total_plates_completed"]
        context["total_quantity_to_print"] = context["total_plates"]

        # Success rate
        finished = all_jobs.filter(status__in=["completed", "failed"]).count()
        context["success_rate"] = (
            round(all_jobs.filter(status="completed").count() / finished * 100, 1) if finished else None
        )

        # Filament stats
        context["total_filament_grams"] = (
            Part.objects.filter(project__in=projects, filament_used_grams__isnull=False).aggregate(
                total=Sum("filament_used_grams")
            )["total"]
            or 0
        )

        # Total estimated print time (from plates)
        total_seconds = all_plates.filter(
            print_time_estimate__isnull=False,
        ).aggregate(total=Sum("print_time_estimate"))["total"]
        context["total_print_time"] = total_seconds

        # Material breakdown
        context["material_breakdown"] = (
            Part.objects.filter(project__in=projects)
            .values("material")
            .annotate(count=Count("id"), total_qty=Sum("quantity"))
            .order_by("-total_qty")
        )

        # Jobs per status
        context["jobs_by_status"] = all_jobs.values("status").annotate(count=Count("id")).order_by("status")

        # Time estimation accuracy
        estimates = PrintTimeEstimate.objects.filter(
            actual_time__isnull=False,
            estimated_time__isnull=False,
        ).only("actual_time", "estimated_time")[:200]
        factors = [e.accuracy_factor for e in estimates if e.accuracy_factor]
        context["avg_accuracy"] = round(sum(factors) / len(factors), 2) if factors else None

        # Queue stats
        user_queue = PrintQueue.objects.all()
        context["queue_total"] = user_queue.count()
        context["queue_waiting"] = user_queue.filter(
            status=PrintQueue.STATUS_WAITING,
        ).count()
        context["queue_printing"] = user_queue.filter(
            status=PrintQueue.STATUS_PRINTING,
        ).count()
        context["queue_review"] = user_queue.filter(
            status=PrintQueue.STATUS_AWAITING_REVIEW,
        ).count()

        # Printer stats
        printers = PrinterProfile.objects.all()
        context["total_printers"] = printers.count()
        active_printer_ids = (
            user_queue.filter(
                status__in=[
                    PrintQueue.STATUS_PRINTING,
                    PrintQueue.STATUS_AWAITING_REVIEW,
                ],
            )
            .values_list("printer_id", flat=True)
            .distinct()
        )
        context["busy_printers"] = active_printer_ids.count()
        context["idle_printers"] = (
            printers.exclude(
                pk__in=active_printer_ids,
            )
            .filter(moonraker_url__gt="")
            .count()
        )

        # Total retries across all queue entries
        context["total_retries"] = user_queue.aggregate(total=Sum("retry_count"))["total"] or 0

        return context


# ---------------------------------------------------------------------------
# Admin Dashboard
# ---------------------------------------------------------------------------


class AdminDashboardView(AdminRequiredMixin, TemplateView):
    """Admin overview showing system health, estimation status, and recent activity.

    Displays:
    - Parts with pending or failed estimations (with re-estimate actions)
    - Print jobs currently slicing or waiting to slice
    - System-wide statistics (projects, parts, users, printers, storage)
    - Recent activity across the platform
    """

    template_name = "core/admin_dashboard.html"

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        """Build admin dashboard context with system-wide data.

        Returns:
            Dictionary with unified worker queue, system stats, and recent activity.
        """
        context = super().get_context_data(**kwargs)

        # -- Unified OrcaSlicer worker queue ----------------------------------
        # Active items (currently being processed by the worker)
        queue_active: list[dict[str, Any]] = []
        queue_waiting: list[dict[str, Any]] = []
        queue_errors: list[dict[str, Any]] = []

        # Parts actively estimating
        for part in Part.objects.filter(
            estimation_status=Part.ESTIMATION_ESTIMATING,
        ).select_related("project"):
            queue_active.append(
                {
                    "type": "estimation",
                    "icon": "bi-calculator",
                    "name": part.name,
                    "detail": part.project.name,
                    "url": reverse("core:part_detail", args=[part.pk]),
                    "detail_url": reverse("core:project_detail", args=[part.project.pk]),
                }
            )

        # Jobs actively slicing
        for job in PrintJob.objects.filter(
            status=PrintJob.STATUS_SLICING,
        ).select_related("created_by"):
            queue_active.append(
                {
                    "type": "slicing",
                    "icon": "bi-scissors",
                    "name": str(job.name or f"Job #{job.pk}"),
                    "detail": str(job.created_by or "—"),
                    "url": reverse("core:printjob_detail", args=[job.pk]),
                    "started_at": job.slicing_started_at,
                }
            )

        # Pending slicing jobs (queued, higher priority)
        for job in (
            PrintJob.objects.filter(
                status=PrintJob.STATUS_PENDING,
            )
            .select_related("created_by")
            .order_by("pk")
        ):
            queue_waiting.append(
                {
                    "type": "slicing",
                    "icon": "bi-scissors",
                    "name": str(job.name or f"Job #{job.pk}"),
                    "detail": str(job.created_by or "—"),
                    "url": reverse("core:printjob_detail", args=[job.pk]),
                }
            )

        # Pending estimations (queued, lower priority)
        for part in (
            Part.objects.filter(
                estimation_status=Part.ESTIMATION_PENDING,
            )
            .select_related("project")
            .order_by("pk")
        ):
            queue_waiting.append(
                {
                    "type": "estimation",
                    "icon": "bi-calculator",
                    "name": part.name,
                    "detail": part.project.name,
                    "url": reverse("core:part_detail", args=[part.pk]),
                    "detail_url": reverse("core:project_detail", args=[part.project.pk]),
                }
            )

        # Estimation errors
        for part in Part.objects.filter(
            estimation_status=Part.ESTIMATION_ERROR,
        ).select_related("project"):
            queue_errors.append(
                {
                    "type": "estimation",
                    "icon": "bi-calculator",
                    "name": part.name,
                    "detail": part.project.name,
                    "url": reverse("core:part_detail", args=[part.pk]),
                    "detail_url": reverse("core:project_detail", args=[part.project.pk]),
                    "error": part.estimation_error,
                    "retry_url": reverse("core:part_re_estimate", args=[part.pk]),
                }
            )

        # Failed slicing jobs
        for job in (
            PrintJob.objects.filter(
                status=PrintJob.STATUS_FAILED,
                slicing_error__gt="",
            )
            .select_related("created_by")
            .order_by("-updated_at")
        ):
            queue_errors.append(
                {
                    "type": "slicing",
                    "icon": "bi-scissors",
                    "name": str(job.name or f"Job #{job.pk}"),
                    "detail": str(job.created_by or "—"),
                    "url": reverse("core:printjob_detail", args=[job.pk]),
                    "error": job.slicing_error,
                }
            )

        context["queue_active"] = queue_active
        context["queue_waiting"] = queue_waiting
        context["queue_errors"] = queue_errors
        context["queue_active_count"] = len(queue_active)
        context["queue_waiting_count"] = len(queue_waiting)
        context["queue_error_count"] = len(queue_errors)

        # -- Estimation breakdown counts (for progress bar) -------------------
        context["parts_estimating_count"] = Part.objects.filter(
            estimation_status=Part.ESTIMATION_ESTIMATING,
        ).count()
        context["parts_pending_count"] = Part.objects.filter(
            estimation_status=Part.ESTIMATION_PENDING,
        ).count()
        context["parts_error_count"] = Part.objects.filter(
            estimation_status=Part.ESTIMATION_ERROR,
        ).count()

        # -- System statistics ------------------------------------------------
        context["total_projects"] = Project.objects.count()
        context["total_parts"] = Part.objects.count()
        context["total_users"] = User.objects.count()
        context["total_printers"] = PrinterProfile.objects.count()
        context["total_jobs"] = PrintJob.objects.count()

        # Parts with / without estimates
        context["parts_estimated"] = Part.objects.filter(
            estimation_status=Part.ESTIMATION_SUCCESS,
        ).count()
        context["parts_no_estimate"] = Part.objects.filter(
            estimation_status=Part.ESTIMATION_NONE,
        ).count()

        # Storage usage
        stl_dir = Path(settings.MEDIA_ROOT) / "stl_files"
        gcode_dir = Path(settings.MEDIA_ROOT) / "gcode_jobs"
        stl_bytes = sum(f.stat().st_size for f in stl_dir.rglob("*") if f.is_file()) if stl_dir.exists() else 0
        gcode_bytes = sum(f.stat().st_size for f in gcode_dir.rglob("*") if f.is_file()) if gcode_dir.exists() else 0
        context["stl_storage_mb"] = round(stl_bytes / (1024 * 1024), 1)
        context["gcode_storage_mb"] = round(gcode_bytes / (1024 * 1024), 1)
        context["total_storage_mb"] = round((stl_bytes + gcode_bytes) / (1024 * 1024), 1)

        # -- Recent activity --------------------------------------------------
        context["recent_projects"] = Project.objects.order_by("-updated_at")[:10]
        context["recent_parts"] = Part.objects.select_related("project").order_by("-updated_at")[:10]
        context["recent_jobs"] = PrintJob.objects.select_related("created_by").order_by("-updated_at")[:10]

        return context


# ---------------------------------------------------------------------------
# Project Document views
# ---------------------------------------------------------------------------


class ProjectDocumentCreateView(ProjectManageMixin, CreateView):
    """Upload a document to a project."""

    model = ProjectDocument
    form_class = ProjectDocumentForm
    template_name = "core/projectdocument_form.html"

    def get_project(self) -> Project:
        """Return the project from the URL."""
        return get_object_or_404(Project, pk=self.kwargs["project_pk"])

    def get_context_data(self, **kwargs) -> dict:
        """Add project and breadcrumb ancestors to context."""
        context = super().get_context_data(**kwargs)
        project = self.get_project()
        context["project"] = project
        context["ancestors"] = project.get_ancestors()
        return context

    def form_valid(self, form: ProjectDocumentForm) -> HttpResponse:
        """Set project and uploaded_by before saving."""
        form.instance.project = self.get_project()
        form.instance.uploaded_by = self.request.user
        response = super().form_valid(form)
        messages.success(self.request, f"Document '{form.instance.name}' uploaded.")
        return response

    def get_success_url(self) -> str:
        """Redirect to the project detail page."""
        return reverse("core:project_detail", kwargs={"pk": self.kwargs["project_pk"]})


class ProjectDocumentDeleteView(ProjectManageMixin, DeleteView):
    """Delete a document from a project."""

    model = ProjectDocument
    template_name = "core/projectdocument_confirm_delete.html"
    context_object_name = "document"

    def get_context_data(self, **kwargs) -> dict:
        """Add project and breadcrumb ancestors to context."""
        context = super().get_context_data(**kwargs)
        project = self.object.project
        context["project"] = project
        context["ancestors"] = project.get_ancestors()
        return context

    def form_valid(self, form) -> HttpResponse:
        """Show success message and delete the document."""
        messages.success(self.request, f"Document '{self.object.name}' deleted.")
        return super().form_valid(form)

    def get_success_url(self) -> str:
        """Redirect to the project detail page."""
        return reverse("core:project_detail", kwargs={"pk": self.object.project_id})


class ProjectDocumentDownloadView(LoginRequiredMixin, View):
    """Serve a project document file with authentication enforcement."""

    def get(self, request: HttpRequest, pk: int) -> FileResponse:
        """Stream the document file to the authenticated user."""
        document = get_object_or_404(ProjectDocument, pk=pk)
        return FileResponse(
            document.file.open("rb"),
            as_attachment=True,
            filename=Path(document.file.name).name,
        )


# ---------------------------------------------------------------------------
# Project Hardware views
# ---------------------------------------------------------------------------


class ProjectHardwareCreateView(ProjectManageMixin, FormView):
    """Add a hardware part to a project (select existing or create new)."""

    form_class = ProjectHardwareForm
    template_name = "core/projecthardware_form.html"

    def get_project(self) -> Project:
        """Return the project from the URL."""
        return get_object_or_404(Project, pk=self.kwargs["project_pk"])

    def get_context_data(self, **kwargs) -> dict:
        """Add project and breadcrumb ancestors to context."""
        context = super().get_context_data(**kwargs)
        project = self.get_project()
        context["project"] = project
        context["ancestors"] = project.get_ancestors()
        return context

    def form_valid(self, form: ProjectHardwareForm) -> HttpResponse:
        """Create or link hardware part and redirect."""
        project = self.get_project()
        try:
            ph = form.save(project=project, user=self.request.user)
            messages.success(
                self.request,
                f"Hardware '{ph.hardware_part.name}' (×{ph.quantity}) added to project.",
            )
        except IntegrityError:
            messages.error(
                self.request,
                "This hardware part is already assigned to this project.",
            )
        except Exception:
            logger.exception("Unexpected error while adding hardware to project %s", project.pk)
            messages.error(
                self.request,
                "An unexpected error occurred while adding the hardware part. "
                "Please try again or contact an administrator.",
            )
            raise
        return redirect(self.get_success_url())

    def get_success_url(self) -> str:
        """Redirect to the project detail page."""
        return reverse("core:project_detail", kwargs={"pk": self.kwargs["project_pk"]})


class ProjectHardwareUpdateView(ProjectManageMixin, UpdateView):
    """Edit a hardware assignment (quantity, notes, and hardware part details)."""

    model = ProjectHardware
    form_class = ProjectHardwareUpdateForm
    template_name = "core/projecthardware_form.html"
    context_object_name = "assignment"

    def get_context_data(self, **kwargs) -> dict:
        """Add project and breadcrumb ancestors to context."""
        context = super().get_context_data(**kwargs)
        project = self.object.project
        context["project"] = project
        context["ancestors"] = project.get_ancestors()
        context["is_edit"] = True
        return context

    def form_valid(self, form: ProjectHardwareUpdateForm) -> HttpResponse:
        """Save and show success message."""
        response = super().form_valid(form)
        messages.success(self.request, f"Hardware '{self.object.hardware_part.name}' updated.")
        return response

    def get_success_url(self) -> str:
        """Redirect to the project detail page."""
        return reverse("core:project_detail", kwargs={"pk": self.object.project_id})


class ProjectHardwareDeleteView(ProjectManageMixin, DeleteView):
    """Remove a hardware assignment from a project (does not delete the HardwarePart)."""

    model = ProjectHardware
    template_name = "core/projecthardware_confirm_delete.html"
    context_object_name = "assignment"

    def get_context_data(self, **kwargs) -> dict:
        """Add project and breadcrumb ancestors to context."""
        context = super().get_context_data(**kwargs)
        project = self.object.project
        context["project"] = project
        context["ancestors"] = project.get_ancestors()
        return context

    def form_valid(self, form) -> HttpResponse:
        """Show success message and remove the assignment."""
        name = self.object.hardware_part.name
        messages.success(self.request, f"Hardware '{name}' removed from project.")
        return super().form_valid(form)

    def get_success_url(self) -> str:
        """Redirect to the project detail page."""
        return reverse("core:project_detail", kwargs={"pk": self.object.project_id})
