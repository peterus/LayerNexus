"""Material and filament mapping views for the LayerNexus application."""

import logging

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect
from django.views import View
from django.views.generic import TemplateView

from core.mixins import FilamentMappingManageMixin
from core.models import (
    OrcaFilamentProfile,
    PrinterProfile,
    SpoolmanFilamentMapping,
)
from core.services.spoolman import SpoolmanClient, SpoolmanError

logger = logging.getLogger(__name__)

__all__ = [
    "SpoolmanSpoolsView",
    "SpoolmanFilamentsAPIView",
    "MaterialProfileListView",
    "SaveFilamentMappingView",
]


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
