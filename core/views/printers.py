"""Printer profile views for the LayerNexus application."""

import logging

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.http import JsonResponse
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
from core.services.moonraker import MoonrakerClient, MoonrakerError

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
