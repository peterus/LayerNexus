"""3MF bundle creation service.

Creates 3MF packages from multiple STL files for multi-part printing.
Uses only Python standard library (zipfile, struct, xml.etree).
"""

import io
import logging
import struct
import zipfile
from pathlib import Path
from xml.etree.ElementTree import Element, SubElement, tostring

logger = logging.getLogger(__name__)

# 3MF XML namespaces
NS_3MF = "http://schemas.microsoft.com/3dmanufacturing/core/2015/02"
NS_CONTENT_TYPES = "http://schemas.openxmlformats.org/package/2006/content-types"
NS_RELATIONSHIPS = "http://schemas.openxmlformats.org/package/2006/relationships"
REL_TYPE_3DMODEL = "http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"


class ThreeMFError(Exception):
    """Raised when 3MF bundle creation fails."""


def _parse_binary_stl(
    data: bytes,
) -> tuple[list[tuple[float, float, float]], list[tuple[int, int, int]]]:
    """Parse a binary STL file into deduplicated vertices and triangle indices.

    Args:
        data: Raw bytes of a binary STL file.

    Returns:
        Tuple of (vertices, triangles) where vertices is a list of
        (x, y, z) tuples and triangles is a list of (v1, v2, v3) index tuples.

    Raises:
        ThreeMFError: If the STL data is invalid or too short.
    """
    if len(data) < 84:
        raise ThreeMFError("STL file too short (< 84 bytes)")

    # Skip 80-byte header, read triangle count
    triangle_count = struct.unpack_from("<I", data, 80)[0]
    expected_size = 84 + triangle_count * 50
    if len(data) < expected_size:
        raise ThreeMFError(f"STL file truncated: expected {expected_size} bytes, got {len(data)}")

    vertex_map: dict[tuple[float, float, float], int] = {}
    vertices: list[tuple[float, float, float]] = []
    triangles: list[tuple[int, int, int]] = []

    offset = 84
    for _ in range(triangle_count):
        # Skip normal vector (3 floats = 12 bytes)
        # Read 3 vertices (each 3 floats = 12 bytes)
        tri_indices = []
        for vi in range(3):
            vx, vy, vz = struct.unpack_from("<fff", data, offset + 12 + vi * 12)
            # Round to avoid floating point noise in deduplication
            key = (round(vx, 6), round(vy, 6), round(vz, 6))
            if key not in vertex_map:
                vertex_map[key] = len(vertices)
                vertices.append(key)
            tri_indices.append(vertex_map[key])

        triangles.append((tri_indices[0], tri_indices[1], tri_indices[2]))
        # 12 (normal) + 36 (3 vertices) + 2 (attribute byte count) = 50
        offset += 50

    return vertices, triangles


def _build_content_types_xml() -> bytes:
    """Build [Content_Types].xml for the 3MF package.

    Returns:
        UTF-8 encoded XML bytes.
    """
    root = Element("Types", xmlns=NS_CONTENT_TYPES)
    SubElement(
        root,
        "Default",
        Extension="rels",
        ContentType="application/vnd.openxmlformats-package.relationships+xml",
    )
    SubElement(
        root,
        "Default",
        Extension="model",
        ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml",
    )
    return b'<?xml version="1.0" encoding="UTF-8"?>\n' + tostring(root, encoding="unicode").encode("utf-8")


def _build_rels_xml() -> bytes:
    """Build _rels/.rels for the 3MF package.

    Returns:
        UTF-8 encoded XML bytes.
    """
    root = Element("Relationships", xmlns=NS_RELATIONSHIPS)
    SubElement(
        root,
        "Relationship",
        Target="/3D/3dmodel.model",
        Id="rel0",
        Type=REL_TYPE_3DMODEL,
    )
    return b'<?xml version="1.0" encoding="UTF-8"?>\n' + tostring(root, encoding="unicode").encode("utf-8")


def _build_3dmodel_xml(
    objects: list[tuple[list[tuple[float, float, float]], list[tuple[int, int, int]], int]],
) -> bytes:
    """Build 3D/3dmodel.model XML with multiple objects.

    Each (vertices, triangles, quantity) tuple is expanded into
    *quantity* separate ``<object>`` elements, each with a unique ID.
    This ensures OrcaSlicer's arranger treats every copy as an
    independent instance that can be placed on a different plate.

    Args:
        objects: List of (vertices, triangles, quantity) tuples.

    Returns:
        UTF-8 encoded XML bytes.
    """
    root = Element("model", unit="millimeter", xmlns=NS_3MF)
    resources = SubElement(root, "resources")
    build = SubElement(root, "build")

    next_id = 1
    for vertices, triangles, quantity in objects:
        # Create a separate <object> for every copy so OrcaSlicer
        # treats each one as an independent instance it can arrange
        # across plates.  Re-using the same objectid with multiple
        # <item> entries is valid per the 3MF spec, but OrcaSlicer's
        # arranger may collapse duplicates when it imports non-native
        # 3MF files.
        for _copy in range(quantity):
            obj_id = str(next_id)
            next_id += 1
            obj_elem = SubElement(resources, "object", id=obj_id, type="model")
            mesh = SubElement(obj_elem, "mesh")

            verts_elem = SubElement(mesh, "vertices")
            for vx, vy, vz in vertices:
                SubElement(verts_elem, "vertex", x=str(vx), y=str(vy), z=str(vz))

            tris_elem = SubElement(mesh, "triangles")
            for v1, v2, v3 in triangles:
                SubElement(tris_elem, "triangle", v1=str(v1), v2=str(v2), v3=str(v3))

            SubElement(build, "item", objectid=obj_id)

    return b'<?xml version="1.0" encoding="UTF-8"?>\n' + tostring(root, encoding="unicode").encode("utf-8")


def extract_meshes_from_3mf(
    data: bytes,
) -> list[tuple[list[tuple[float, float, float]], list[tuple[int, int, int]]]]:
    """Extract mesh data from a 3MF file.

    Reads the ``3D/3dmodel.model`` XML inside the 3MF ZIP and parses
    all ``<object>`` elements with mesh geometry.

    Args:
        data: Raw bytes of a 3MF ZIP archive.

    Returns:
        List of (vertices, triangles) tuples — same format as
        :func:`_parse_binary_stl`.

    Raises:
        ThreeMFError: If the 3MF file is invalid or contains no meshes.
    """
    from defusedxml.ElementTree import fromstring

    try:
        with zipfile.ZipFile(io.BytesIO(data), "r") as zf:
            # Find the 3D model file
            model_path = None
            for name in zf.namelist():
                if name.lower().endswith(".model") and "3d/" in name.lower():
                    model_path = name
                    break
            if model_path is None:
                raise ThreeMFError("No 3D model file found in 3MF archive")

            model_xml = zf.read(model_path)
    except zipfile.BadZipFile as exc:
        raise ThreeMFError(f"Invalid 3MF file: {exc}") from exc

    root = fromstring(model_xml)

    # Detect namespace from root tag
    ns = ""
    if root.tag.startswith("{"):
        ns = root.tag.split("}")[0] + "}"

    meshes = []
    for obj in root.iter(f"{ns}object"):
        mesh = obj.find(f"{ns}mesh")
        if mesh is None:
            continue

        vertices_elem = mesh.find(f"{ns}vertices")
        triangles_elem = mesh.find(f"{ns}triangles")
        if vertices_elem is None or triangles_elem is None:
            continue

        vertices = []
        for v in vertices_elem.findall(f"{ns}vertex"):
            vertices.append((
                float(v.get("x", "0")),
                float(v.get("y", "0")),
                float(v.get("z", "0")),
            ))

        triangles = []
        for t in triangles_elem.findall(f"{ns}triangle"):
            triangles.append((
                int(t.get("v1", "0")),
                int(t.get("v2", "0")),
                int(t.get("v3", "0")),
            ))

        if vertices and triangles:
            meshes.append((vertices, triangles))

    if not meshes:
        raise ThreeMFError("No mesh objects found in 3MF file")

    logger.debug("Extracted %d mesh(es) from 3MF file", len(meshes))
    return meshes


def create_3mf_bundle(parts: list[tuple[Path, int]]) -> bytes:
    """Create a 3MF package containing multiple model files.

    Accepts both STL and 3MF files.  Each file is parsed into mesh
    data and added as ``<object>`` elements in the output 3MF model.
    The ``quantity`` determines how many ``<item>`` references are
    created in the ``<build>`` section.

    Args:
        parts: List of (model_path, quantity) tuples.

    Returns:
        Bytes of the 3MF ZIP archive.

    Raises:
        ThreeMFError: If any file cannot be parsed.
        FileNotFoundError: If any file does not exist.
    """
    if not parts:
        raise ThreeMFError("No parts provided for 3MF bundle")

    objects = []
    for model_path, quantity in parts:
        model_path = Path(model_path)
        if not model_path.exists():
            raise FileNotFoundError(f"Model file not found: {model_path}")

        file_data = model_path.read_bytes()

        if model_path.suffix.lower() == ".3mf":
            meshes = extract_meshes_from_3mf(file_data)
            for vertices, triangles in meshes:
                objects.append((vertices, triangles, quantity))
            logger.debug(
                "Parsed 3MF %s: %d mesh(es), qty=%d",
                model_path.name,
                len(meshes),
                quantity,
            )
        else:
            # Detect ASCII STL (starts with "solid") vs binary
            if file_data[:5] == b"solid" and b"\n" in file_data[:100]:
                raise ThreeMFError(
                    f"ASCII STL files are not supported: {model_path.name}. Please convert to binary STL."
                )

            vertices, triangles = _parse_binary_stl(file_data)
            objects.append((vertices, triangles, quantity))
            logger.debug(
                "Parsed STL %s: %d vertices, %d triangles, qty=%d",
                model_path.name,
                len(vertices),
                len(triangles),
                quantity,
            )

    # Build the 3MF ZIP archive in memory
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", _build_content_types_xml())
        zf.writestr("_rels/.rels", _build_rels_xml())
        zf.writestr("3D/3dmodel.model", _build_3dmodel_xml(objects))

    bundle_bytes = buffer.getvalue()
    logger.info(
        "Created 3MF bundle: %d objects, %d bytes",
        len(objects),
        len(bundle_bytes),
    )
    return bundle_bytes
