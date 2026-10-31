"""Tests for 3MF mesh extraction and bundle safety guards."""

import io
import zipfile

from django.test import TestCase

from core.services.threemf import (
    MAX_3MF_UNCOMPRESSED_BYTES,
    ThreeMFError,
    extract_meshes_from_3mf,
)

NS = "http://schemas.microsoft.com/3dmanufacturing/core/2015/02"

_MODEL_XML = f"""<?xml version="1.0" encoding="UTF-8"?>
<model xmlns="{NS}">
  <resources>
    <object id="1" type="model">
      <mesh>
        <vertices>
          <vertex x="0" y="0" z="0"/>
          <vertex x="1" y="0" z="0"/>
          <vertex x="0" y="1" z="0"/>
        </vertices>
        <triangles>
          <triangle v1="0" v2="1" v3="2"/>
        </triangles>
      </mesh>
    </object>
  </resources>
</model>
"""


def _make_3mf(model_xml: str, path: str = "3D/3dmodel.model") -> bytes:
    """Build a minimal in-memory 3MF ZIP containing a single model file."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(path, model_xml)
    return buffer.getvalue()


class ExtractMeshesFrom3mfTests(TestCase):
    """Behaviour of :func:`extract_meshes_from_3mf`."""

    def test_extracts_vertices_and_triangles(self) -> None:
        meshes = extract_meshes_from_3mf(_make_3mf(_MODEL_XML))
        self.assertEqual(len(meshes), 1)
        vertices, triangles = meshes[0]
        self.assertEqual(len(vertices), 3)
        self.assertEqual(triangles, [(0, 1, 2)])

    def test_prefers_standard_model_path(self) -> None:
        """A stray extra ``.model`` file must not shadow ``3D/3dmodel.model``."""
        decoy = _MODEL_XML.replace('x="1" y="0"', 'x="9" y="9"')
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("3D/other.model", decoy)
            zf.writestr("3D/3dmodel.model", _MODEL_XML)
        meshes = extract_meshes_from_3mf(buffer.getvalue())
        # The standard path (not the decoy) is parsed.
        self.assertEqual(meshes[0][0][1], (1.0, 0.0, 0.0))

    def test_invalid_zip_raises_threemf_error(self) -> None:
        with self.assertRaises(ThreeMFError):
            extract_meshes_from_3mf(b"not a zip file")

    def test_malformed_xml_raises_threemf_error(self) -> None:
        with self.assertRaises(ThreeMFError):
            extract_meshes_from_3mf(_make_3mf("<model><unclosed>"))

    def test_no_model_file_raises_threemf_error(self) -> None:
        with self.assertRaises(ThreeMFError):
            extract_meshes_from_3mf(_make_3mf(_MODEL_XML, path="Metadata/thumbnail.png"))

    def test_no_meshes_raises_threemf_error(self) -> None:
        empty = f'<?xml version="1.0"?><model xmlns="{NS}"><resources/></model>'
        with self.assertRaises(ThreeMFError):
            extract_meshes_from_3mf(_make_3mf(empty))

    def test_zip_bomb_guard_rejects_oversized_archive(self) -> None:
        """An archive that decompresses beyond the cap is rejected before parsing."""
        oversized = b"A" * (MAX_3MF_UNCOMPRESSED_BYTES + 1)
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("3D/3dmodel.model", _MODEL_XML)
            zf.writestr("big.bin", oversized)
        with self.assertRaises(ThreeMFError):
            extract_meshes_from_3mf(buffer.getvalue())


def _binary_stl(triangles: int = 1) -> bytes:
    """Build a minimal binary STL with ``triangles`` identical facets."""
    import struct

    facet = struct.pack("<12fH", 0, 0, 1, 0, 0, 0, 1, 0, 0, 0, 1, 0, 0)
    return b"\0" * 80 + struct.pack("<I", triangles) + facet * triangles


_OBJECT_XML = _MODEL_XML.split("<resources>")[1].split("</resources>")[0]
_SECOND_OBJECT_XML = _OBJECT_XML.replace('id="1"', 'id="2"')
_TWO_MESH_XML = _MODEL_XML.replace("</resources>", f"{_SECOND_OBJECT_XML}</resources>")


class BundleObjectMappingTests(TestCase):
    """``create_3mf_bundle`` records which part copy every 3MF object belongs to."""

    def setUp(self) -> None:
        import tempfile
        from pathlib import Path

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def _file(self, name: str, data: bytes) -> str:
        path = self.dir / name
        path.write_bytes(data)
        return str(path)

    def _model_object_ids(self, bundle: bytes) -> list[int]:
        from defusedxml.ElementTree import fromstring

        with zipfile.ZipFile(io.BytesIO(bundle)) as zf:
            root = fromstring(zf.read("3D/3dmodel.model"))
        return [int(obj.get("id")) for obj in root.iter(f"{{{NS}}}object")]

    def test_bundle_objects_numbered_per_copy(self) -> None:
        """Two STL parts with quantities 2 and 1 → objects 1..3 in part/copy order."""
        from core.services.threemf import BundleObject, create_3mf_bundle

        a = self._file("a.stl", _binary_stl())
        b = self._file("b.stl", _binary_stl())
        bundle, objects = create_3mf_bundle([(a, 2), (b, 1)])

        self.assertEqual(
            objects,
            [
                BundleObject(object_id=1, part_index=0, copy_index=0, mesh_index=0, mesh_count=1),
                BundleObject(object_id=2, part_index=0, copy_index=1, mesh_index=0, mesh_count=1),
                BundleObject(object_id=3, part_index=1, copy_index=0, mesh_index=0, mesh_count=1),
            ],
        )
        # The mapping matches the object IDs actually written into the model.
        self.assertEqual(self._model_object_ids(bundle), [1, 2, 3])

    def test_bundle_multi_mesh_part_has_mesh_count(self) -> None:
        """A 3MF part with two meshes yields two objects per copy, mesh-major."""
        from core.services.threemf import create_3mf_bundle

        multi = self._file("multi.3mf", _make_3mf(_TWO_MESH_XML))
        single = self._file("single.stl", _binary_stl())
        bundle, objects = create_3mf_bundle([(multi, 2), (single, 1)])

        self.assertEqual(
            [(o.object_id, o.part_index, o.mesh_index, o.copy_index, o.mesh_count) for o in objects],
            [
                (1, 0, 0, 0, 2),
                (2, 0, 0, 1, 2),
                (3, 0, 1, 0, 2),
                (4, 0, 1, 1, 2),
                (5, 1, 0, 0, 1),
            ],
        )
        self.assertEqual(self._model_object_ids(bundle), [1, 2, 3, 4, 5])

    def test_bundle_objects_helper_matches_bundle(self) -> None:
        """``bundle_objects`` (used by the backfill) numbers like the bundler."""
        from core.services.threemf import bundle_objects, create_3mf_bundle

        multi = self._file("multi.3mf", _make_3mf(_TWO_MESH_XML))
        _bundle, objects = create_3mf_bundle([(multi, 3)])
        self.assertEqual(bundle_objects([(3, 2)]), objects)

    def test_read_model_meshes_counts_meshes(self) -> None:
        """STL files have one mesh; 3MF files one per mesh object."""
        from core.services.threemf import read_model_meshes

        self.assertEqual(len(read_model_meshes(self._file("a.stl", _binary_stl()))), 1)
        self.assertEqual(len(read_model_meshes(self._file("m.3mf", _make_3mf(_TWO_MESH_XML)))), 2)

    def test_missing_file_raises(self) -> None:
        """A missing model file raises ``FileNotFoundError``."""
        from core.services.threemf import create_3mf_bundle

        with self.assertRaises(FileNotFoundError):
            create_3mf_bundle([(str(self.dir / "nope.stl"), 1)])
