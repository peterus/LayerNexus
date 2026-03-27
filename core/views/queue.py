"""Print queue views for the LayerNexus application."""

import logging
import re

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.db.models import Max
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse, reverse_lazy
from django.utils import timezone
from django.views import View
from django.views.generic import CreateView, DeleteView, ListView

from core.forms import PrintQueueForm
from core.mixins import PrinterControlMixin, QueueDequeueMixin, QueueManageMixin
from core.models import (
    PrinterProfile,
    PrintJob,
    PrintJobPlate,
    PrintQueue,
)
from core.services.printer_backend import PrinterError, get_printer_backend

logger = logging.getLogger(__name__)

__all__ = [
    "PrintQueueListView",
    "PrintQueueCreateView",
    "PrintQueueDeleteView",
    "RunNextQueueView",
    "RunAllQueuesView",
    "QueueEntryReviewView",
    "QueueCheckPrinterStatusView",
    "CancelQueueEntryView",
]


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

    def get_context_data(self, **kwargs):
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

        # Build a descriptive, filesystem-safe filename for the printer
        safe_name = re.sub(r"[^\w\-]", "_", str(job))[:50]
        remote_filename = f"LN_{safe_name}_p{plate.plate_number}.gcode"

        try:
            backend = get_printer_backend(printer)
            backend.upload_gcode(gcode_file.path, filename=remote_filename)
            backend.start_print(remote_filename)

            # Update queue entry
            entry.status = PrintQueue.STATUS_PRINTING
            entry.started_at = timezone.now()
            entry.save(update_fields=["status", "started_at"])

            # Update plate status
            plate.status = PrintJobPlate.STATUS_PRINTING
            plate.remote_job_id = remote_filename
            plate.started_at = timezone.now()
            plate.save(update_fields=["status", "remote_job_id", "started_at"])

            # Update job status
            job.status = PrintJob.STATUS_PRINTING
            if not job.started_at:
                job.started_at = timezone.now()
            job.save(update_fields=["status", "started_at"])

            messages.success(
                request,
                f'Started printing plate {plate.plate_number} of "{job}" on {printer.name}.',
            )
        except PrinterError as exc:
            logger.exception("Failed to run queue entry %s", entry.pk)
            messages.error(request, f"Failed to start print: {exc}")

        return redirect("core:printqueue_list")


class RunAllQueuesView(PrinterControlMixin, View):
    """Start the next waiting job on every free printer.

    Iterates over all configured printers.
    For each printer that is free (no printing/awaiting_review entry), it
    picks the highest-priority waiting entry and starts the print.
    """

    def post(self, request: HttpRequest) -> HttpResponse:
        printers = PrinterProfile.objects.all()

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

            try:
                backend = get_printer_backend(printer)
                backend.upload_gcode(gcode_file.path, filename=remote_filename)
                backend.start_print(remote_filename)

                entry.status = PrintQueue.STATUS_PRINTING
                entry.started_at = timezone.now()
                entry.save(update_fields=["status", "started_at"])

                plate.status = PrintJobPlate.STATUS_PRINTING
                plate.remote_job_id = remote_filename
                plate.started_at = timezone.now()
                plate.save(update_fields=["status", "remote_job_id", "started_at"])

                job.status = PrintJob.STATUS_PRINTING
                if not job.started_at:
                    job.started_at = timezone.now()
                job.save(update_fields=["status", "started_at"])

                started += 1
            except PrinterError as exc:
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
    """JSON endpoint: poll printer backend for a printing queue entry.

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
        try:
            backend = get_printer_backend(printer)
            job_status = backend.get_job_status()
            state = job_status.state
            progress = job_status.progress

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

        except PrinterError as exc:
            logger.warning("Printer poll failed for entry %s: %s", pk, exc)
            return JsonResponse({"status": "error", "message": str(exc)})


class CancelQueueEntryView(PrinterControlMixin, View):
    """Cancel a running print and move the queue entry to awaiting_review.

    Sends a cancel command to the printer backend and sets the entry status
    so the operator can then Pass (discard the failed print) or Fail (retry).
    """

    def post(self, request: HttpRequest, pk: int) -> HttpResponse:
        entry = get_object_or_404(
            PrintQueue.objects.select_related("plate__print_job", "printer"),
            pk=pk,
            status=PrintQueue.STATUS_PRINTING,
        )
        printer = entry.printer
        plate_label = f"Plate {entry.plate.plate_number} of '{entry.print_job}'"

        try:
            backend = get_printer_backend(printer)
            backend.cancel_print()
        except PrinterError as exc:
            logger.warning("Cancel command failed for entry %s: %s", pk, exc)
            messages.warning(
                request,
                f"Cancel command failed ({exc}), but entry marked for review.",
            )

        entry.status = PrintQueue.STATUS_AWAITING_REVIEW
        entry.completed_at = timezone.now()
        entry.save(update_fields=["status", "completed_at"])

        messages.info(
            request,
            f"{plate_label} on {printer.name} cancelled — please review.",
        )
        return redirect("core:printqueue_list")
