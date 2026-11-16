#!/usr/bin/env python3
"""Convert a CityJSON document into binary glTF (.glb), optionally with a
3D Tiles `tileset.json` wrapping it.

The converter turns CityJSON building geometry into a compact glTF asset while
preserving feature metadata for picking and styling in compatible viewers.

Coordinates
-----------
glTF is a *local* Cartesian space in metres, Y-up. 3D Tiles places it on the
globe with a 4x4 `transform` in ECEF (EPSG:4978). So this script:

  1. reprojects the CityJSON vertices to ECEF,
  2. subtracts the centroid, making vertices local metre offsets (small floats —
     absolute ECEF values are ~6.4e6 and would lose precision in float32),
  3. rotates Z-up (the CRS convention) to Y-up (the glTF convention),
  4. writes the centroid, as an ENU-oriented basis, into the tileset transform.

Step 2 is not an optimisation: float32 has ~7 significant digits, so storing raw
ECEF would quantise positions to metres and the model would visibly jitter.

Usage:
  python -m cityjson_gltf.converter <in.city.json> <out.glb>
      [--tileset <tileset.json>]
                      [--srs-in EPSG] [--attributes a,b,c]

`--srs-in` overrides the CRS declared in the file's `metadata.referenceSystem`.
"""

from __future__ import annotations

import argparse
import json
import math
import struct
import sys
from pathlib import Path

# WGS84, for the ENU basis of the tileset transform.
WGS84_A = 6378137.0
WGS84_E2 = 6.69437999014e-3


def parse_reference_system(cityjson: dict) -> int | None:
    """EPSG code declared in the CityJSON metadata, if any.

    The value is a URN like `https://www.opengis.net/def/crs/EPSG/0/25828`;
    only the trailing code matters here.
    """
    ref = (cityjson.get("metadata") or {}).get("referenceSystem")
    if not ref:
        return None
    tail = str(ref).rstrip("/").split("/")[-1]
    try:
        return int(tail)
    except ValueError:
        return None


def dequantise(cityjson: dict) -> list[list[float]]:
    """Undo the CityJSON `transform`, returning real-world vertices.

    CityJSON stores integers plus a scale/translate pair; without applying it the
    coordinates are meaningless.
    """
    verts = cityjson.get("vertices") or []
    transform = cityjson.get("transform")
    if not transform:
        return [[float(v[0]), float(v[1]), float(v[2])] for v in verts]

    sx, sy, sz = transform["scale"]
    tx, ty, tz = transform["translate"]
    return [
        [v[0] * sx + tx, v[1] * sy + ty, v[2] * sz + tz]
        for v in verts
    ]


def build_transformer(src_epsg: int):
    """Return a callable (x, y, z) -> ECEF metres.

    pyproj is required: reprojecting to a geocentric CRS by hand is exactly the
    kind of thing that silently produces a model in the wrong place.
    """
    try:
        from pyproj import Transformer
    except ImportError:  # pragma: no cover - environment error, not logic
        sys.exit("pyproj is required to reproject to ECEF (EPSG:4978)")

    transformer = Transformer.from_crs(src_epsg, 4978, always_xy=True)

    def to_ecef(x: float, y: float, z: float) -> tuple[float, float, float]:
        return transformer.transform(x, y, z)

    return to_ecef


def enu_transform(cx: float, cy: float, cz: float) -> list[float]:
    """A 4x4 column-major 3D Tiles transform placing local ENU axes at an ECEF point.

    The model is authored around the origin in metres; this matrix carries it to
    its place on the globe, east/north/up aligned.
    """
    lon = math.atan2(cy, cx)
    p = math.sqrt(cx * cx + cy * cy)
    lat = math.atan2(cz, p * (1 - WGS84_E2))
    for _ in range(6):
        sin_lat = math.sin(lat)
        n = WGS84_A / math.sqrt(1 - WGS84_E2 * sin_lat * sin_lat)
        alt = p / math.cos(lat) - n
        lat = math.atan2(cz, p * (1 - WGS84_E2 * n / (n + alt)))

    sin_lon, cos_lon = math.sin(lon), math.cos(lon)
    sin_lat, cos_lat = math.sin(lat), math.cos(lat)

    east = (-sin_lon, cos_lon, 0.0)
    north = (-sin_lat * cos_lon, -sin_lat * sin_lon, cos_lat)
    up = (cos_lat * cos_lon, cos_lat * sin_lon, sin_lat)

    # Column-major, as 3D Tiles requires.
    return [
        east[0], east[1], east[2], 0.0,
        north[0], north[1], north[2], 0.0,
        up[0], up[1], up[2], 0.0,
        cx, cy, cz, 1.0,
    ]


def _ring_normal(points: list[list[float]]) -> tuple[float, float, float]:
    """Newell's method: a polygon normal that works for any planar ring."""
    nx = ny = nz = 0.0
    count = len(points)
    for i in range(count):
        a = points[i]
        b = points[(i + 1) % count]
        nx += (a[1] - b[1]) * (a[2] + b[2])
        ny += (a[2] - b[2]) * (a[0] + b[0])
        nz += (a[0] - b[0]) * (a[1] + b[1])
    return nx, ny, nz


def _project_to_plane(rings: list[list[int]], vertices: list[list[float]]):
    """Flatten a planar 3D surface to 2D so it can be tessellated.

    Drops whichever axis the surface's normal points along most — the standard
    trick, and exact for the axis-aligned floors/roofs and vertical walls an LOD1
    extrusion produces.
    """
    exterior = [vertices[i] for i in rings[0]]
    nx, ny, nz = _ring_normal(exterior)
    ax, ay, az = abs(nx), abs(ny), abs(nz)
    if ax >= ay and ax >= az:
        pick = (1, 2)
    elif ay >= az:
        pick = (0, 2)
    else:
        pick = (0, 1)

    flat = []
    for ring in rings:
        flat.append([(vertices[i][pick[0]], vertices[i][pick[1]]) for i in ring])
    return flat


def triangulate(boundaries: list, vertices: list[list[float]] | None = None) -> list[list[int]]:
    """Tessellate CityJSON surface boundaries into index triples.

    A surface is a list of rings: the exterior first, then any interior rings.
    Two properties of real cadastral footprints make a naive triangle fan wrong,
    and both inflate the geometry rather than failing visibly:

    - **Interior rings are courtyards** and must stay holes. Filling one in adds
      floor area that does not exist and casts a shadow over open ground.
    - **Exterior rings are usually concave** — L- and U-shaped blocks. 447 of
      La Graciosa's 538 buildings are. A fan from vertex 0 spans the notch of a
      concave polygon, so it covers ground the building does not occupy.

    Everything therefore goes through mapbox_earcut, which handles both. The fan
    survives only as a fallback for when `vertices` is not supplied (it cannot
    project to 2D without them) and for degenerate 3-point rings, where a fan and
    a tessellation are the same single triangle.
    """
    triangles = []
    for surface in boundaries:
        if not surface:
            continue

        rings = [r for r in surface if len(r) >= 3]
        if not rings:
            continue

        if vertices is None or (len(rings) == 1 and len(rings[0]) == 3):
            ring = rings[0]
            for i in range(1, len(ring) - 1):
                triangles.append([ring[0], ring[i], ring[i + 1]])
            continue

        flat = _project_to_plane(rings, vertices)
        try:
            import numpy as np
            from mapbox_earcut import triangulate_float64
        except ImportError:  # pragma: no cover - environment error
            sys.exit(
                "mapbox_earcut is required: cadastral footprints are concave and "
                "have courtyards, and a triangle fan covers ground they do not"
            )

        flat_vertices = np.array([p for ring in flat for p in ring], dtype=np.float64)
        ring_ends = []
        total = 0
        for ring in flat:
            total += len(ring)
            ring_ends.append(total)

        local = triangulate_float64(flat_vertices, np.array(ring_ends, dtype=np.uint32))

        # earcut indexes its own flattened array; map back to CityJSON indices.
        lookup = [index for ring in rings for index in ring]
        for i in range(0, len(local) - 2, 3):
            triangles.append([lookup[local[i]], lookup[local[i + 1]], lookup[local[i + 2]]])

    return triangles


def collect_geometry(cityjson: dict, vertices: list[list[float]]):
    """Flatten every CityObject into triangles, tracking which object each belongs to.

    Returns (positions, indices, feature_ids, object_ids). Vertices are
    duplicated per object so that each triangle can carry a feature id — glTF
    accessors are per-vertex, so sharing vertices between objects would make
    per-building picking impossible.
    """
    positions: list[list[float]] = []
    indices: list[int] = []
    feature_ids: list[int] = []
    object_ids: list[str] = []

    for object_id, city_object in (cityjson.get("CityObjects") or {}).items():
        geometries = city_object.get("geometry") or []
        if not geometries:
            continue

        feature_index = len(object_ids)
        emitted = False

        for geometry in geometries:
            boundaries = geometry.get("boundaries") or []
            # Solid nests one level deeper than MultiSurface: [shell][surface].
            shells = boundaries if geometry.get("type") == "Solid" else [boundaries]
            for shell in shells:
                for triangle in triangulate(shell, vertices):
                    for vertex_index in triangle:
                        if vertex_index >= len(vertices):
                            continue
                        indices.append(len(positions))
                        positions.append(vertices[vertex_index])
                        feature_ids.append(feature_index)
                        emitted = True

        if emitted:
            object_ids.append(object_id)

    return positions, indices, feature_ids, object_ids


def to_local_y_up(positions: list[list[float]], to_ecef) -> tuple[list[list[float]], list[float]]:
    """Reproject to ECEF, recentre on the centroid, and convert Z-up to Y-up.

    Returns the local vertices and the ECEF centre they are relative to.
    """
    ecef = [to_ecef(x, y, z) for x, y, z in positions]

    cx = sum(p[0] for p in ecef) / len(ecef)
    cy = sum(p[1] for p in ecef) / len(ecef)
    cz = sum(p[2] for p in ecef) / len(ecef)

    lon = math.atan2(cy, cx)
    p_xy = math.sqrt(cx * cx + cy * cy)
    lat = math.atan2(cz, p_xy * (1 - WGS84_E2))
    sin_lon, cos_lon = math.sin(lon), math.cos(lon)
    sin_lat, cos_lat = math.sin(lat), math.cos(lat)

    east = (-sin_lon, cos_lon, 0.0)
    north = (-sin_lat * cos_lon, -sin_lat * sin_lon, cos_lat)
    up = (cos_lat * cos_lon, cos_lat * sin_lon, sin_lat)

    local = []
    for x, y, z in ecef:
        dx, dy, dz = x - cx, y - cy, z - cz
        e = dx * east[0] + dy * east[1] + dz * east[2]
        n = dx * north[0] + dy * north[1] + dz * north[2]
        u = dx * up[0] + dy * up[1] + dz * up[2]
        # glTF is Y-up: east->X, up->Y, and north->-Z to keep a right-handed basis.
        local.append([e, u, -n])

    return local, [cx, cy, cz]


def compute_normals(positions: list[list[float]], indices: list[int]) -> list[list[float]]:
    """Flat per-triangle normals, written per vertex.

    Vertices are already duplicated per triangle, so a flat normal is correct and
    gives LOD1 boxes their crisp edges.
    """
    normals = [[0.0, 0.0, 0.0] for _ in positions]
    for i in range(0, len(indices) - 2, 3):
        a, b, c = indices[i], indices[i + 1], indices[i + 2]
        pa, pb, pc = positions[a], positions[b], positions[c]
        ux, uy, uz = pb[0] - pa[0], pb[1] - pa[1], pb[2] - pa[2]
        vx, vy, vz = pc[0] - pa[0], pc[1] - pa[1], pc[2] - pa[2]
        nx, ny, nz = uy * vz - uz * vy, uz * vx - ux * vz, ux * vy - uy * vx
        length = math.sqrt(nx * nx + ny * ny + nz * nz) or 1.0
        for index in (a, b, c):
            normals[index] = [nx / length, ny / length, nz / length]
    return normals


def write_glb(
    out_path: Path,
    positions: list[list[float]],
    indices: list[int],
    normals: list[list[float]],
    feature_ids: list[int],
    object_ids: list[str],
    properties: dict[str, list],
) -> None:
    """Assemble and write the .glb.

    Uses EXT_mesh_features + EXT_structural_metadata so Cesium can pick and style
    individual buildings — the same pair the already-published tiles use.
    """
    position_bytes = b"".join(struct.pack("<3f", *p) for p in positions)
    normal_bytes = b"".join(struct.pack("<3f", *n) for n in normals)
    index_bytes = b"".join(struct.pack("<I", i) for i in indices)
    feature_bytes = b"".join(struct.pack("<f", float(f)) for f in feature_ids)

    def pad(data: bytes) -> bytes:
        return data + b"\x00" * (-len(data) % 4)

    buffer_views = []
    accessors = []
    blob = b""

    def add_view(data: bytes, target: int | None = None) -> int:
        nonlocal blob
        blob = pad(blob)
        view = {"buffer": 0, "byteOffset": len(blob), "byteLength": len(data)}
        if target is not None:
            view["target"] = target
        buffer_views.append(view)
        blob += data
        return len(buffer_views) - 1

    min_pos = [min(p[i] for p in positions) for i in range(3)]
    max_pos = [max(p[i] for p in positions) for i in range(3)]

    accessors.append({
        "bufferView": add_view(position_bytes, 34962),
        "componentType": 5126, "count": len(positions), "type": "VEC3",
        "min": min_pos, "max": max_pos,
    })
    accessors.append({
        "bufferView": add_view(normal_bytes, 34962),
        "componentType": 5126, "count": len(normals), "type": "VEC3",
    })
    accessors.append({
        "bufferView": add_view(feature_bytes, 34962),
        "componentType": 5126, "count": len(feature_ids), "type": "SCALAR",
    })
    accessors.append({
        "bufferView": add_view(index_bytes, 34963),
        "componentType": 5125, "count": len(indices), "type": "SCALAR",
    })

    # Property tables are stored as string blobs plus offsets, per the spec.
    property_table = {}
    schema_properties = {}
    for name, values in properties.items():
        encoded = [str(v) if v is not None else "" for v in values]
        data = "".join(encoded).encode("utf-8")
        offsets = [0]
        for value in encoded:
            offsets.append(offsets[-1] + len(value.encode("utf-8")))
        offset_bytes = b"".join(struct.pack("<I", o) for o in offsets)
        property_table[name] = {
            "values": add_view(data),
            "stringOffsets": add_view(offset_bytes),
            "stringOffsetType": "UINT32",
        }
        schema_properties[name] = {"type": "STRING"}

    gltf = {
        "asset": {"version": "2.0", "generator": "cityjson-gltf"},
        "extensionsUsed": ["EXT_mesh_features", "EXT_structural_metadata"],
        "scene": 0,
        "scenes": [{"nodes": [0]}],
        "nodes": [{"mesh": 0}],
        "meshes": [{
            "primitives": [{
                "attributes": {"POSITION": 0, "NORMAL": 1, "_FEATURE_ID_0": 2},
                "indices": 3,
                "material": 0,
                "mode": 4,
                "extensions": {
                    "EXT_mesh_features": {
                        "featureIds": [{
                            "featureCount": len(object_ids),
                            "attribute": 0,
                            "propertyTable": 0,
                        }]
                    }
                },
            }]
        }],
        "materials": [{
            "pbrMetallicRoughness": {
                "baseColorFactor": [0.92, 0.92, 0.89, 1.0],
                "metallicFactor": 0.0,
                "roughnessFactor": 1.0,
            },
            "doubleSided": True,
        }],
        "buffers": [{"byteLength": len(pad(blob))}],
        "bufferViews": buffer_views,
        "accessors": accessors,
        "extensions": {
            "EXT_structural_metadata": {
                "schema": {
                    "id": "buildings",
                    "classes": {"building": {"name": "Building", "properties": schema_properties}},
                },
                "propertyTables": [{
                    "name": "buildings",
                    "class": "building",
                    "count": len(object_ids),
                    "properties": property_table,
                }],
            }
        },
    }

    blob = pad(blob)
    json_bytes = json.dumps(gltf, separators=(",", ":")).encode("utf-8")
    json_bytes += b" " * (-len(json_bytes) % 4)

    glb = struct.pack("<III", 0x46546C67, 2, 12 + 8 + len(json_bytes) + 8 + len(blob))
    glb += struct.pack("<II", len(json_bytes), 0x4E4F534A) + json_bytes
    glb += struct.pack("<II", len(blob), 0x004E4942) + blob

    out_path.write_bytes(glb)


def write_tileset(
    path: Path,
    glb_name: str,
    transform: list[float],
    positions: list[list[float]],
) -> None:
    """A minimal single-content 3D Tiles tileset around the .glb.

    Shaped so `publish_buildings.py` can pick it up: it reads the root
    `boundingVolume.box` to decide the geographic shard.
    """
    half = [
        (max(p[i] for p in positions) - min(p[i] for p in positions)) / 2 or 1.0
        for i in range(3)
    ]
    centre = [
        (max(p[i] for p in positions) + min(p[i] for p in positions)) / 2
        for i in range(3)
    ]
    geometric_error = max(half) * 2

    tileset = {
        "asset": {"version": "1.1"},
        "geometricError": geometric_error,
        "root": {
            "transform": transform,
            "boundingVolume": {
                "box": [
                    centre[0], centre[1], centre[2],
                    half[0], 0, 0,
                    0, half[1], 0,
                    0, 0, half[2],
                ]
            },
            "geometricError": 0.0,
            "refine": "REPLACE",
            "content": {"uri": glb_name},
        },
    }
    path.write_text(json.dumps(tileset, indent=1))


def main() -> int:
    parser = argparse.ArgumentParser(description="CityJSON to glTF (.glb)")
    parser.add_argument("input")
    parser.add_argument("output")
    parser.add_argument("--tileset")
    parser.add_argument("--srs-in", type=int, default=None)
    parser.add_argument(
        "--attributes",
        default="measuredHeight",
        help="comma-separated CityObject attributes to expose as glTF metadata",
    )
    args = parser.parse_args()

    cityjson = json.loads(Path(args.input).read_text())
    if cityjson.get("type") != "CityJSON":
        sys.exit("not a CityJSON file: {0}".format(args.input))

    src_epsg = args.srs_in or parse_reference_system(cityjson)
    if not src_epsg:
        sys.exit(
            "no CRS: the file declares no metadata.referenceSystem and "
            "--srs-in was not given"
        )

    vertices = dequantise(cityjson)
    if not vertices:
        sys.exit("the CityJSON has no vertices")

    positions, indices, feature_ids, object_ids = collect_geometry(cityjson, vertices)
    if not positions:
        sys.exit("no usable geometry found in the CityJSON")

    local, centre = to_local_y_up(positions, build_transformer(src_epsg))
    normals = compute_normals(local, indices)

    wanted = [a.strip() for a in args.attributes.split(",") if a.strip()]
    city_objects = cityjson.get("CityObjects") or {}
    properties = {"id": [object_id for object_id in object_ids]}
    for attribute in wanted:
        properties[attribute] = [
            (city_objects.get(object_id, {}).get("attributes") or {}).get(attribute)
            for object_id in object_ids
        ]

    out_path = Path(args.output)
    write_glb(out_path, local, indices, normals, feature_ids, object_ids, properties)

    if args.tileset:
        write_tileset(Path(args.tileset), out_path.name, enu_transform(*centre), local)

    print(
        "{0} objects, {1} triangles -> {2} ({3} KB)".format(
            len(object_ids), len(indices) // 3, out_path, out_path.stat().st_size // 1024
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
