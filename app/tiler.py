"""Deterministic vector tile compilation.

Pipeline for one tile (z, x, y) of one immutable revision:

1. Convert every candidate feature from EPSG:4326 to EPSG:3857 metres.
2. Skip features whose bounding box cannot touch the (buffered) tile.
3. Transform metres into tile-local extent units (4096 grid) with a
   128-unit (8 px) buffer on every side.
4. Clip to the buffered box, repair any geometry made degenerate by clipping.
5. Topology-preserving simplification with a 0.5 px tolerance.
6. Quantize coordinates to integers on the 4096 grid.
7. Encode with a fixed feature order (layer name, feature id) so identical
   inputs always serialize to byte-identical output.
"""

from __future__ import annotations

from typing import Any, Iterable

import mapbox_vector_tile
from shapely import make_valid
from shapely.geometry import (
    MultiLineString,
    MultiPoint,
    MultiPolygon,
    Polygon,
    box,
)
from shapely.geometry.base import BaseGeometry
from shapely.ops import transform, unary_union

from .constants import (
    BUFFER_UNITS,
    MERCATOR_MAX,
    SIMPLIFY_TOLERANCE_UNITS,
    TILE_EXTENT,
)
from .geo import LINE_TYPES, POINT_TYPES, POLYGON_TYPES
from .projection import (
    mercator_to_lonlat,
    project_geometry,
    tile_bounds_mercator,
    tile_meters,
)

#: Buffered clip rectangle in tile-local extent units.
CLIP_BOX = box(
    -BUFFER_UNITS, -BUFFER_UNITS, TILE_EXTENT + BUFFER_UNITS, TILE_EXTENT + BUFFER_UNITS
)


def _to_extent_coordinates(z: int, x: int, y: int):
    """Build a Shapely ``transform`` mapping Mercator metres -> extent units."""
    tile = tile_meters(z)
    scale = TILE_EXTENT / tile
    origin_x = -MERCATOR_MAX + x * tile
    origin_y = MERCATOR_MAX - y * tile  # northern edge is the top of tile row y

    def fn(mx: float, my: float, zcoord: float | None = None):
        return (mx - origin_x) * scale, (origin_y - my) * scale

    return fn


def _filter_dimensions(geom: BaseGeometry, keep_types: set[str]) -> BaseGeometry | None:
    """Extract one geometry family from a clipping/make_valid result.

    Clipping a polygon can yield a GeometryCollection holding polygons, lines
    and points; keep only the dimensional family MVT expects for the feature
    and merge touching polygonal parts back together.
    """
    if geom.is_empty:
        return None
    if geom.geom_type in keep_types:
        return geom
    if geom.geom_type != "GeometryCollection":
        return None
    parts = [g for g in geom.geoms if not g.is_empty and g.geom_type in keep_types]
    if not parts:
        return None
    if keep_types is POLYGON_TYPES:
        merged = unary_union(parts)
        return None if merged.is_empty else merged
    if keep_types is LINE_TYPES:
        return parts[0] if len(parts) == 1 else MultiLineString(parts)
    return parts[0] if len(parts) == 1 else MultiPoint(parts)


def _clip_and_normalize(geom: BaseGeometry, keep_types: set[str]) -> BaseGeometry | None:
    """Clip to the buffered box, keeping only the requested geometry family."""
    clipped = geom.intersection(CLIP_BOX)
    if not clipped.is_valid:
        clipped = make_valid(clipped)
    return _filter_dimensions(clipped, keep_types)


def _simplify(geom: BaseGeometry, keep_types: set[str]) -> BaseGeometry | None:
    """Topology-preserving Douglas-Peucker simplification (``preserve=True``).

    Points are untouched. A line may collapse to a point after simplifying and
    a polygon ring may lose vertices; those dimensionality changes are dropped
    because they cannot be encoded as the feature's original geometry type.
    """
    if geom.geom_type in POINT_TYPES:
        return geom
    simplified = geom.simplify(SIMPLIFY_TOLERANCE_UNITS, preserve_topology=True)
    if not simplified.is_valid:
        simplified = make_valid(simplified)
    simplified = _filter_dimensions(simplified, keep_types)
    if simplified is None:
        return None
    if keep_types is POLYGON_TYPES:
        if isinstance(simplified, Polygon):
            if len(simplified.exterior.coords) < 4 or simplified.area == 0:
                return None
        elif isinstance(simplified, MultiPolygon):
            kept = [
                p
                for p in simplified.geoms
                if len(p.exterior.coords) >= 4 and p.area > 0
            ]
            if not kept:
                return None
            simplified = kept[0] if len(kept) == 1 else MultiPolygon(kept)
    return simplified


def _quantize(geom: BaseGeometry) -> BaseGeometry:
    """Snap every coordinate to an integer cell of the 4096 grid."""
    return transform(lambda x, y, z=None: (int(round(x)), int(round(y))), geom)


def _sort_key(feature: dict[str, Any]) -> tuple:
    """Total, stable ordering of features inside one tile layer.

    Integer ids first (numeric), then string ids (lexicographic), then
    id-less features in their original publish order. The original publish
    index is the final tiebreaker so ordering never depends on dict hashing.
    """
    int_id = feature["_int_id"]
    str_id = feature["_str_id"]
    if int_id is not None:
        return (0, int_id, 0, "", feature["_index"])
    if str_id is not None:
        return (1, 0, 0, str_id, feature["_index"])
    return (2, 0, 0, "", feature["_index"])


def _to_encoder_feature(feature: dict[str, Any]) -> dict[str, Any]:
    """Strip the internal ordering metadata before handing off to MVT."""
    out = {"geometry": feature["geometry"], "properties": feature["properties"]}
    if feature["_int_id"] is not None:
        out["id"] = feature["_int_id"]
    return out


def _encode(layer_name: str, features: list[dict[str, Any]]) -> bytes:
    return mapbox_vector_tile.encode(
        [{"name": layer_name, "features": features}],
        default_options={
            "extents": TILE_EXTENT,
            "y_coord_down": False,  # encoder flips the Y axis itself
            "on_invalid_geometry": mapbox_vector_tile.encoder.on_invalid_geometry_raise,
            "check_winding_order": True,
        },
    )


def _encode_with_isolation(layer_name: str, features: list[dict[str, Any]]) -> bytes:
    """Encode, and as a safety net drop a single pathological feature.

    Revisions are validated at publish time, so reaching the fallback means
    clipping/quantization produced an encoder-rejected geometry; skip that
    one feature rather than failing the whole tile.
    """
    try:
        return _encode(layer_name, features)
    except Exception:
        survivors: list[dict[str, Any]] = []
        for feature in features:
            trial = survivors + [feature]
            try:
                _encode(layer_name, trial)
            except Exception:
                continue
            survivors.append(feature)
        return _encode(layer_name, survivors)


def compile_tile(
    layer_name: str,
    features: Iterable[dict[str, Any]],
    z: int,
    x: int,
    y: int,
) -> tuple[bytes, int]:
    """Compile one MVT, returning ``(bytes, emitted_feature_count)``.

    A tile with zero emitted features serializes to a 16-byte protobuf that
    still carries the layer metadata; callers distinguish "empty" from
    "non-empty" by the feature count rather than by byte length.
    """
    tile = tile_bounds_mercator(z, x, y)
    buffer_m = BUFFER_UNITS / TILE_EXTENT * tile_meters(z)
    bbox_m = (
        tile[0] - buffer_m,
        tile[1] - buffer_m,
        tile[2] + buffer_m,
        tile[3] + buffer_m,
    )
    # Lon/lat padding for the cheap bbox pre-filter is derived by inverting
    # the padded Mercator box (its latitude padding is slightly asymmetric).
    lon0, lat0 = mercator_to_lonlat(bbox_m[0], bbox_m[1])
    lon1, lat1 = mercator_to_lonlat(bbox_m[2], bbox_m[3])
    bbox_ll = (lon0, lat0, lon1, lat1)

    to_extent = _to_extent_coordinates(z, x, y)
    compiled: list[dict[str, Any]] = []

    for record in features:
        minx, miny, maxx, maxy = record["bounds"]
        if maxx < bbox_ll[0] or minx > bbox_ll[2] or maxy < bbox_ll[1] or miny > bbox_ll[3]:
            continue

        source_geom = record["geometry"]
        if source_geom.geom_type in POINT_TYPES:
            keep = POINT_TYPES
        elif source_geom.geom_type in LINE_TYPES:
            keep = LINE_TYPES
        else:
            keep = POLYGON_TYPES

        geom = transform(to_extent, project_geometry(source_geom))
        geom = _clip_and_normalize(geom, keep)
        if geom is None:
            continue
        geom = _simplify(geom, keep)
        if geom is None or geom.is_empty:
            continue
        geom = _quantize(geom)

        properties = dict(record["properties"])
        # String ids cannot live in the MVT uint64 id field; expose them via
        # a well-known property while keeping the ordering deterministic.
        if record["id"] is None and record["str_id"] is not None:
            properties = {"feature_id": record["str_id"], **properties}

        compiled.append(
            {
                "geometry": geom,
                "properties": properties,
                "_int_id": record["id"],
                "_str_id": record["str_id"],
                "_index": record["index"],
            }
        )

    compiled.sort(key=_sort_key)
    encoded_features = [_to_encoder_feature(f) for f in compiled]
    payload = _encode_with_isolation(layer_name, encoded_features)
    return payload, len(compiled)
