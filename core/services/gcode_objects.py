"""Map the objects in sliced G-code back to the job parts on each plate.

OrcaSlicer labels every object of the 3MF bundle in the G-code it writes per
plate — ``EXCLUDE_OBJECT_DEFINE NAME=Object_<N>_id_<i>_copy_0`` in the header
and ``; printing object Object_<N> …`` in the body — where ``<N>`` is the 3MF
object ID assigned by :func:`core.services.threemf.create_3mf_bundle`.  Together
with the bundle's :class:`~core.services.threemf.BundleObject` list this tells
which part copies (and which of their meshes) ended up on which plate.

These are pure functions without database access; the plate-parts data
migration uses them as well, so keep them backwards compatible.
"""

import logging
import re
from collections import Counter
from dataclasses import dataclass

from core.services.threemf import BundleObject

logger = logging.getLogger(__name__)

_EXCLUDE_OBJECT_RE = re.compile(rb"^\s*EXCLUDE_OBJECT_DEFINE\b[^\n]*?\bNAME=['\"]?Object_(\d+)(?![0-9])", re.MULTILINE)
_PRINTING_OBJECT_RE = re.compile(rb"^\s*;\s*printing object\s+['\"]?Object_(\d+)(?![0-9])", re.MULTILINE)


@dataclass(frozen=True)
class PlateCopyShare:
    """How much of one part copy lies on one plate.

    Attributes:
        part_index: Index of the part in the bundle (see ``BundleObject.part_index``).
        copy_index: Copy of that part (0-based).
        meshes_on_plate: Number of the copy's meshes on this plate.
        mesh_count: Total number of meshes of the copy.
    """

    part_index: int
    copy_index: int
    meshes_on_plate: int
    mesh_count: int


def parse_gcode_object_ids(gcode: bytes) -> list[int]:
    """Return the bundle object IDs printed by a plate's G-code.

    Reads the ``EXCLUDE_OBJECT_DEFINE NAME=Object_<N>…`` lines; when there are
    none, falls back to the ``; printing object Object_<N>`` comments.

    Args:
        gcode: Raw G-code of one plate.

    Returns:
        The unique object IDs, sorted ascending (empty if the G-code has no labels).
    """
    ids = {int(match) for match in _EXCLUDE_OBJECT_RE.findall(gcode)}
    if not ids:
        ids = {int(match) for match in _PRINTING_OBJECT_RE.findall(gcode)}
    return sorted(ids)


def assign_plate_objects(
    objects: list[BundleObject],
    plate_object_ids: dict[int, list[int]],
) -> dict[int, list[PlateCopyShare]] | None:
    """Work out which part copies lie on which plate.

    Fallback when the G-code carries no object labels: a single plate gets all
    bundle objects; with several plates the assignment is unknown.

    Args:
        objects: The bundle objects (object-to-part mapping) of the sliced bundle.
        plate_object_ids: Plate number → object IDs parsed from that plate's G-code.

    Returns:
        Plate number → copy shares on that plate (plates without objects map to
        an empty list), or ``None`` if the assignment cannot be determined
        (labels missing on a multi-plate job, an unknown object ID, or an
        object on more than one plate).  Objects on no plate are simply absent.
    """
    by_id = {obj.object_id: obj for obj in objects}
    plate_ids = dict(plate_object_ids)

    if plate_ids and not any(plate_ids.values()):
        if len(plate_ids) > 1:
            logger.warning("G-code of %d plates has no object labels; plate assignment unknown", len(plate_ids))
            return None
        (only_plate,) = plate_ids
        plate_ids[only_plate] = sorted(by_id)
    elif any(not ids for ids in plate_ids.values()) and len(plate_ids) > 1:
        # Some plates are labelled and some are not: the unlabelled ones cannot be attributed.
        logger.warning("Some plates have no object labels; plate assignment unknown")
        return None

    seen: set[int] = set()
    result: dict[int, list[PlateCopyShare]] = {}
    for plate_number, ids in plate_ids.items():
        meshes: Counter[tuple[int, int]] = Counter()
        mesh_counts: dict[tuple[int, int], int] = {}
        for object_id in ids:
            obj = by_id.get(object_id)
            if obj is None or object_id in seen:
                logger.warning(
                    "Object %s on plate %s is unknown or on several plates; plate assignment unknown",
                    object_id,
                    plate_number,
                )
                return None
            seen.add(object_id)
            key = (obj.part_index, obj.copy_index)
            meshes[key] += 1
            mesh_counts[key] = obj.mesh_count
        result[plate_number] = [
            PlateCopyShare(
                part_index=part_index,
                copy_index=copy_index,
                meshes_on_plate=count,
                mesh_count=mesh_counts[(part_index, copy_index)],
            )
            for (part_index, copy_index), count in sorted(meshes.items())
        ]
    return result
