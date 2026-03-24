"""Dashboard views for the LayerNexus application."""

import logging
from pathlib import Path
from typing import Any

from django.conf import settings
from django.contrib.auth.mixins import LoginRequiredMixin
from django.contrib.auth.models import User
from django.db.models import Count, Sum
from django.urls import reverse
from django.utils import timezone
from django.views.generic import TemplateView

from core.mixins import AdminRequiredMixin
from core.models import (
    Part,
    PrinterProfile,
    PrintJob,
    PrintJobPlate,
    PrintQueue,
    PrintTimeEstimate,
    Project,
)

logger = logging.getLogger(__name__)

__all__ = [
    "DashboardView",
    "FarmDashboardView",
    "StatisticsView",
    "AdminDashboardView",
]


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
