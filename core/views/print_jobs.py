"""Print job views for the LayerNexus application."""

import logging
from collections import defaultdict

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse, reverse_lazy
from django.views import View
from django.views.generic import CreateView, DeleteView, DetailView, ListView, UpdateView

from core.forms import AddPartToJobForm, PrintJobForm
from core.mixins import RoleRequiredMixin
from core.models import (
    OrcaMachineProfile,
    Part,
    PrintJob,
    PrintJobPart,
    Project,
    SpoolmanFilamentMapping,
)
from core.services.slicing_worker import _start_orcaslicer_worker

logger = logging.getLogger(__name__)

__all__ = [
    "PrintJobListView",
    "PrintJobCreateView",
    "PrintJobDetailView",
    "PrintJobUpdateView",
    "PrintJobDeleteView",
    "AddPartToJobView",
    "RemovePartFromJobView",
    "CreateJobsFromProjectView",
    "PrintJobSliceView",
    "SliceJobStatusView",
]


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
