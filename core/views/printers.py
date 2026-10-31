"""Printer profile views for the LayerNexus application."""

import logging
from typing import Any

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.db import IntegrityError
from django.db.models.deletion import ProtectedError
from django.forms import Form
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse, reverse_lazy
from django.views import View
from django.views.generic import CreateView, DeleteView, ListView, UpdateView

from core.forms import CostProfileForm, PrinterProfileForm
from core.mixins import PrinterControlMixin, PrinterManageMixin
from core.models import (
    CostProfile,
    OrcaMachineProfile,
    PrinterProfile,
    PrintJobPlate,
)
from core.services.printer_backend import (
    PrinterError,
    PrinterNotConfiguredError,
    get_printer_backend,
)

logger = logging.getLogger(__name__)

__all__ = [
    "PrinterProfileListView",
    "PrinterProfileCreateView",
    "PrinterProfileUpdateView",
    "PrinterProfileDeleteView",
    "CostProfileUpdateView",
    "PrinterStatusView",
    "UploadToPrinterView",
]


class PrinterProfileListView(LoginRequiredMixin, ListView):
    """List all printer profiles."""

    model = PrinterProfile
    template_name = "core/printerprofile_list.html"
    context_object_name = "printer_profiles"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        # Check real printer backend connectivity for each printer
        statuses = {}
        for printer in context["printer_profiles"]:
            if not printer.is_active:
                statuses[printer.pk] = "inactive"
                continue
            try:
                backend = get_printer_backend(printer)
                backend.get_printer_status()
                statuses[printer.pk] = "online"
            except PrinterNotConfiguredError:
                statuses[printer.pk] = "unconfigured"
            except PrinterError:
                statuses[printer.pk] = "offline"
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
    """Delete a printer profile.

    A printer with print attempts (queue entries, incl. history) cannot be
    deleted — ``PrintQueue.printer`` is ``PROTECT``.  The page explains this
    and offers deactivating the printer instead.
    """

    model = PrinterProfile
    template_name = "core/printerprofile_confirm_delete.html"
    success_url = reverse_lazy("core:printerprofile_list")

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        """Add whether the printer has print history (deletion refused)."""
        context = super().get_context_data(**kwargs)
        context["has_print_history"] = self.object.has_print_history()
        return context

    def form_valid(self, form: Form) -> HttpResponse:
        """Delete the printer, or refuse with a message if it has print history.

        The ``PROTECT`` foreign key makes the database enforce the guard, so an
        attempt created after the page was rendered cannot be lost either.
        """
        name = self.object.name
        try:
            response = super().form_valid(form)
        except (ProtectedError, IntegrityError):
            messages.error(
                self.request,
                f"Cannot delete printer '{name}': it has print history. Deactivate it instead.",
            )
            return redirect("core:printerprofile_delete", pk=self.object.pk)
        messages.success(self.request, "Printer profile deleted.")
        return response


class PrinterStatusView(LoginRequiredMixin, View):
    """API proxy to get printer status from the configured backend."""

    def get(self, request, printer_pk):
        printer = get_object_or_404(PrinterProfile, pk=printer_pk)
        try:
            backend = get_printer_backend(printer)
            status = backend.get_printer_status()
            return JsonResponse({"status": status})
        except PrinterNotConfiguredError as exc:
            return JsonResponse({"error": str(exc)}, status=400)
        except PrinterError as exc:
            logger.exception("Printer backend error for printer %s: %s", printer.pk, exc)
            msg = "Could not retrieve printer status. Check server logs for details."
            return JsonResponse({"error": msg}, status=502)


class UploadToPrinterView(PrinterControlMixin, View):
    """Upload a plate's G-code to the printer via the configured backend."""

    def post(self, request, plate_pk):
        plate = get_object_or_404(
            PrintJobPlate.objects.select_related("print_job__printer"),
            pk=plate_pk,
        )
        job = plate.print_job

        if not plate.gcode_file:
            messages.error(request, "No G-code file for this plate.")
            return redirect("core:printjob_detail", pk=job.pk)

        if not job.printer:
            messages.error(request, "No printer assigned to this job.")
            return redirect("core:printjob_detail", pk=job.pk)

        try:
            backend = get_printer_backend(job.printer)
            # Upload only: the plate status follows print attempts (queue), not uploads.
            backend.upload_gcode(plate.gcode_file.path)
            messages.success(request, f"G-code for plate {plate.plate_number} uploaded to printer.")
        except PrinterNotConfiguredError as exc:
            messages.error(request, str(exc))
        except PrinterError as exc:
            logger.exception("Upload error for plate %s: %s", plate.pk, exc)
            messages.error(request, "G-code upload failed. Please check the printer connection and try again.")

        return redirect("core:printjob_detail", pk=job.pk)


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
