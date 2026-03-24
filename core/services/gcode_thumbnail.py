"""Extract embedded thumbnail images from G-code files.

OrcaSlicer (and PrusaSlicer) embed PNG thumbnails in G-code comments
using a well-known format::

    ; thumbnail begin 300x300 12345
    ; iVBORw0KGgoAAAANSUhEU...
    ; ...base64 data...
    ; thumbnail end

This module provides functions to extract those thumbnails and return
them as raw PNG bytes.
"""

from __future__ import annotations

import base64
import logging
import re
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# Regex matching the ``; thumbnail begin WxH LEN`` header line.
_THUMB_BEGIN_RE = re.compile(
    r";\s*thumbnail\s+begin\s+(\d+)x(\d+)\s+(\d+)",
    re.IGNORECASE,
)
_THUMB_END_RE = re.compile(r";\s*thumbnail\s+end", re.IGNORECASE)


@dataclass
class GCodeThumbnail:
    """A single thumbnail extracted from G-code.

    Attributes:
        width: Image width in pixels.
        height: Image height in pixels.
        png_data: Raw PNG image bytes.
    """

    width: int
    height: int
    png_data: bytes


def extract_thumbnails(gcode: bytes | str) -> list[GCodeThumbnail]:
    """Extract all embedded thumbnails from G-code content.

    Scans the G-code for ``; thumbnail begin`` / ``; thumbnail end``
    blocks and decodes the base64-encoded PNG data between them.

    Args:
        gcode: Raw G-code content (bytes or string).

    Returns:
        List of :class:`GCodeThumbnail` instances, ordered by
        descending resolution (largest first).
    """
    if isinstance(gcode, bytes):
        # Only scan the first ~500 KB – thumbnails are always near the top
        text = gcode[:512_000].decode("utf-8", errors="replace")
    else:
        text = gcode[:512_000]

    thumbnails: list[GCodeThumbnail] = []
    lines = text.splitlines()
    i = 0

    while i < len(lines):
        match = _THUMB_BEGIN_RE.match(lines[i].strip())
        if match:
            width = int(match.group(1))
            height = int(match.group(2))
            b64_lines: list[str] = []
            i += 1

            # Collect base64 lines until ``; thumbnail end``
            while i < len(lines):
                stripped = lines[i].strip()
                if _THUMB_END_RE.match(stripped):
                    break
                # Remove leading ``; `` comment prefix
                if stripped.startswith(";"):
                    b64_lines.append(stripped[1:].strip())
                i += 1

            # Decode
            b64_str = "".join(b64_lines)
            if b64_str:
                try:
                    png_data = base64.b64decode(b64_str)
                    # Verify PNG signature
                    if png_data[:4] == b"\x89PNG":
                        thumbnails.append(
                            GCodeThumbnail(
                                width=width,
                                height=height,
                                png_data=png_data,
                            )
                        )
                        logger.debug(
                            "Extracted %dx%d thumbnail (%d bytes)",
                            width,
                            height,
                            len(png_data),
                        )
                    else:
                        logger.warning(
                            "Decoded thumbnail %dx%d is not a valid PNG",
                            width,
                            height,
                        )
                except Exception:
                    logger.warning(
                        "Failed to decode base64 thumbnail %dx%d",
                        width,
                        height,
                    )
        i += 1

    # Sort largest first
    thumbnails.sort(key=lambda t: t.width * t.height, reverse=True)
    return thumbnails


def extract_largest_thumbnail(gcode: bytes | str) -> bytes | None:
    """Extract the largest thumbnail PNG from G-code.

    Convenience wrapper around :func:`extract_thumbnails` that returns
    only the raw PNG bytes of the biggest image, or ``None`` if no
    thumbnails are found.

    Args:
        gcode: Raw G-code content (bytes or string).

    Returns:
        Raw PNG bytes of the largest thumbnail, or ``None``.
    """
    thumbs = extract_thumbnails(gcode)
    if thumbs:
        return thumbs[0].png_data
    return None
