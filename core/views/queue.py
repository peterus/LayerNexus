"""Print queue views for the LayerNexus application."""

import logging
from datetime import datetime, timedelta
from typing import Any

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.db import IntegrityError, transaction
from django.db.models import Case, IntegerField, OuterRef, QuerySet, Subquery, Value, When
from django.http import (
    Http404,
    HttpRequest,
    HttpResponse,
    HttpResponseBadRequest,
    HttpResponseRedirect,
    JsonResponse,
)
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse, reverse_lazy
from django.utils import timezone
from django.utils.functional import cached_property
from django.views import View
from django.views.generic import CreateView, DeleteView, FormView, ListView

from core.forms import EnqueueAllPlatesForm, PrintQueueForm, ScheduledStartForm
from core.mixins import PrinterControlMixin, QueueDequeueMixin, QueueManageMixin
from core.models import (
    PrinterProfile,
    PrintJob,
    PrintJobPlate,
    PrintQueue,
    ScheduledQueueStart,
)
from core.services.printer_backend import (
    PrinterError,
    get_printer_backend,
)
from core.services.queue import (
    MOVE_DIRECTIONS,
    QUEUE_EXECUTION_ORDER,
    QueueReorderError,
    ReviewStateError,
    move_queue_entry,
    queue_entry_status_label,
    review_queue_entry,
    start_next_for_printer,
)
from core.services.scheduled_start import cancel_scheduled_start, schedule_start

logger = logging.getLogger(__name__)

__all__ = [
    "PrintQueueListView",
    "PrintQueueCreateView",
    "PrintJobEnqueueAllView",
    "PrintQueueDeleteView",
    "PrintQueueMoveView",
    "RunNextQueueView",
    "RunAllQueuesView",
    "QueueEntryReviewView",
    "QueueCheckPrinterStatusView",
    "CancelQueueEntryView",
    "ScheduleQueueStartView",
    "CancelScheduledStartView",
]


def _next_morning(hour: int = 6) -> datetime:
    """Return the next ``hour``:00 in the current time zone (today if still ahead, else tomorrow)."""
    now = timezone.localtime()
    target = now.replace(hour=hour, minute=0, second=0, microsecond=0)
    return target if target > now else target + timedelta(days=1)


def _queueable_plates() -> QuerySet[PrintJobPlate]:
    """Return plates that can be added to the print queue.

    See :meth:`PrintJobPlateQuerySet.queueable`: the job is sliced, the plate
    has G-code and is neither completed nor queued (its state is ``not_queued``
    or ``failed``).

    Returns:
        Queryset of queueable ``PrintJobPlate`` objects.
    """
    return PrintJobPlate.objects.queueable()


class PrintQueueListView(LoginRequiredMixin, ListView):
    """Show the print queue for all printers.

    Per printer, active entries (printing / awaiting review) come first,
    followed by the waiting entries in their actual execution order
    (``QUEUE_EXECUTION_ORDER``).  Waiting entries get a 1-based
    ``queue_rank`` and ``can_move_up`` / ``can_move_down`` flags for the
    reorder controls.
    """

    model = PrintQueue
    template_name = "core/printqueue_list.html"
    context_object_name = "queue_entries"

    def get_queryset(self):
        return (
            PrintQueue.objects.open()
            .with_retry_count()
            .with_exclusion_count()
            .select_related("plate__print_job", "printer")
            .annotate(
                waiting_last=Case(
                    When(started_at__isnull=True, completed_at__isnull=True, then=Value(1)),
                    default=Value(0),
                    output_field=IntegerField(),
                )
            )
            .order_by("printer__name", "printer_id", "waiting_last", *QUEUE_EXECUTION_ORDER)
        )

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        """Annotate waiting entries with their queue rank and move flags."""
        context = super().get_context_data(**kwargs)
        entries = list(context["object_list"])
        waiting_by_printer: dict[int, list[PrintQueue]] = {}
        for entry in entries:
            entry.queue_rank = None
            entry.can_move_up = False
            entry.can_move_down = False
            if entry.is_waiting:
                waiting_by_printer.setdefault(entry.printer_id, []).append(entry)
        for waiting in waiting_by_printer.values():
            for rank, entry in enumerate(waiting, start=1):
                entry.queue_rank = rank
                entry.can_move_up = rank > 1
                entry.can_move_down = rank < len(waiting)
        context["object_list"] = entries
        context[self.context_object_name] = entries
        self._annotate_schedules(entries)
        context["default_schedule_start"] = _next_morning().strftime("%Y-%m-%dT%H:%M")
        return context

    @staticmethod
    def _annotate_schedules(entries: list[PrintQueue]) -> None:
        """Attach the pending schedule and the last result to each entry's printer."""
        printer_ids = {entry.printer_id for entry in entries}
        pending = {
            row.printer_id: row
            for row in ScheduledQueueStart.objects.filter(
                printer_id__in=printer_ids, status=ScheduledQueueStart.STATUS_PENDING
            )
        }
        latest_finished = (
            ScheduledQueueStart.objects.filter(printer=OuterRef("pk"))
            .exclude(status=ScheduledQueueStart.STATUS_PENDING)
            .order_by("-executed_at", "-start_at")
            .values("pk")[:1]
        )
        last_ids = (
            PrinterProfile.objects.filter(pk__in=printer_ids)
            .annotate(last_id=Subquery(latest_finished))
            .exclude(last_id=None)
            .values_list("last_id", flat=True)
        )
        last = {row.printer_id: row for row in ScheduledQueueStart.objects.filter(pk__in=list(last_ids))}
        for entry in entries:
            entry.printer.scheduled_start = pending.get(entry.printer_id)
            entry.printer.last_scheduled_result = last.get(entry.printer_id)


class PrintQueueCreateView(QueueManageMixin, CreateView):
    """Add a plate to the print queue.

    The user selects a plate from a sliced job and assigns it to a printer.
    """

    model = PrintQueue
    form_class = PrintQueueForm
    template_name = "core/printqueue_form.html"
    success_url = reverse_lazy("core:printqueue_list")

    def _queueable(self) -> list[PrintJobPlate]:
        """Return the queueable plates, computed once per request."""
        if not hasattr(self, "_queueable_cache"):
            self._queueable_cache = list(_queueable_plates().select_related("print_job__machine_profile"))
        return self._queueable_cache

    def get_form(self, form_class=None):
        form = super().get_form(form_class)
        queueable_pks = [plate.pk for plate in self._queueable()]
        form.fields["plate"].queryset = PrintJobPlate.objects.filter(pk__in=queueable_pks).select_related("print_job")

        # Pre-select plate from query parameter
        plate_pk = self.request.GET.get("plate")
        if plate_pk and plate_pk.isdigit() and int(plate_pk) in queueable_pks:
            form.fields["plate"].initial = int(plate_pk)

        # Deactivated printers get no new attempts
        form.fields["printer"].queryset = PrinterProfile.objects.active()
        return form

    def get_context_data(self, **kwargs):
        """Add plate->compatible-printers mapping for client-side filtering."""
        context = super().get_context_data(**kwargs)

        # Build a map: plate_pk -> [compatible printer PKs]
        plates = self._queueable()

        printers = PrinterProfile.objects.active()

        plate_printer_map: dict[str, list[int]] = {}
        for plate in plates:
            job = plate.print_job
            if job.machine_profile:
                compatible = [p.pk for p in printers if p.orca_machine_profile_id == job.machine_profile_id]
            else:
                compatible = [p.pk for p in printers]
            plate_printer_map[str(plate.pk)] = compatible

        context["job_printer_map_json"] = plate_printer_map
        return context

    def form_valid(self, form):
        """Create the queue entry for the plate."""
        printer = form.cleaned_data["printer"]
        plate = form.cleaned_data["plate"]
        priority = form.cleaned_data["priority"]

        try:
            PrintQueue.enqueue(plate=plate, printer=printer, priority=priority)
        except IntegrityError:
            # Queued concurrently since the form was validated (one open attempt per plate).
            messages.error(self.request, f"Plate {plate.plate_number} of '{plate.print_job}' is already queued.")
            return redirect(self.success_url)
        except ValueError:
            # The job was closed since the form was validated.
            messages.error(self.request, f"Print job '{plate.print_job}' is closed; its plates cannot be queued.")
            return redirect(self.success_url)
        # A plate of the job is queued again: an old "not re-queued" hint is obsolete.
        PrintJob.objects.filter(pk=plate.print_job_id).exclude(requeue_error="").update(requeue_error="")

        messages.success(
            self.request,
            f"Plate {plate.plate_number} of '{plate.print_job}' added to queue on {printer.name}.",
        )
        if "_save_and_add_another" in self.request.POST:
            return redirect(reverse("core:printqueue_create"))
        return redirect(self.success_url)


class PrintJobEnqueueAllView(QueueManageMixin, FormView):
    """Add every remaining plate of one print job to the queue of a printer.

    Plates that are waiting, printing, awaiting review or completed are skipped
    (see :meth:`PrintJob.enqueue_plates`); the rest are appended in plate order.
    Only active printers with the job's machine profile are offered.  Single
    plates are still queued via :class:`PrintQueueCreateView`.
    """

    form_class = EnqueueAllPlatesForm
    template_name = "core/printjob_enqueue_all.html"

    @cached_property
    def job(self) -> PrintJob:
        """The print job (404 if missing); loaded lazily, i.e. after the permission check."""
        return get_object_or_404(PrintJob.objects.select_related("machine_profile"), pk=self.kwargs["pk"])

    @cached_property
    def plates(self) -> list[PrintJobPlate]:
        """The job's queueable plates in plate order (computed once per request)."""
        return list(self.job.plates.queueable().order_by("plate_number", "pk"))

    def _detail_url(self) -> str:
        return reverse("core:printjob_detail", kwargs={"pk": self.job.pk})

    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        """Show the form, or go back to the job if no plate is left to queue."""
        if not self.plates:
            messages.info(request, f"No plates of '{self.job}' are left to queue.")
            return redirect(self._detail_url())
        if not self.job.compatible_printers().exists():
            messages.error(request, f"No active printer uses the machine profile of '{self.job}'.")
            return redirect(self._detail_url())
        return super().get(request, *args, **kwargs)

    def get_form_kwargs(self) -> dict[str, Any]:
        """Pass the job to the form (it limits the printer choices)."""
        kwargs = super().get_form_kwargs()
        kwargs["job"] = self.job
        return kwargs

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        """Add the job and the plates that will be queued."""
        context = super().get_context_data(**kwargs)
        context["job"] = self.job
        context["plates"] = self.plates
        return context

    def form_valid(self, form: EnqueueAllPlatesForm) -> HttpResponse:
        """Queue the remaining plates and go back to the job."""
        printer = form.cleaned_data["printer"]
        try:
            entries = self.job.enqueue_plates(printer=printer, priority=form.cleaned_data["priority"])
        except ValueError:
            # Deactivated or re-profiled since the form was validated.
            messages.error(self.request, f"Printer '{printer.name}' cannot print this job.")
            return redirect(self._detail_url())
        if entries:
            numbers = ", ".join(str(entry.plate.plate_number) for entry in entries)
            messages.success(
                self.request,
                f"Added {len(entries)} plate(s) of '{self.job}' to the queue on {printer.name}: plate {numbers}.",
            )
        else:
            messages.info(self.request, f"No plates of '{self.job}' were left to queue.")
        return redirect(self._detail_url())


class PrintQueueDeleteView(QueueDequeueMixin, DeleteView):
    """Remove an open print attempt from the queue.

    A never-started attempt is deleted; a started one is kept as history
    with ``result=cancelled`` (see :meth:`PrintQueue.remove`).  Resolved
    attempts are history and cannot be removed (404).

    ``can_dequeue_job`` is held by both Operators and Designers, but only
    users who can *control the printer* (Operators/Admins) may remove an
    entry that is already printing or awaiting review — removing such an
    entry would desync the database from the real printer.  Designers (who
    lack ``can_control_printer``) are therefore restricted to waiting
    entries; any other entry is simply not found (404) for them.
    """

    model = PrintQueue
    template_name = "core/printqueue_confirm_delete.html"
    success_url = reverse_lazy("core:printqueue_list")

    def get_queryset(self):
        queryset = PrintQueue.objects.open()
        if not self.request.user.has_perm("core.can_control_printer"):
            queryset = queryset.waiting()
        return queryset

    def form_valid(self, form):
        if not self.request.user.has_perm("core.can_control_printer"):
            # Close the TOCTOU window: the queryset check above ran at SELECT
            # time, but a printer worker could flip the row to printing /
            # awaiting_review before we delete it.  Delete conditionally on the
            # status so a live entry can never be removed by a Designer.
            # A waiting entry whose print start is in progress (G-code upload)
            # is about to become printing, so it is protected the same way.
            with transaction.atomic():
                deleted, _ = (
                    PrintQueue.objects.waiting()
                    .filter(pk=self.object.pk)
                    .exclude(PrintQueue.active_start_claim_q())
                    .delete()
                )
                if not deleted:
                    raise Http404("Queue entry is no longer waiting.")
            messages.success(self.request, "Removed from queue.")
            return HttpResponseRedirect(self.get_success_url())

        with transaction.atomic():
            # Re-read under lock: the attempt may have been resolved since the page loaded.
            # A print start in progress (G-code upload) is protected like for Designers:
            # deleting it would let the print run untracked.
            entry = (
                PrintQueue.objects.open()
                .select_for_update()
                .filter(pk=self.object.pk)
                .exclude(PrintQueue.active_start_claim_q())
                .first()
            )
            if entry is None:
                raise Http404("Queue entry is no longer open or its print is being started.")
            entry.remove()
        messages.success(self.request, "Removed from queue.")
        return HttpResponseRedirect(self.get_success_url())


class PrintQueueMoveView(QueueManageMixin, View):
    """Reorder a waiting queue entry within its printer's queue (POST only).

    Accepts either ``direction`` (``up``/``down``/``top``/``bottom``) or a
    1-based target ``position`` among the printer's waiting entries.  Entries
    that are printing or awaiting review are fixed: the move is refused and
    the user is redirected back with a warning.  Malformed input yields 400.
    See :func:`core.services.queue.move_queue_entry` for the priority rules.
    """

    http_method_names = ["post"]

    def post(self, request: HttpRequest, pk: int) -> HttpResponse:
        """Validate the move parameters, move the entry and redirect to the list.

        Args:
            request: The POST request carrying ``direction`` or ``position``.
            pk: Primary key of the queue entry to move.

        Returns:
            A redirect to the queue list (with a warning if the entry is no
            longer waiting), or 400 for a malformed direction/position.

        Raises:
            Http404: If the queue entry does not exist.
        """
        direction = request.POST.get("direction") or None
        target_index = None
        if direction is None:
            raw_position = request.POST.get("position", "")
            try:
                position = int(raw_position)
            except ValueError:
                return HttpResponseBadRequest("A direction or a valid position is required.")
            if position < 1:
                return HttpResponseBadRequest("Position must be 1 or greater.")
            target_index = position - 1
        elif direction not in MOVE_DIRECTIONS:
            return HttpResponseBadRequest("Invalid direction.")

        try:
            result = move_queue_entry(pk, direction=direction, target_index=target_index)
        except PrintQueue.DoesNotExist as exc:
            raise Http404("Queue entry not found.") from exc
        except QueueReorderError as exc:
            # Parameters were valid, so this is the entry having left the
            # ``waiting`` state since the page was rendered (stale UI race).
            messages.warning(request, str(exc))
            return redirect("core:printqueue_list")

        entry = result.entry
        label = f"Plate {entry.plate.plate_number} of '{entry.print_job}'" if entry.plate else "Queue entry"
        messages.success(request, f"Moved {label} on {entry.printer.name}.")
        if result.priority_changed:
            old_label = dict(PrintQueue.PRIORITY_CHOICES).get(result.previous_priority, result.previous_priority)
            messages.info(
                request,
                f"Priority changed from {old_label} to {entry.get_priority_display()} to match its new position.",
            )
        return redirect("core:printqueue_list")


class RunNextQueueView(PrinterControlMixin, View):
    """Run the next waiting print on a specific printer.

    Checks that the printer is free (no entry currently printing or awaiting
    review), then uploads the G-code via Moonraker and starts the print.
    """

    def post(self, request: HttpRequest, printer_pk: int) -> HttpResponse:
        printer = get_object_or_404(PrinterProfile, pk=printer_pk)
        result = start_next_for_printer(printer)

        if result.outcome == "not_configured":
            messages.error(request, "No Moonraker URL configured for this printer.")
        elif result.outcome == "busy":
            busy_entry = result.busy_entry
            status_label = queue_entry_status_label(busy_entry)
            messages.warning(
                request,
                f'Printer "{printer.name}" is busy ({status_label}: '
                f'plate {busy_entry.plate.plate_number} of "{busy_entry.print_job}").',
            )
        elif result.outcome == "empty":
            messages.warning(request, "No waiting jobs in the queue for this printer.")
        elif result.outcome == "started":
            plate = result.entry.plate
            messages.success(
                request,
                f'Started printing plate {plate.plate_number} of "{plate.print_job}" on {printer.name}.',
            )
        else:
            messages.error(request, result.error)

        return redirect("core:printqueue_list")


class RunAllQueuesView(PrinterControlMixin, View):
    """Start the next waiting job on every free printer.

    Iterates over all active printers that have a Moonraker URL.
    For each printer that is free (no printing/awaiting_review entry), it
    picks the highest-priority waiting entry and starts the print.
    """

    def post(self, request: HttpRequest) -> HttpResponse:
        printers = PrinterProfile.objects.active().exclude(moonraker_url="")

        started = 0
        skipped_busy = 0
        skipped_empty = 0
        errors = 0

        for printer in printers:
            # Same rules (and entry claim) as Run Next and the printer's Start button.
            result = start_next_for_printer(printer)
            if result.outcome == "started":
                started += 1
            elif result.outcome == "busy":
                skipped_busy += 1
            elif result.outcome == "no_gcode":
                logger.warning("Queue entry %s has no G-code file, skipping.", result.entry.pk)
                skipped_empty += 1
            elif result.outcome == "empty":
                skipped_empty += 1
            else:
                logger.error("Failed to start print on %s: %s", printer.name, result.error)
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


def _exclusion_choices(entry: PrintQueue) -> list[dict[str, Any]]:
    """Return the plate's part copies with their exclusion in ``entry`` (for the mark-as-not-ok checkboxes).

    Args:
        entry: The attempt (``plate__print_job`` loaded).

    Returns:
        Dicts with ``pk`` (plate part), ``label`` (``"<part> #<copy>"``, partial
        copies marked), ``excluded`` and ``source`` (display text of the exclusion
        source, ``""`` if not excluded), ordered by part label and copy.
    """
    plate_parts = list(entry.plate.plate_parts.select_related("part")) if entry.plate else []
    exclusions = {row.plate_part_id: row for row in entry.exclusions.all()}
    labels = entry.plate.print_job.part_labels() if entry.plate else {}
    choices = []
    for plate_part in plate_parts:
        exclusion = exclusions.get(plate_part.pk)
        partial = " (partial)" if plate_part.meshes_on_plate < plate_part.mesh_count else ""
        name = labels.get(plate_part.part_id, plate_part.part.name)
        choices.append(
            {
                "pk": plate_part.pk,
                "label": f"{name} #{plate_part.copy_index + 1}{partial}",
                "excluded": exclusion is not None,
                "source": exclusion.get_source_display() if exclusion is not None else "",
            }
        )
    choices.sort(key=lambda choice: choice["label"])
    return choices


def _posted_exclusions(request: HttpRequest, entry: PrintQueue) -> list[int] | None:
    """Return the plate part IDs ticked as not ok, or ``None`` if invalid (not of the entry's plate).

    Args:
        request: The POST request (checkbox field ``excluded``).
        entry: The attempt being edited.

    Returns:
        The selected plate part primary keys, or ``None`` on invalid input.
    """
    try:
        wanted = {int(value) for value in request.POST.getlist("excluded")}
    except ValueError:
        return None
    valid = set(entry.plate.plate_parts.values_list("pk", flat=True)) if entry.plate else set()
    return sorted(wanted) if wanted <= valid else None


class QueueEntryReviewView(PrinterControlMixin, View):
    """Confirm a finished print as Pass (success) or Fail (retry/discard).

    GET  -- renders a review page showing the queue entry details.
    POST -- processes the ``action`` parameter (``pass`` or ``fail``).

    **Pass:** Marks the plate as completed.  If all plates of the job are
    done, the job status is set to *completed*.

    **Fail:** Keeps the failed attempt as history and queues a retry
    attempt, until the plate reaches ``max_retries`` failed attempts — then
    the plate counts as failed and a warning is shown.
    """

    def _get_entry(self, request: HttpRequest, pk: int) -> PrintQueue:
        """Fetch the queue entry or 404."""
        return get_object_or_404(
            PrintQueue.objects.awaiting_review().with_retry_count().select_related("plate__print_job", "printer"),
            pk=pk,
        )

    def get(self, request: HttpRequest, pk: int) -> HttpResponse:
        """Show the review confirmation page (with the plate's copies to mark as not ok)."""
        from django.template.response import TemplateResponse

        entry = self._get_entry(request, pk)
        return TemplateResponse(
            request,
            "core/printqueue_review.html",
            {"entry": entry, "exclusion_choices": _exclusion_choices(entry)},
        )

    def post(self, request: HttpRequest, pk: int) -> HttpResponse:
        """Process pass or fail action.

        A Pass from the review page also stores the copies ticked as not ok
        (only when the form carries ``exclusions_submitted``, so a bare Pass
        keeps the exclusions reported by the printer).
        """
        entry = self._get_entry(request, pk)
        action = request.POST.get("action", "")
        if action not in ("pass", "fail"):
            messages.error(request, "Invalid action.")
            return redirect("core:printqueue_list")

        excluded = None
        if action == "pass" and request.POST.get("exclusions_submitted"):
            excluded = _posted_exclusions(request, entry)
            if excluded is None:
                messages.error(request, "Invalid selection of parts.")
                return redirect("core:printqueue_review", pk=entry.pk)

        try:
            # One transaction: the verdict and the copies marked as not ok land together (or not at all).
            with transaction.atomic():
                result = review_queue_entry(entry.pk, action == "pass")
                if excluded is not None and result.outcome == "passed":
                    entry.refresh_from_db()
                    entry.set_excluded_plate_parts(excluded, user=request.user)
        except ReviewStateError:
            # Reviewed concurrently (e.g. on the printer) since the page loaded.
            messages.warning(request, "This queue entry has already been reviewed.")
            return redirect("core:printqueue_list")
        except ValueError:
            # The plate's parts changed since the page loaded; nothing was saved.
            messages.error(request, "Invalid selection of parts — the print was not reviewed.")
            return redirect("core:printqueue_review", pk=entry.pk)

        if result.outcome == "passed":
            messages.success(
                request,
                f"{result.plate_label} passed! ({result.plates_completed}/{result.plate_count} plates completed)",
            )
        elif result.outcome == "discarded":
            messages.error(
                request,
                f"{result.plate_label} failed and max retries ({result.max_retries}) reached — plate marked failed.",
            )
        else:
            messages.warning(
                request,
                f"{result.plate_label} failed — requeued (retry {result.retry_count}/{result.max_retries}).",
            )

        return redirect("core:printqueue_list")


class PrintAttemptExclusionsView(PrinterControlMixin, View):
    """Mark part copies of a print attempt as not ok (also after the review).

    Corrects the exclusions of an attempt awaiting review or passed — e.g. a
    part that broke off after the review.  Excluded copies of a passed attempt
    do not count as printed.

    GET  -- renders the plate's copies as checkboxes.
    POST -- stores the ticked copies (``excluded``) as the attempt's exclusions.
    """

    def _get_entry(self, pk: int) -> PrintQueue:
        """Fetch an attempt whose exclusions may be edited, or 404."""
        entry = get_object_or_404(PrintQueue.objects.select_related("plate__print_job", "printer"), pk=pk)
        if entry.plate is None or not entry.can_edit_exclusions:
            raise Http404("This attempt is neither awaiting review nor passed.")
        return entry

    def get(self, request: HttpRequest, pk: int) -> HttpResponse:
        """Show the copies of the attempt's plate."""
        from django.template.response import TemplateResponse

        entry = self._get_entry(pk)
        return TemplateResponse(
            request,
            "core/printqueue_exclusions.html",
            {"entry": entry, "exclusion_choices": _exclusion_choices(entry)},
        )

    def post(self, request: HttpRequest, pk: int) -> HttpResponse:
        """Store the copies ticked as not ok."""
        entry = self._get_entry(pk)
        excluded = _posted_exclusions(request, entry)
        if excluded is None:
            messages.error(request, "Invalid selection of parts.")
            return redirect("core:printqueue_exclusions", pk=entry.pk)
        with transaction.atomic():
            # Re-check under the row lock: the attempt may have been reviewed as failed meanwhile.
            locked = PrintQueue.objects.select_for_update().select_related("plate__print_job").get(pk=entry.pk)
            if not locked.can_edit_exclusions:
                messages.warning(request, "This attempt can no longer be corrected.")
                return redirect("core:printjob_detail", pk=entry.plate.print_job_id)
            locked.set_excluded_plate_parts(excluded, user=request.user)
        count = len(excluded)
        messages.success(
            request,
            f"Plate {entry.plate.plate_number}: {count} part(s) marked as not ok."
            if count
            else f"Plate {entry.plate.plate_number}: all parts marked as ok.",
        )
        return redirect("core:printjob_detail", pk=entry.plate.print_job_id)


#: If ``status_updated_at`` is older than this, the worker is
#: considered stale and the client is told so via ``worker_stale``.
WORKER_STALE_THRESHOLD_SECONDS = 60


class QueueCheckPrinterStatusView(LoginRequiredMixin, View):
    """JSON endpoint: return the current status of a printing queue entry.

    Data is served entirely from the database.  The ``moonraker_worker``
    management command is responsible for pushing live progress and
    terminal-state transitions into the DB via its WebSocket
    connection to Moonraker, so this endpoint no longer talks to the
    printer directly.
    """

    def get(self, request: HttpRequest, pk: int) -> JsonResponse:
        entry = get_object_or_404(
            PrintQueue.objects.select_related("plate__print_job", "printer"),
            pk=pk,
        )

        if not entry.is_printing:
            payload: dict = {"status": entry.state}
            if entry.last_error:
                payload["message"] = entry.last_error
            return JsonResponse(payload)

        payload = {
            "status": "printing",
            "progress": entry.progress,
        }
        if entry.status_updated_at is not None:
            payload["status_updated_at"] = entry.status_updated_at.isoformat()
            age = (timezone.now() - entry.status_updated_at).total_seconds()
            if age > WORKER_STALE_THRESHOLD_SECONDS:
                payload["worker_stale"] = True
        else:
            # No update yet -> worker either hasn't connected or just started.
            payload["worker_stale"] = True
        return JsonResponse(payload)


class CancelQueueEntryView(PrinterControlMixin, View):
    """Cancel a running print and move the queue entry to awaiting_review.

    Sends a cancel command to Moonraker and sets the entry status so
    the operator can then Pass (discard the failed print) or Fail
    (retry).
    """

    def post(self, request: HttpRequest, pk: int) -> HttpResponse:
        entry = get_object_or_404(
            PrintQueue.objects.printing().select_related("plate__print_job", "printer"),
            pk=pk,
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
                "Cancel command failed, but entry marked for review. Check server logs for details.",
            )

        entry.mark_finished()

        messages.info(
            request,
            f"{plate_label} on {printer.name} cancelled — please review.",
        )
        return redirect("core:printqueue_list")


class ScheduleQueueStartView(PrinterControlMixin, View):
    """Schedule (or move) the one-shot start of the next queue job on a printer (POST only)."""

    http_method_names = ["post"]

    def post(self, request: HttpRequest, printer_pk: int) -> HttpResponse:
        """Validate the start time and create or update the pending schedule.

        Args:
            request: The POST request carrying ``start_at``.
            printer_pk: Primary key of the printer.

        Returns:
            A redirect to the queue list.
        """
        printer = get_object_or_404(PrinterProfile, pk=printer_pk)
        form = ScheduledStartForm(request.POST)
        if form.is_valid():
            row = schedule_start(printer, form.cleaned_data["start_at"], request.user)
            messages.success(
                request,
                f"Next job on {printer.name} will start at {timezone.localtime(row.start_at):%Y-%m-%d %H:%M}.",
            )
        else:
            for error in form.errors.get("start_at", ["Invalid start time."]):
                messages.error(request, error)
        return redirect("core:printqueue_list")


class CancelScheduledStartView(PrinterControlMixin, View):
    """Cancel the pending scheduled start of a printer (POST only)."""

    http_method_names = ["post"]

    def post(self, request: HttpRequest, printer_pk: int) -> HttpResponse:
        """Cancel the pending schedule, if any.

        Args:
            request: The POST request.
            printer_pk: Primary key of the printer.

        Returns:
            A redirect to the queue list.
        """
        printer = get_object_or_404(PrinterProfile, pk=printer_pk)
        if cancel_scheduled_start(printer):
            messages.success(request, f"Scheduled start on {printer.name} cancelled.")
        else:
            messages.info(request, f"No scheduled start pending on {printer.name}.")
        return redirect("core:printqueue_list")
