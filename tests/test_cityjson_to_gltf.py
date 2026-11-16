"""Tests for the CityJSON -> glTF conversion.

Each test pins a property that is easy to break silently: a wrong CRS or a lost
recentring produces a file that loads fine and renders in the wrong place, or
not at all.
"""

from __future__ import annotations

import json
import math
import struct
import sys
from pathlib import Path

import pytest

# Make the src-layout package importable from a source checkout and the image.
_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent / "src"))

from cityjson_gltf import converter as module


def _cube_cityjson(epsg: int = 25828) -> dict:
    """A single 10x10x5 box, quantised the way extrude_cityjson.py writes them."""
    return {
        "type": "CityJSON",
        "version": "2.0",
        "metadata": {"referenceSystem": f"https://www.opengis.net/def/crs/EPSG/0/{epsg}"},
        "transform": {"scale": [0.001, 0.001, 0.001], "translate": [640000.0, 3230000.0, 0.0]},
        "CityObjects": {
            "B1": {
                "type": "Building",
                "attributes": {"measuredHeight": 5.0},
                "geometry": [{
                    "type": "Solid",
                    "lod": "1",
                    "boundaries": [[
                        [[0, 1, 2, 3]],
                        [[4, 5, 6, 7]],
                        [[0, 1, 5, 4]],
                    ]],
                }],
            }
        },
        "vertices": [
            [0, 0, 0], [10000, 0, 0], [10000, 10000, 0], [0, 10000, 0],
            [0, 0, 5000], [10000, 0, 5000], [10000, 10000, 5000], [0, 10000, 5000],
        ],
    }


def test_parse_reference_system_reads_epsg():
    assert module.parse_reference_system(_cube_cityjson(25828)) == 25828


def test_parse_reference_system_missing_is_none():
    assert module.parse_reference_system({"metadata": {}}) is None


def test_dequantise_applies_scale_and_translate():
    vertices = module.dequantise(_cube_cityjson())
    # vertex 1 is 10000 * 0.001 = 10 m east of the translate origin
    assert vertices[0] == pytest.approx([640000.0, 3230000.0, 0.0])
    assert vertices[1] == pytest.approx([640010.0, 3230000.0, 0.0])


def _area_xy(triangles, vertices):
    """Planar area of a tessellation in the XY plane."""
    total = 0.0
    for tri in triangles:
        a, b, c = (vertices[i] for i in tri)
        total += abs((b[0] - a[0]) * (c[1] - a[1]) - (c[0] - a[0]) * (b[1] - a[1])) / 2
    return total


def test_triangulate_fans_a_quad_into_two_triangles():
    # Without vertices there is nothing to project, so the fan is all we can do.
    assert module.triangulate([[[0, 1, 2, 3]]]) == [[0, 1, 2], [0, 2, 3]]


def test_square_with_courtyard_excludes_the_hole():
    """The user's example: a 100x100 block with a 70x70 patio is 5100 m2 of
    building, not 10000. Filling the hole in would invent 4900 m2 of floor and
    cast a shadow over an open courtyard."""
    vertices = [
        [0.0, 0.0, 0.0], [100.0, 0.0, 0.0], [100.0, 100.0, 0.0], [0.0, 100.0, 0.0],
        [15.0, 15.0, 0.0], [85.0, 15.0, 0.0], [85.0, 85.0, 0.0], [15.0, 85.0, 0.0],
    ]
    surface = [[0, 1, 2, 3], [4, 5, 6, 7]]

    triangles = module.triangulate([surface], vertices)

    assert _area_xy(triangles, vertices) == pytest.approx(100 * 100 - 70 * 70)


def test_concave_footprint_is_not_filled_in():
    """An L-shaped block: a triangle fan from vertex 0 spans the notch and
    reports the full bounding square. 447 of La Graciosa's 538 footprints are
    concave, so this was the larger error of the two."""
    vertices = [
        [0.0, 0.0, 0.0], [10.0, 0.0, 0.0], [10.0, 4.0, 0.0],
        [4.0, 4.0, 0.0], [4.0, 10.0, 0.0], [0.0, 10.0, 0.0],
    ]
    surface = [[0, 1, 2, 3, 4, 5]]

    triangles = module.triangulate([surface], vertices)

    # The L covers 10x4 plus 4x6; the bounding square would be 100.
    assert _area_xy(triangles, vertices) == pytest.approx(10 * 4 + 4 * 6)


def test_convex_footprint_area_is_preserved():
    """The tessellator must not change a simple polygon either."""
    vertices = [[0.0, 0.0, 0.0], [10.0, 0.0, 0.0], [10.0, 10.0, 0.0], [0.0, 10.0, 0.0]]
    triangles = module.triangulate([[[0, 1, 2, 3]]], vertices)
    assert _area_xy(triangles, vertices) == pytest.approx(100.0)


def test_triangulate_skips_degenerate_rings():
    assert module.triangulate([[[0, 1]]]) == []


def test_collect_geometry_duplicates_vertices_per_feature():
    """Vertices must not be shared between objects: a glTF feature id is a
    per-vertex attribute, so sharing would break per-building picking."""
    cityjson = _cube_cityjson()
    vertices = module.dequantise(cityjson)
    positions, indices, feature_ids, object_ids = module.collect_geometry(cityjson, vertices)

    assert object_ids == ["B1"]
    assert len(positions) == len(indices) == len(feature_ids)
    assert set(feature_ids) == {0}


def test_local_coordinates_are_small_offsets():
    """The whole point of recentring: raw ECEF is ~6.4e6 and float32 would
    quantise it to metres, making the model jitter."""
    cityjson = _cube_cityjson()
    vertices = module.dequantise(cityjson)
    positions, _, _, _ = module.collect_geometry(cityjson, vertices)

    local, centre = module.to_local_y_up(positions, module.build_transformer(25828))

    assert max(abs(c) for c in centre) > 1e6, "centre should be a real ECEF position"
    assert all(abs(v) < 1000 for p in local for v in p), "local coords must be small"


def test_local_y_up_preserves_building_height():
    """Y is up in glTF. A 5 m box must still be 5 m tall after the basis change."""
    cityjson = _cube_cityjson()
    vertices = module.dequantise(cityjson)
    positions, _, _, _ = module.collect_geometry(cityjson, vertices)

    local, _ = module.to_local_y_up(positions, module.build_transformer(25828))

    height = max(p[1] for p in local) - min(p[1] for p in local)
    assert height == pytest.approx(5.0, abs=0.01)


def test_enu_transform_places_origin_at_given_ecef():
    transform = module.enu_transform(5416317.0, -1300499.0, 3096489.0)
    assert len(transform) == 16
    # Column-major: the translation occupies the last column.
    assert transform[12:15] == [5416317.0, -1300499.0, 3096489.0]
    assert transform[15] == 1.0


def test_enu_basis_is_orthonormal():
    transform = module.enu_transform(5416317.0, -1300499.0, 3096489.0)
    east, north, up = transform[0:3], transform[4:7], transform[8:11]
    for axis in (east, north, up):
        assert math.sqrt(sum(c * c for c in axis)) == pytest.approx(1.0, abs=1e-9)
    assert sum(e * n for e, n in zip(east, north)) == pytest.approx(0.0, abs=1e-9)
    assert sum(e * u for e, u in zip(east, up)) == pytest.approx(0.0, abs=1e-9)


def test_write_glb_produces_a_valid_container(tmp_path: Path):
    """Header, chunk layout and 4-byte alignment — a malformed .glb fails to
    load with no useful error in the viewer."""
    cityjson = _cube_cityjson()
    vertices = module.dequantise(cityjson)
    positions, indices, feature_ids, object_ids = module.collect_geometry(cityjson, vertices)
    local, _ = module.to_local_y_up(positions, module.build_transformer(25828))
    normals = module.compute_normals(local, indices)

    out = tmp_path / "out.glb"
    module.write_glb(out, local, indices, normals, feature_ids, object_ids,
                     {"id": object_ids, "measuredHeight": [5.0]})

    data = out.read_bytes()
    magic, version, total = struct.unpack("<III", data[:12])
    assert magic == 0x46546C67, "glTF magic"
    assert version == 2
    assert total == len(data), "header length must match the real file size"

    json_len, json_type = struct.unpack("<II", data[12:20])
    assert json_type == 0x4E4F534A, "first chunk must be JSON"
    gltf = json.loads(data[20:20 + json_len])
    assert gltf["asset"]["version"] == "2.0"
    assert "EXT_mesh_features" in gltf["extensionsUsed"]
    assert "_FEATURE_ID_0" in gltf["meshes"][0]["primitives"][0]["attributes"]


def test_glb_metadata_exposes_requested_attributes(tmp_path: Path):
    """Cesium styles by property name (the viewer uses currentUse), so the
    property table must carry the attributes asked for."""
    cityjson = _cube_cityjson()
    vertices = module.dequantise(cityjson)
    positions, indices, feature_ids, object_ids = module.collect_geometry(cityjson, vertices)
    local, _ = module.to_local_y_up(positions, module.build_transformer(25828))
    normals = module.compute_normals(local, indices)

    out = tmp_path / "out.glb"
    module.write_glb(out, local, indices, normals, feature_ids, object_ids,
                     {"id": object_ids, "measuredHeight": [5.0]})

    data = out.read_bytes()
    json_len = struct.unpack("<I", data[12:16])[0]
    gltf = json.loads(data[20:20 + json_len])
    table = gltf["extensions"]["EXT_structural_metadata"]["propertyTables"][0]
    assert table["count"] == 1
    assert set(table["properties"]) == {"id", "measuredHeight"}


def test_tileset_bounding_box_is_written(tmp_path: Path):
    """publish_buildings.py reads root.boundingVolume.box to pick the shard."""
    positions = [[-10.0, 0.0, -10.0], [10.0, 5.0, 10.0]]
    path = tmp_path / "tileset.json"
    module.write_tileset(path, "out.glb", module.enu_transform(5416317.0, -1300499.0, 3096489.0), positions)

    tileset = json.loads(path.read_text())
    assert tileset["root"]["content"]["uri"] == "out.glb"
    assert len(tileset["root"]["boundingVolume"]["box"]) == 12
    assert len(tileset["root"]["transform"]) == 16
