"""OrcaSlicer API integration service.

Communicates with the orca-slicer-api Docker container
(https://github.com/AFKFelix/orca-slicer-api) via REST.
"""

import io
import json
import logging
import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import requests

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 300  # seconds — slicing can be slow


@dataclass
class SliceResult:
    """Result returned after slicing a model."""

    gcode_content: bytes
    filament_used_grams: float | None = None
    filament_used_mm: float | None = None
    print_time_seconds: float | None = None


@dataclass
class PlateResult:
    """Result for a single plate in a multi-plate slice."""

    plate_number: int
    gcode_content: bytes
    filament_used_grams: float | None = None
    filament_used_mm: float | None = None
    print_time_seconds: float | None = None
    thumbnail_png: bytes | None = None


@dataclass
class MultiPlateSliceResult:
    """Result containing one or more plates from a slice operation."""

    plates: list[PlateResult] = field(default_factory=list)

    # Aggregated values from HTTP response headers (orca-slicer-api sums
    # the per-plate values and returns them in X-* headers).
    header_filament_grams: float | None = None
    header_filament_mm: float | None = None
    header_print_time_seconds: float | None = None

    @property
    def total_filament_grams(self) -> float | None:
        """Sum of filament usage across all plates.

        Falls back to the aggregated HTTP header value when per-plate
        values are all zero or missing.
        """
        values = [p.filament_used_grams for p in self.plates if p.filament_used_grams is not None]
        total = sum(values) if values else None
        if total is not None and total > 0:
            return total
        return self.header_filament_grams

    @property
    def total_filament_mm(self) -> float | None:
        """Sum of filament length across all plates (mm).

        Falls back to the aggregated HTTP header value when per-plate
        values are all zero or missing.
        """
        values = [p.filament_used_mm for p in self.plates if p.filament_used_mm is not None]
        total = sum(values) if values else None
        if total is not None and total > 0:
            return total
        return self.header_filament_mm

    @property
    def total_print_time_seconds(self) -> float | None:
        """Sum of print time across all plates.

        Falls back to the aggregated HTTP header value when per-plate
        values are all zero or missing.
        """
        values = [p.print_time_seconds for p in self.plates if p.print_time_seconds is not None]
        total = sum(values) if values else None
        if total is not None and total > 0:
            return total
        return self.header_print_time_seconds


class OrcaSlicerError(Exception):
    """Raised when an OrcaSlicer API operation fails."""


class OrcaSlicerAPIClient:
    """Client for the orca-slicer-api REST service."""

    def __init__(self, base_url: str = "http://orcaslicer:3000"):
        """Initialize the OrcaSlicer API client.

        Args:
            base_url: Base URL of the orca-slicer-api instance.
        """
        self.base_url = base_url.rstrip("/")

    def _url(self, path: str) -> str:
        """Construct full URL from path.

        Args:
            path: API endpoint path.

        Returns:
            Full URL to the endpoint.
        """
        return f"{self.base_url}/{path.lstrip('/')}"

    # ------------------------------------------------------------------
    # G-code metadata parsing
    # ------------------------------------------------------------------

    # Patterns for OrcaSlicer / BambuStudio G-code metadata comments
    _RE_FILAMENT_GRAMS = re.compile(r";\s*(?:total\s+)?filament\s+used\s*\[g\]\s*=\s*([\d.]+)", re.IGNORECASE)
    _RE_FILAMENT_MM = re.compile(r";\s*(?:total\s+)?filament\s+used\s*\[mm?\]\s*=\s*([\d.]+)", re.IGNORECASE)
    _RE_PRINT_TIME = re.compile(
        r";\s*(?:total\s+estimated\s+time|estimated\s+printing\s+time\s*(?:\(normal\s+mode\))?)\s*[:=]\s*(.+)",
        re.IGNORECASE,
    )

    @staticmethod
    def _parse_time_string(time_str: str) -> float | None:
        """Parse an OrcaSlicer time string like '1h 23m 45s' to seconds.

        Args:
            time_str: Human-readable time string (e.g. '1h 23m 45s',
                '5m 30s', '45s', '1d 2h 30m').

        Returns:
            Total seconds, or None if unparseable.
        """
        total = 0.0
        parts = re.findall(r"([\d.]+)\s*([dhms])", time_str, re.IGNORECASE)
        if not parts:
            return None
        for value, unit in parts:
            try:
                v = float(value)
            except ValueError:
                continue
            if unit.lower() == "d":
                total += v * 86400
            elif unit.lower() == "h":
                total += v * 3600
            elif unit.lower() == "m":
                total += v * 60
            elif unit.lower() == "s":
                total += v
        return total if total > 0 else None

    @classmethod
    def _parse_gcode_metadata(cls, gcode_bytes: bytes) -> dict[str, float | None]:
        """Extract filament usage and print time from G-code comment lines.

        Reads only the last ~8 KB of the G-code (metadata is at the end)
        to avoid scanning multi-megabyte files.

        Args:
            gcode_bytes: Raw G-code content.

        Returns:
            Dictionary with optional keys ``filament_used_grams``,
            ``filament_used_mm``, and ``print_time_seconds``.
        """
        # Metadata comments are typically in the last few KB, but
        # OrcaSlicer settings dumps can be 10-20 KB, so scan 32 KB.
        tail = gcode_bytes[-32768:].decode("utf-8", errors="replace")
        result: dict[str, float | None] = {}

        m = cls._RE_FILAMENT_GRAMS.search(tail)
        if m:
            try:
                result["filament_used_grams"] = float(m.group(1))
            except ValueError:
                pass

        m = cls._RE_FILAMENT_MM.search(tail)
        if m:
            try:
                result["filament_used_mm"] = float(m.group(1))
            except ValueError:
                pass

        m = cls._RE_PRINT_TIME.search(tail)
        if m:
            result["print_time_seconds"] = cls._parse_time_string(m.group(1))

        return result

    # ------------------------------------------------------------------
    # Profile management
    # ------------------------------------------------------------------

    def list_profiles(self, category: str) -> list[str]:
        """List uploaded profiles for a category.

        Args:
            category: One of 'printers', 'presets', 'filaments'.

        Returns:
            List of profile names.
        """
        try:
            resp = requests.get(
                self._url(f"/profiles/{category}"),
                timeout=DEFAULT_TIMEOUT,
            )
            resp.raise_for_status()
            return resp.json()
        except requests.RequestException as exc:
            raise OrcaSlicerError(f"Failed to list {category} profiles: {exc}") from exc

    def get_profile(self, category: str, name: str) -> dict:
        """Retrieve a specific profile.

        Args:
            category: One of 'printers', 'presets', 'filaments'.
            name: Profile name.

        Returns:
            Profile data as a dictionary.
        """
        try:
            resp = requests.get(
                self._url(f"/profiles/{category}/{name}"),
                timeout=DEFAULT_TIMEOUT,
            )
            resp.raise_for_status()
            return resp.json()
        except requests.RequestException as exc:
            raise OrcaSlicerError(f"Failed to get profile {category}/{name}: {exc}") from exc

    def upload_profile(self, category: str, name: str, json_content: bytes | str) -> dict:
        """Upload a profile JSON file.

        Args:
            category: One of 'printers', 'presets', 'filaments'.
            name: Name for the profile (alphanumeric).
            json_content: The JSON profile file content.

        Returns:
            API response dict.
        """
        if isinstance(json_content, str):
            json_content = json_content.encode("utf-8")

        try:
            resp = requests.post(
                self._url(f"/profiles/{category}"),
                files={"file": (f"{name}.json", json_content, "application/json")},
                data={"name": name},
                timeout=DEFAULT_TIMEOUT,
            )
            resp.raise_for_status()
            return resp.json()
        except requests.RequestException as exc:
            raise OrcaSlicerError(f"Failed to upload {category} profile: {exc}") from exc

    # ------------------------------------------------------------------
    # Slicing
    # ------------------------------------------------------------------

    def slice(
        self,
        model_path: str | Path | None = None,
        *,
        model_content: bytes | None = None,
        model_filename: str = "model.3mf",
        printer_profile_name: str | None = None,
        preset_profile_name: str | None = None,
        filament_profile_name: str | None = None,
        printer_profile_json: bytes | None = None,
        preset_profile_json: bytes | None = None,
        filament_profile_json: bytes | None = None,
        bed_type: str | None = None,
        plate: int | None = None,
        arrange: bool = False,
        orient: bool = False,
        export_type: str = "gcode",
    ) -> SliceResult:
        """Slice a 3D model via the API.

        Either ``model_path`` (file on disk) or ``model_content`` (in-memory
        bytes) must be provided.  Profile JSON files take precedence over
        profile names.

        Args:
            model_path: Path to the STL/STEP/3MF file.
            model_content: Raw bytes of the model file (alternative to path).
            model_filename: Filename to use when sending model_content.
            printer_profile_name: Name of an uploaded printer profile.
            preset_profile_name: Name of an uploaded preset profile.
            filament_profile_name: Name of an uploaded filament profile.
            printer_profile_json: Raw JSON bytes for an inline printer profile.
            preset_profile_json: Raw JSON bytes for an inline preset profile.
            filament_profile_json: Raw JSON bytes for an inline filament profile.
            bed_type: Bed type string (e.g. 'Textured PEI Plate').
            plate: Plate number (0 = all plates).
            arrange: Whether to auto-arrange models.
            orient: Whether to auto-orient models.
            export_type: 'gcode' or '3mf'.

        Returns:
            SliceResult with G-code content and usage metadata.

        Raises:
            OrcaSlicerError: If slicing fails.
            FileNotFoundError: If the model file doesn't exist.
        """
        if model_content is not None:
            # In-memory model (e.g. 3MF bundle)
            content_types = {
                ".stl": "model/stl",
                ".step": "model/step",
                ".stp": "model/step",
                ".3mf": "model/3mf",
            }
            suffix = Path(model_filename).suffix.lower()
            ct = content_types.get(suffix, "application/octet-stream")
            file_tuple = (model_filename, model_content, ct)
            display_name = model_filename
        elif model_path is not None:
            model_path = Path(model_path)
            if not model_path.exists():
                raise FileNotFoundError(f"Model file not found: {model_path}")
            model_bytes = model_path.read_bytes()
            suffix = model_path.suffix.lower()
            content_types = {
                ".stl": "model/stl",
                ".step": "model/step",
                ".stp": "model/step",
                ".3mf": "model/3mf",
            }
            ct = content_types.get(suffix, "application/octet-stream")
            file_tuple = (model_path.name, model_bytes, ct)
            display_name = model_path.name
        else:
            raise OrcaSlicerError("Either model_path or model_content must be provided")

        files = {}
        data = {}

        files["file"] = file_tuple

        # Inline profile JSONs (higher priority)
        # Validate JSON profiles to ensure they are properly formatted
        if printer_profile_json:
            try:
                json.loads(
                    printer_profile_json
                    if isinstance(printer_profile_json, str)
                    else printer_profile_json.decode("utf-8")
                )
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                raise OrcaSlicerError(f"Invalid printer profile JSON: {exc}") from exc
            files["printerProfile"] = (
                "printer.json",
                printer_profile_json,
                "application/json",
            )
        elif printer_profile_name:
            data["printer"] = printer_profile_name

        if preset_profile_json:
            try:
                json.loads(
                    preset_profile_json if isinstance(preset_profile_json, str) else preset_profile_json.decode("utf-8")
                )
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                raise OrcaSlicerError(f"Invalid preset profile JSON: {exc}") from exc
            files["presetProfile"] = (
                "preset.json",
                preset_profile_json,
                "application/json",
            )
        elif preset_profile_name:
            data["preset"] = preset_profile_name

        if filament_profile_json:
            files["filamentProfile"] = (
                "filament.json",
                filament_profile_json,
                "application/json",
            )
        elif filament_profile_name:
            data["filament"] = filament_profile_name

        if bed_type:
            data["bedType"] = bed_type
        if plate is not None:
            data["plate"] = str(plate)
        if arrange:
            data["arrange"] = "true"
        if orient:
            data["orient"] = "true"
        if export_type:
            data["exportType"] = export_type

        logger.info(
            "Slicing %s with profiles printer=%s preset=%s filament=%s",
            display_name,
            printer_profile_name or "(inline)",
            preset_profile_name or "(inline)",
            filament_profile_name or "(inline)",
        )

        try:
            resp = requests.post(
                self._url("/slice"),
                files=files,
                data=data,
                timeout=DEFAULT_TIMEOUT,
            )
            resp.raise_for_status()
        except requests.ConnectionError as exc:
            raise OrcaSlicerError(f"Cannot connect to OrcaSlicer API at {self.base_url}") from exc
        except requests.Timeout as exc:
            raise OrcaSlicerError("OrcaSlicer API request timed out.") from exc
        except requests.HTTPError as exc:
            body = ""
            if exc.response is not None:
                body = exc.response.text[:500]
            raise OrcaSlicerError(f"Slicing failed: {body}") from exc
        except requests.RequestException as exc:
            raise OrcaSlicerError(f"OrcaSlicer API request failed: {exc}") from exc

        # Parse usage metadata from response headers
        result = SliceResult(gcode_content=resp.content)

        grams = resp.headers.get("X-Filament-Used-g")
        if grams:
            try:
                result.filament_used_grams = float(grams)
            except ValueError:
                pass

        mm_val = resp.headers.get("X-Filament-Used-mm")
        if mm_val:
            try:
                result.filament_used_mm = float(mm_val)
            except ValueError:
                pass

        time_s = resp.headers.get("X-Print-Time-Seconds")
        if time_s:
            try:
                result.print_time_seconds = float(time_s)
            except ValueError:
                pass

        logger.info(
            "Slicing completed: %.1f g filament, %.0f s print time",
            result.filament_used_grams or 0,
            result.print_time_seconds or 0,
        )

        return result

    # ------------------------------------------------------------------
    # Multi-plate slicing
    # ------------------------------------------------------------------

    def slice_bundle(
        self,
        model_content: bytes,
        model_filename: str = "bundle.3mf",
        **slice_kwargs: Any,
    ) -> MultiPlateSliceResult:
        """Slice a 3MF bundle and handle multi-plate ZIP responses.

        Sends the model to the OrcaSlicer API with ``arrange=True``.  If
        the response is a ZIP archive (multiple plates), each G-code
        file is extracted as a separate ``PlateResult``.  Otherwise the
        single G-code is returned as a 1-plate result.

        Args:
            model_content: In-memory 3MF file bytes.
            model_filename: Filename for the upload.
            **slice_kwargs: Additional keyword arguments forwarded to
                :meth:`slice` (profile names/JSONs, bed_type, etc.).

        Returns:
            MultiPlateSliceResult with one PlateResult per build plate.
        """
        # Force arrange for multi-part bundles and slice all plates
        slice_kwargs.setdefault("arrange", True)
        slice_kwargs.setdefault("plate", 0)

        # Use the low-level slice method but intercept the response ourselves
        # to check for ZIP. We re-implement parts of the call here.
        if model_content is None:
            raise OrcaSlicerError("model_content is required for slice_bundle")

        # Build files/data like slice() does
        files: dict[str, Any] = {}
        data: dict[str, str] = {}

        suffix = Path(model_filename).suffix.lower()
        content_types = {
            ".stl": "model/stl",
            ".step": "model/step",
            ".stp": "model/step",
            ".3mf": "model/3mf",
        }
        ct = content_types.get(suffix, "application/octet-stream")
        files["file"] = (model_filename, model_content, ct)

        # Build profile payloads
        for key in (
            "printer_profile_json",
            "preset_profile_json",
            "filament_profile_json",
        ):
            value = slice_kwargs.pop(key, None)
            if value is not None:
                # Map to the API field names
                api_field_map = {
                    "printer_profile_json": "printerProfile",
                    "preset_profile_json": "presetProfile",
                    "filament_profile_json": "filamentProfile",
                }
                api_name = api_field_map[key]
                label = key.replace("_json", "").replace("_profile", "")
                if isinstance(value, bytes):
                    try:
                        json.loads(value)
                    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                        raise OrcaSlicerError(f"Invalid {label} profile JSON: {exc}") from exc
                files[api_name] = (f"{label}.json", value, "application/json")

        for key in (
            "printer_profile_name",
            "preset_profile_name",
            "filament_profile_name",
        ):
            value = slice_kwargs.pop(key, None)
            if value:
                # Map to API field: printer_profile_name -> printer
                data_key_map = {
                    "printer_profile_name": "printer",
                    "preset_profile_name": "preset",
                    "filament_profile_name": "filament",
                }
                data[data_key_map[key]] = value

        if slice_kwargs.get("bed_type"):
            data["bedType"] = slice_kwargs.pop("bed_type")
        if slice_kwargs.get("plate") is not None:
            data["plate"] = str(slice_kwargs.pop("plate"))
        if slice_kwargs.get("arrange"):
            data["arrange"] = "true"
        slice_kwargs.pop("arrange", None)
        if slice_kwargs.get("orient"):
            data["orient"] = "true"
        slice_kwargs.pop("orient", None)
        if slice_kwargs.get("export_type"):
            data["exportType"] = slice_kwargs.pop("export_type")

        logger.info("Slicing bundle %s (%d bytes)", model_filename, len(model_content))

        try:
            resp = requests.post(
                self._url("/slice"),
                files=files,
                data=data,
                timeout=DEFAULT_TIMEOUT,
            )
            resp.raise_for_status()
        except requests.ConnectionError as exc:
            raise OrcaSlicerError(f"Cannot connect to OrcaSlicer API at {self.base_url}") from exc
        except requests.Timeout as exc:
            raise OrcaSlicerError("OrcaSlicer API request timed out.") from exc
        except requests.HTTPError as exc:
            body = ""
            if exc.response is not None:
                body = exc.response.text[:500]
            raise OrcaSlicerError(f"Slicing failed: {body}") from exc
        except requests.RequestException as exc:
            raise OrcaSlicerError(f"OrcaSlicer API request failed: {exc}") from exc

        # Check if response is a ZIP (multi-plate)
        content_type = resp.headers.get("Content-Type", "")
        result = MultiPlateSliceResult()

        # Capture aggregated metadata from HTTP response headers.
        # The orca-slicer-api sums per-plate values into these headers
        # for both single-plate and multi-plate responses.
        hdr_grams = resp.headers.get("X-Filament-Used-g")
        if hdr_grams:
            try:
                result.header_filament_grams = float(hdr_grams)
            except ValueError:
                pass
        hdr_mm = resp.headers.get("X-Filament-Used-mm")
        if hdr_mm:
            try:
                result.header_filament_mm = float(hdr_mm)
            except ValueError:
                pass
        hdr_time = resp.headers.get("X-Print-Time-Seconds")
        if hdr_time:
            try:
                result.header_print_time_seconds = float(hdr_time)
            except ValueError:
                pass

        if "application/zip" in content_type or self._is_zip(resp.content):
            # Multi-plate: extract G-code files and thumbnails from ZIP
            try:
                with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
                    all_names = zf.namelist()
                    gcode_files = sorted([n for n in all_names if n.lower().endswith(".gcode")])
                    if not gcode_files:
                        raise OrcaSlicerError("ZIP response contains no G-code files")

                    # Collect PNG thumbnails from ZIP (plate_N.png pattern)
                    png_files = sorted([n for n in all_names if n.lower().endswith(".png")])

                    for idx, gcode_name in enumerate(gcode_files):
                        gcode_bytes = zf.read(gcode_name)
                        plate = PlateResult(
                            plate_number=idx + 1,
                            gcode_content=gcode_bytes,
                        )

                        # Try to find a matching PNG in the ZIP
                        plate_stem = Path(gcode_name).stem.lower()
                        for png_name in png_files:
                            png_stem = Path(png_name).stem.lower()
                            if png_stem == plate_stem or png_stem.startswith(plate_stem):
                                plate.thumbnail_png = zf.read(png_name)
                                logger.debug(
                                    "Extracted ZIP thumbnail %s for plate %d",
                                    png_name,
                                    idx + 1,
                                )
                                break

                        # Fallback: extract thumbnail from G-code comments
                        if not plate.thumbnail_png:
                            from .gcode_thumbnail import extract_largest_thumbnail

                            plate.thumbnail_png = extract_largest_thumbnail(gcode_bytes)

                        # Parse filament/time metadata from G-code comments
                        meta = self._parse_gcode_metadata(gcode_bytes)
                        if meta.get("filament_used_grams") is not None:
                            plate.filament_used_grams = meta["filament_used_grams"]
                        if meta.get("filament_used_mm") is not None:
                            plate.filament_used_mm = meta["filament_used_mm"]
                        if meta.get("print_time_seconds") is not None:
                            plate.print_time_seconds = meta["print_time_seconds"]

                        result.plates.append(plate)

                logger.info("Multi-plate slicing: %d plates extracted", len(result.plates))
            except zipfile.BadZipFile as exc:
                raise OrcaSlicerError(f"Invalid ZIP response: {exc}") from exc
        else:
            # Single plate — parse metadata from headers
            plate = PlateResult(
                plate_number=1,
                gcode_content=resp.content,
            )

            grams = resp.headers.get("X-Filament-Used-g")
            if grams:
                try:
                    plate.filament_used_grams = float(grams)
                except ValueError:
                    pass

            mm_val = resp.headers.get("X-Filament-Used-mm")
            if mm_val:
                try:
                    plate.filament_used_mm = float(mm_val)
                except ValueError:
                    pass

            time_s = resp.headers.get("X-Print-Time-Seconds")
            if time_s:
                try:
                    plate.print_time_seconds = float(time_s)
                except ValueError:
                    pass

            result.plates.append(plate)

            # Extract thumbnail from single-plate G-code
            from .gcode_thumbnail import extract_largest_thumbnail

            plate.thumbnail_png = extract_largest_thumbnail(resp.content)

            # Fallback: parse metadata from G-code if headers were missing
            if plate.filament_used_grams is None or plate.print_time_seconds is None:
                meta = self._parse_gcode_metadata(resp.content)
                if plate.filament_used_grams is None and meta.get("filament_used_grams") is not None:
                    plate.filament_used_grams = meta["filament_used_grams"]
                if plate.filament_used_mm is None and meta.get("filament_used_mm") is not None:
                    plate.filament_used_mm = meta["filament_used_mm"]
                if plate.print_time_seconds is None and meta.get("print_time_seconds") is not None:
                    plate.print_time_seconds = meta["print_time_seconds"]

            logger.info(
                "Single-plate slicing: %.1f g filament, %.0f s print time",
                plate.filament_used_grams or 0,
                plate.print_time_seconds or 0,
            )

        return result

    @staticmethod
    def _is_zip(data: bytes) -> bool:
        """Check if data starts with ZIP magic bytes.

        Args:
            data: Raw response bytes.

        Returns:
            True if data appears to be a ZIP file.
        """
        return data[:4] == b"PK\x03\x04"
