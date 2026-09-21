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
