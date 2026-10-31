"""Print job views for the LayerNexus application."""

import logging
import re
from collections import defaultdict
from collections.abc import Iterable
from typing import Any
from urllib.parse import urlencode

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import PermissionDenied
from django.db import DatabaseError, models, transaction
from django.db.models import Prefetch, Q, QuerySet
from django.forms import Form
from django.http import HttpRequest, HttpResponse, JsonResponse, QueryDict
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse, reverse_lazy
from django.views import View
from django.views.generic import CreateView, DeleteView, DetailView, ListView, UpdateView

from core.forms import AddPartToJobForm, PrintJobForm
from core.mixins import RoleRequiredMixin
from core.models import (
    OrcaMachineProfile,
    Part,
    PrinterProfile,
    PrintJob,
    PrintJobPart,
    PrintJobPlate,
    PrintQueue,
    Project,
    SpoolmanFilamentMapping,
)
from core.services.filament_display import filament_badges, filament_label, mapping_lookup
from core.services.queue import QUEUE_EXECUTION_ORDER
from core.services.slicing_worker import _start_orcaslicer_worker

logger = logging.getLogger(__name__)

# Plain ASCII digits, bounded so int() and the SQLite integer binding cannot overflow.
_PK_RE = re.compile(r"[0-9]{1,18}")

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


class _IncompatiblePartError(Exception):
    """Internal signal: a part cannot be added to a job (preset/filament conflict).

    Raised inside the ``AddPartToJobView`` transaction so a conflict rolls back any
    in-transaction pin/insert; caught by the view to show the user-facing message.
    """


class PrintJobListView(LoginRequiredMixin, ListView):
    """List print jobs with server-side filtering and pagination.

    Supported GET params: ``q`` (job or part name) plus the repeatable
    multi-select params ``status`` (derived display status, see
    :attr:`PrintJob.display_status`), ``printer`` (pk; jobs with a print
    attempt on that printer), ``project`` (pk of a
    top-level project; a job matches when any of its parts lies in that project's
    subtree) and ``filament`` (Spoolman filament id, or ``none``
    for jobs with a part that has no filament assigned).  Nothing selected means no filter; invalid
    values are ignored.  Newest jobs come first.
    """

    model = PrintJob
    template_name = "core/printjob_list.html"
    context_object_name = "print_jobs"
    paginate_by = 25

    session_key = "printjob_list_filters"
    filter_keys = ("q", "status", "printer", "project", "filament")
    no_filament = "none"

    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        """Remember the last filters per session and restore them on a bare visit.

        Explicit filter params are saved; ``?reset=1`` clears them; a request
        without any filter param is redirected to the saved filters (``page`` is dropped).
        """
        list_url = reverse("core:printjob_list")
        if "reset" in request.GET:
            request.session.pop(self.session_key, None)
            return redirect(list_url)
        if any(key in request.GET for key in self.filter_keys):
            filters = self._filters()
            if any(filters.values()):
                request.session[self.session_key] = filters
            else:
                request.session.pop(self.session_key, None)
        else:
            saved = self._saved_filters()
            if saved:
                # Drop ``page``: it was computed for the unfiltered list and may not exist once filtered.
                return redirect(f"{list_url}?{self._querystring(saved)}")
        return super().get(request, *args, **kwargs)

    def _saved_filters(self) -> dict[str, str | list[str]]:
        """Return the session-saved filters, silently dropping values that are no longer valid."""
        saved = self.request.session.get(self.session_key)
        if not isinstance(saved, dict):
            return {}
        params = QueryDict(mutable=True)
        for key in self.filter_keys:
            value = saved.get(key)
            params.setlist(key, [value] if isinstance(value, str) else [v for v in value or [] if isinstance(v, str)])
        filters = self._sanitize(params)
        filters["printer"] = self._existing(PrinterProfile, filters["printer"])
        filters["project"] = self._existing_roots(filters["project"])
        available = {option["value"] for option in self._filament_options()}
        filters["filament"] = [v for v in filters["filament"] if v in available]
        return filters if any(filters.values()) else {}

    @staticmethod
    def _existing(model: type[models.Model], pks: list[str]) -> list[str]:
        """Return the pks (as strings, in order) that still exist for ``model``."""
        if not pks:
            return []
        found = {str(pk) for pk in model.objects.filter(pk__in=[int(pk) for pk in pks]).values_list("pk", flat=True)}
        return [pk for pk in pks if pk in found]

    @staticmethod
    def _existing_roots(pks: list[str]) -> list[str]:
        """Return the pks (as strings, in order) of existing top-level projects."""
        if not pks:
            return []
        found = {
            str(pk)
            for pk in Project.objects.filter(pk__in=[int(pk) for pk in pks], parent__isnull=True).values_list(
                "pk", flat=True
            )
        }
        return [pk for pk in pks if pk in found]

    def _filters(self) -> dict[str, str | list[str]]:
        """Return the sanitized filter values from the query string (cached per request).

        Project values that are not (or no longer) top-level projects are dropped.
        """
        if not hasattr(self, "_cached_filters"):
            filters = self._sanitize(self.request.GET)
            filters["project"] = self._existing_roots(filters["project"])
            self._cached_filters = filters
        return self._cached_filters

    def _sanitize(self, params: QueryDict) -> dict[str, str | list[str]]:
        """Sanitize raw filter params (a QueryDict) into validated values."""
        valid_status = dict(PrintJob.DISPLAY_CHOICES)
        return {
            "q": params.get("q", "").strip(),
            "status": self._unique(v.strip() for v in params.getlist("status") if v.strip() in valid_status),
            "printer": self._unique(v.strip() for v in params.getlist("printer") if _PK_RE.fullmatch(v.strip())),
            "project": self._unique(v.strip() for v in params.getlist("project") if _PK_RE.fullmatch(v.strip())),
            "filament": self._unique(
                v.strip()
                for v in params.getlist("filament")
                if v.strip() == self.no_filament or _PK_RE.fullmatch(v.strip())
            ),
        }

    @staticmethod
    def _unique(values: Iterable[str]) -> list[str]:
        """Return the values de-duplicated, preserving order."""
        return list(dict.fromkeys(values))

    def get_queryset(self) -> QuerySet:
        """Return the filtered, newest-first queryset."""
        filters = self._filters()
        queryset = (
            PrintJob.objects.with_display_status()
            .select_related("machine_profile")
            .prefetch_related("job_parts__part", "plates")
        )
        # Join filters go through ``pk__in`` subqueries: no row fan-out, so no ``distinct()``
        # (which would make the database compute the display-status subqueries for every row).
        if filters["q"]:
            queryset = queryset.filter(
                Q(name__icontains=filters["q"])
                | Q(pk__in=PrintJobPart.objects.filter(part__name__icontains=filters["q"]).values("print_job_id"))
            )
        if filters["status"]:
            queryset = queryset.filter(annotated_display_status__in=filters["status"])
        if filters["printer"]:
            attempts = PrintQueue.objects.filter(printer_id__in=[int(v) for v in filters["printer"]])
            queryset = queryset.filter(pk__in=attempts.values("plate__print_job_id"))
        if filters["project"]:
            project_ids: set[int] = set()
            for root in Project.objects.filter(pk__in=[int(v) for v in filters["project"]]):
                project_ids |= root.subtree_ids()
            queryset = queryset.filter(
                pk__in=PrintJobPart.objects.filter(part__project_id__in=project_ids).values("print_job_id")
            )
        if filters["filament"]:
            condition = Q(part__spoolman_filament_id__in=[int(v) for v in filters["filament"] if v != self.no_filament])
            if self.no_filament in filters["filament"]:
                condition |= Q(part__spoolman_filament_id__isnull=True)
            queryset = queryset.filter(pk__in=PrintJobPart.objects.filter(condition).values("print_job_id"))
        return queryset.order_by("-created_at", "-pk")

    def _filament_options(self) -> list[dict[str, str]]:
        """Return the filament filter options: filaments used by any job, plus ``none`` if some part has none."""
        rows = (
            PrintJobPart.objects.order_by("part__spoolman_filament_id", "part__material")
            .values_list("part__spoolman_filament_id", "part__material")
            .distinct()
        )
        materials: dict[int, str] = {}
        has_none = False
        for filament_id, material in rows:
            if filament_id is None:
                has_none = True
            elif not materials.get(filament_id):
                materials[filament_id] = material
        mappings = mapping_lookup(set(materials))
        options = [
            {
                "value": str(filament_id),
                "label": filament_label(
                    filament_id,
                    mappings[filament_id].spoolman_filament_name if filament_id in mappings else "",
                    material,
                ),
            }
            for filament_id, material in materials.items()
        ]
        options.sort(key=lambda option: option["label"].lower())
        if has_none:
            options.append({"value": self.no_filament, "label": "No filament assigned"})
        return options

    @staticmethod
    def _attach_filament_badges(jobs: Iterable[PrintJob]) -> None:
        """Set ``filament_badges`` on each job using one mapping query (parts are already prefetched)."""
        jobs = list(jobs)
        ids = {
            jp.part.spoolman_filament_id for job in jobs for jp in job.job_parts.all() if jp.part.spoolman_filament_id
        }
        mappings = mapping_lookup(ids)
        for job in jobs:
            job.filament_badges = filament_badges([jp.part for jp in job.job_parts.all()], mappings)

    @staticmethod
    def _querystring(filters: dict[str, str | list[str]]) -> str:
        """Encode filters (repeating multi-value keys) for links that must keep them."""
        pairs = []
        for key, value in filters.items():
            for item in [value] if isinstance(value, str) else value:
                if item:
                    pairs.append((key, item))
        return urlencode(pairs)

    @staticmethod
    def _summary(selected: list[str], all_label: str) -> str:
        """Return the dropdown button label for a multi-select filter."""
        return f"{len(selected)} selected" if selected else all_label

    def get_context_data(self, **kwargs) -> dict:
        """Add filter values, filter options and a pagination query string."""
        context = super().get_context_data(**kwargs)
        filters = self._filters()
        context.update(filters)
        context["status_choices"] = PrintJob.DISPLAY_CHOICES
        context["printers"] = PrinterProfile.objects.order_by("name")
        context["projects"] = Project.objects.filter(parent__isnull=True).order_by("name")
        context["status_summary"] = self._summary(filters["status"], "All statuses")
        context["printer_summary"] = self._summary(filters["printer"], "All printers")
        context["project_summary"] = self._summary(filters["project"], "All projects")
        context["filament_options"] = self._filament_options()
        context["filament_summary"] = self._summary(filters["filament"], "All filaments")
        self._attach_filament_badges(context["print_jobs"])
        context["filters_active"] = any(filters.values())
        context["filter_querystring"] = self._querystring(filters)
        # "Hide finished": drop the finished display statuses (completed, partially failed).
        shown = filters["status"] or [value for value, _label in PrintJob.DISPLAY_CHOICES]
        hide_completed = [s for s in shown if s not in PrintJob.FINISHED_DISPLAY_STATUSES]
        context["show_hide_completed"] = any(s in PrintJob.FINISHED_DISPLAY_STATUSES for s in shown) and bool(
            hide_completed
        )
        context["hide_completed_querystring"] = self._querystring({**filters, "status": hide_completed})
        return context


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
        return PrintJob.objects.select_related("machine_profile").prefetch_related(
            "job_parts__part__project",
            Prefetch(
                "plates",
                queryset=PrintJobPlate.objects.prefetch_related("queue_entries__printer", "plate_parts__part"),
            ),
        )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        job = self.object
        # Cover the ancestor chain so ``path_label`` renders without a query per row.
        context["job_parts"] = job.job_parts.select_related("part__project__parent__parent__parent")
        plates = list(job.plates.all())
        self._attach_queue_labels(plates)
        part_labels = job.part_labels()
        for plate in plates:
            plate.part_labels = part_labels
        context["plates"] = plates
        context["first_queueable_plate"] = next((plate for plate in plates if plate.is_queueable), None)
        context["bed_dimensions"] = job.bed_dimensions
        context["job_started_at"] = job.started_at
        context["job_completed_at"] = job.completed_at
        context["plate_warnings"] = job.plate_assignment_warnings()
        has_parts = job.job_parts.exists()
        parts_valid = has_parts and all(jp.part.stl_file for jp in job.job_parts.select_related("part"))
        has_machine = job.machine_profile is not None
        context["can_change_contents"] = job.can_change_contents
        context["can_slice"] = job.status == PrintJob.STATUS_DRAFT and has_machine and parts_valid
        context["can_reslice"] = (
            job.status in (PrintJob.STATUS_SLICED, PrintJob.STATUS_FAILED)
            and job.can_reslice
            and has_machine
            and parts_valid
        )
        context["missing_machine_profile"] = job.status == PrintJob.STATUS_DRAFT and not has_machine

        # Resolve effective print preset and filament profile from first part
        first_jp = job.job_parts.select_related("part__print_preset").first()
        if first_jp:
            # Prefer the preset pinned on the job; fall back to the first
            # part's own preset only for legacy jobs created before pinning existed.
            context["effective_print_preset"] = job.print_preset or first_jp.part.effective_print_preset
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

    @staticmethod
    def _attach_queue_labels(plates: list[PrintJobPlate]) -> None:
        """Set ``queue_label`` on each plate with an open attempt (Topic 31).

        "Queued · <printer>, position N" for a waiting attempt (N = its 1-based
        place in the printer's execution order), otherwise "<state> · <printer>".
        The positions of all affected printers come from one query.
        """
        waiting = {plate.pk: plate.open_attempt for plate in plates if plate.state == PrintJobPlate.STATE_WAITING}
        printer_ids = {attempt.printer_id for attempt in waiting.values() if attempt.printer_id is not None}
        rank: dict[int, int] = {}
        per_printer: dict[int, int] = defaultdict(int)
        for pk, printer_id in (
            PrintQueue.objects.waiting()
            .filter(printer_id__in=printer_ids)
            .order_by("printer_id", *QUEUE_EXECUTION_ORDER)
            .values_list("pk", "printer_id")
        ):
            per_printer[printer_id] += 1
            rank[pk] = per_printer[printer_id]
        for plate in plates:
            attempt = plate.open_attempt
            if attempt is None:
                plate.queue_label = ""
                continue
            printer = attempt.printer.name if attempt.printer is not None else "unknown printer"
            if plate.state == PrintJobPlate.STATE_WAITING and attempt.pk in rank:
                plate.queue_label = f"{plate.get_state_display()} · {printer}, position {rank[attempt.pk]}"
            else:
                plate.queue_label = f"{plate.get_state_display()} · {printer}"


class PrintJobUpdateView(RoleRequiredMixin, UpdateView):
    """Update an existing print job.

    Name and notes are always editable.  The machine profile (which decides
    the slicing) only while :attr:`PrintJob.can_change_contents` — no print
    attempt yet and not slicing; changing it on a sliced job discards the
    plates and puts the job back to draft.
    """

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
        if not self.object.can_change_contents:
            # Disabled fields ignore posted values: the profile stays as it is.
            form.fields["machine_profile"].disabled = True
            form.fields["machine_profile"].help_text = "Fixed once the job has print attempts (or while it is slicing)."
        return form

    def form_valid(self, form):
        """Save; a changed machine profile on a sliced job discards its plates (back to draft)."""
        job = self.object
        if "machine_profile" not in form.changed_data:
            # Only name / notes: write just those columns, so a concurrent slicing
            # worker's status and stats are neither overwritten nor stale.
            self.object = form.save(commit=False)
            try:
                self.object.save(update_fields=[*form.changed_data, "updated_at"])
            except DatabaseError:
                # update_fields affected no row: the job was deleted meanwhile.
                messages.error(self.request, "This print job no longer exists.")
                return redirect("core:printjob_list")
            messages.success(self.request, "Print job updated.")
            return redirect(self.get_success_url())
        try:
            with transaction.atomic():
                # Decide on the current row (locked), not on the job loaded with the request.
                current = job.lock_for_contents_change()
                self.object = form.save(commit=False)
                self.object.save(update_fields=[*form.changed_data, "updated_at"])
                if current.status != PrintJob.STATUS_DRAFT:
                    job.discard_slicing()
                    messages.info(self.request, "Machine profile changed: plates discarded, the job is a draft again.")
        except ValueError:
            messages.error(self.request, "The machine profile can no longer be changed (print started or slicing).")
            return redirect("core:printjob_detail", pk=job.pk)
        messages.success(self.request, "Print job updated.")
        return redirect(self.get_success_url())

    def get_success_url(self):
        return reverse("core:printjob_detail", kwargs={"pk": self.object.pk})


class PrintJobDeleteView(RoleRequiredMixin, DeleteView):
    """Delete a print job.

    Only while :attr:`PrintJob.can_delete`: a job with any print attempt is not
    deletable — its history must be kept.  A waiting, never started attempt
    must be removed from the queue first (it is deleted then).
    """

    permission_required = "core.delete_printjob"

    model = PrintJob
    template_name = "core/printjob_confirm_delete.html"
    success_url = reverse_lazy("core:printjob_list")

    def get_queryset(self) -> QuerySet[PrintJob]:
        """Annotate the display status, so the confirmation page needs no query per plate."""
        return PrintJob.objects.with_display_status()

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        """Add whether the job has print attempts (deletion refused)."""
        context = super().get_context_data(**kwargs)
        context["has_print_history"] = not self.object.can_delete
        return context

    def form_valid(self, form: Form) -> HttpResponse:
        """Delete the job unless it has print attempts (re-checked in the delete transaction)."""
        job = self.object
        with transaction.atomic():
            # Live check (not the annotation loaded with the page): an attempt may have appeared since.
            if PrintQueue.objects.filter(plate__print_job=job).exists():
                messages.error(
                    self.request,
                    f"Cannot delete print job '{job}': it has print attempts. "
                    "Remove waiting attempts from the queue first; print history is kept.",
                )
                return redirect("core:printjob_detail", pk=job.pk)
            response = super().form_valid(form)
        messages.success(self.request, f"Print job '{job}' deleted (including plates).")
        return response


class AddPartToJobView(RoleRequiredMixin, View):
    """Add a part to an existing job (not printing yet) or create a new draft job.

    Adding to a sliced (or slicing-failed) job discards its plates; the job is a
    draft again and the user starts the next slice.

    POST parameters:
        - job: PK of an existing job whose contents may change, or empty for a new job.
        - quantity: Number of copies to add.
    """

    permission_required = "core.add_printjob"

    def post(self, request: HttpRequest, part_pk: int) -> HttpResponse:
        """Handle adding a part to a print job."""
        part = get_object_or_404(Part, pk=part_pk)

        if not part.stl_file:
            messages.error(request, f"Part '{part.name}' has no model file.")
            return redirect("core:part_detail", pk=part.pk)

        form = AddPartToJobForm(request.POST, user=request.user)
        if not form.is_valid():
            messages.error(request, "Invalid form data.")
            return redirect("core:part_detail", pk=part.pk)

        job = form.cleaned_data.get("job")
        quantity = form.cleaned_data["quantity"]

        if job is not None and not request.user.has_perm("core.change_printjob"):
            # Changing an existing job may discard its slicing result — same permission as editing it.
            raise PermissionDenied("Changing an existing print job requires the change_printjob permission.")

        # The job is pinned to the part's bottom-up resolved preset.
        incoming_preset_id = part.effective_print_preset_id
        if incoming_preset_id is None:
            # No override and no project default up the chain: the job would slice with a
            # None preset and fail. Refuse rather than create an unsliceable job.
            messages.error(
                request,
                f"Part '{part.name}' has no resolvable print preset "
                f"(no override and no project default) — set a preset before adding it to a job.",
            )
            return redirect("core:part_detail", pk=part.pk)

        if not job:
            # Create a new draft job pinned to the resolved preset.
            job = PrintJob.objects.create(
                name=f"Job with {part.name}",
                status=PrintJob.STATUS_DRAFT,
                created_by=request.user,
                print_preset_id=incoming_preset_id,
            )
            messages.info(request, f"New draft job '{job}' created.")

        if not job.can_change_contents:
            messages.error(
                request, "Parts can only be added to jobs that have not started printing (and are not slicing)."
            )
            return redirect("core:part_detail", pk=part.pk)

        # Do the compatibility check, the empty-draft pin, and the part insert in one
        # transaction so concurrent adds can't interleave ("one preset per job").
        discarded = False
        try:
            with transaction.atomic():
                if job.lock_for_contents_change().status != PrintJob.STATUS_DRAFT:
                    job.discard_slicing()
                    discarded = True
                existing_parts = list(job.job_parts.select_related("part").all())
                if existing_parts:
                    # Compare the resolved incoming preset against the job's pinned preset,
                    # or — for a legacy unpinned job — the existing parts' presets.
                    filament_conflict = any(
                        jp.part.spoolman_filament_id != part.spoolman_filament_id for jp in existing_parts
                    )
                    if job.print_preset_id is not None:
                        preset_conflict = job.print_preset_id != incoming_preset_id
                    else:
                        preset_conflict = any(
                            jp.part.effective_print_preset_id != incoming_preset_id for jp in existing_parts
                        )
                    if filament_conflict or preset_conflict:
                        raise _IncompatiblePartError
                else:
                    # Empty draft: atomically pin it to the resolved preset. Only one
                    # concurrent request's conditional update wins; a loser re-reads the
                    # pinned value and, if it differs, is rejected (so two parts can't land
                    # in one job with mismatched presets).
                    PrintJob.objects.filter(pk=job.pk, print_preset__isnull=True).update(
                        print_preset_id=incoming_preset_id
                    )
                    job.refresh_from_db(fields=["print_preset"])
                    if job.print_preset_id != incoming_preset_id:
                        raise _IncompatiblePartError

                job_part, created = PrintJobPart.objects.get_or_create(
                    print_job=job,
                    part=part,
                    defaults={"quantity": quantity},
                )
                if not created:
                    job_part.quantity += quantity
                    job_part.save(update_fields=["quantity"])
        except ValueError:
            messages.error(
                request, "Parts can only be added to jobs that have not started printing (and are not slicing)."
            )
            return redirect("core:part_detail", pk=part.pk)
        except _IncompatiblePartError:
            messages.error(
                request,
                f"Part '{part.name}' has a different preset or filament "
                f"than the existing parts in '{job}'. "
                f"All parts in a job must use the same preset and filament.",
            )
            return redirect("core:part_detail", pk=part.pk)

        if discarded:
            messages.info(
                request, f"Plates of '{job}' discarded: the job is a draft again — slice it once it is complete."
            )
        if created:
            messages.success(request, f"Added {quantity}× '{part.name}' to '{job}'.")
        else:
            messages.success(request, f"Updated '{part.name}' quantity to {job_part.quantity} in '{job}'.")

        return redirect("core:printjob_detail", pk=job.pk)


class RemovePartFromJobView(RoleRequiredMixin, View):
    """Remove a part from a print job that has not started printing.

    Removing it from a sliced (or slicing-failed) job discards the plates; the
    job is a draft again.
    """

    permission_required = "core.change_printjob"

    def post(self, request: HttpRequest, job_pk: int, job_part_pk: int) -> HttpResponse:
        """Remove a PrintJobPart entry."""
        job = get_object_or_404(PrintJob, pk=job_pk)
        refused = "Parts can only be removed from jobs that have not started printing (and are not slicing)."
        if not job.can_change_contents:
            messages.error(request, refused)
            return redirect("core:printjob_detail", pk=job.pk)

        job_part = get_object_or_404(PrintJobPart, pk=job_part_pk, print_job=job)
        part_name = job_part.part.name
        discarded = False
        try:
            with transaction.atomic():
                if job.lock_for_contents_change().status != PrintJob.STATUS_DRAFT:
                    job.discard_slicing()
                    discarded = True
                job_part.delete()
        except ValueError:
            messages.error(request, refused)
            return redirect("core:printjob_detail", pk=job.pk)
        if discarded:
            messages.info(request, "Plates discarded: the job is a draft again — slice it once it is complete.")
        messages.success(request, f"Removed '{part_name}' from job.")
        return redirect("core:printjob_detail", pk=job.pk)


class CreateJobsFromProjectView(RoleRequiredMixin, View):
    """Bulk-create draft print jobs from all eligible parts in a project.

    Groups the subtree's parts by ``(effective_print_preset_id, spoolman_filament_id)``
    so that each job contains only compatible parts.  Each part gets
    ``needed − printed`` copies (``needed`` from the tree multiplier, ``printed``
    = :attr:`Part.printed_quantity`: passed plates, all-plates jobs and manual
    bookings); parts without a model file, without a
    resolvable preset, or with nothing remaining are skipped.

    Each job is named ``"<Project> — <Material / Preset>"``.
    """

    permission_required = "core.add_printjob"

    def post(self, request: HttpRequest, pk: int) -> HttpResponse:
        """Create one draft job per preset/filament group.

        The tree read and all inserts run in one transaction, so a concurrent subtree
        delete cannot leave a half-filled draft job behind.
        """
        with transaction.atomic():
            return self._create_jobs(request, pk)

    def _create_jobs(self, request: HttpRequest, pk: int) -> HttpResponse:
        """Build the preset/filament groups for ``pk`` and create the draft jobs."""
        project = get_object_or_404(Project.objects.prefetch_related(*Project.aggregate_prefetch_lookups()), pk=pk)

        groups: dict[tuple[int, int | None], list[tuple[Part, int]]] = defaultdict(list)
        skipped_no_preset = 0
        # ``build_progress`` scales the need to the whole tree (also when started on a
        # sub-project), matching the tree-wide ``printed`` count.
        for row in project.build_progress()["parts"]:
            part = row["part"]
            if not part.stl_file:
                continue
            preset_id = part.effective_print_preset_id
            if preset_id is None:
                # No override and no project default up the chain: skip rather than
                # create a job that would slice with None.
                skipped_no_preset += 1
                continue
            remaining = row["remaining"]
            if remaining > 0:
                groups[(preset_id, part.spoolman_filament_id)].append((part, remaining))

        if skipped_no_preset:
            messages.warning(
                request,
                f"Skipped {skipped_no_preset} part(s) with no resolvable print preset "
                f"(no override and no project default) — set a preset to include them.",
            )

        if not groups:
            messages.warning(request, "No eligible parts found (all printed, missing model file, or no preset).")
            return redirect("core:project_detail", pk=project.pk)

        jobs_created = 0
        parts_added = 0

        for (preset_id, filament_id), group_parts in groups.items():
            # Build a descriptive job name
            label_parts: list[str] = [project.name]
            if filament_id:
                # Use filament name from Spoolman mapping, fall back to material
                mapping = SpoolmanFilamentMapping.objects.filter(
                    spoolman_filament_id=filament_id,
                ).first()
                filament_name = filament_label(
                    filament_id,
                    mapping.spoolman_filament_name if mapping else "",
                    next((p.material for p, _ in group_parts if p.material), None),
                )
                label_parts.append(filament_name)
            if len(groups) > 1:
                label_parts.append(f"Group {jobs_created + 1}")

            job_name = " — ".join(label_parts)

            job = PrintJob.objects.create(
                name=job_name,
                status=PrintJob.STATUS_DRAFT,
                created_by=request.user,
                print_preset_id=preset_id,
            )

            for part, remaining in group_parts:
                PrintJobPart.objects.create(
                    print_job=job,
                    part=part,
                    quantity=remaining,
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

    Collects all model files (STL or 3MF) from the job's parts, creates a
    3MF bundle, and starts background slicing via OrcaSlicer.
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

        # Validate all parts have model files
        missing_files = [jp.part.name for jp in job_parts if not jp.part.stl_file]
        if missing_files:
            messages.error(
                request,
                f"Parts without model files: {', '.join(missing_files)}",
            )
            return redirect("core:printjob_detail", pk=pk)

        with transaction.atomic():
            # Checked together with the plate delete: re-slicing deletes all
            # plates, which must never take print attempts (history) along.
            if not job.can_reslice:
                messages.error(request, "This job has print attempts and cannot be re-sliced.")
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
        plates = [
            {"plate_number": plate.plate_number, "state": plate.state, "filament_used_grams": plate.filament_used_grams}
            for plate in job.plates.with_state()
        ]
        data = {
            "status": job.status,
            "error": job.slicing_error,
            "filament_used_grams": job.filament_used_grams,
            "print_time_estimate": (str(job.print_time_estimate) if job.print_time_estimate else None),
            "plate_count": len(plates),
            "plates": plates,
        }
        return JsonResponse(data)
