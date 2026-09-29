"""Unit tests for the projection and tile-geometry pipeline."""

from __future__ import annotations

import math

from shapely.geometry import LineString, Point, box

from app import tiler
from app.constants import (
    BUFFER_UNITS,
    SIMPLIFY_TOLERANCE_UNITS,
    TILE_EXTENT,
)
from app.geo import LINE_TYPES, POINT_TYPES, POLYGON_TYPES, prepare_geojson
from app.projection import (
    lonlat_to_mercator,
    project_geometry,
    tile_bounds_mercator,
)


def test_mercator_known_values():
    # Equator origin projects to the origin of the Mercator metre frame.
    x, y = lonlat_to_mercator(0.0, 0.0)
    assert x == 0.0 and math.isclose(y, 0.0, abs_tol=1e-9)
    x, y = lonlat_to_mercator(180.0, 0.0)
    assert math.isclose(x, 20037508.342789244, rel_tol=1e-9)
    assert math.isclose(y, 0.0, abs_tol=1e-9)


def test_tile_bounds_z0_cover_world():
    minx, miny, maxx, maxy = tile_bounds_mercator(0, 0, 0)
    assert math.isclose(minx, -20037508.342789244, abs_tol=1e-6)
    assert math.isclose(miny, -20037508.342789244, abs_tol=1e-6)
    assert math.isclose(maxx, 20037508.342789244, abs_tol=1e-6)
    assert math.isclose(maxy, 20037508.342789244, abs_tol=1e-6)


def test_projection_roundtrip_presolves_geometry():
    polygon = box(-1, -1, 1, 1)
    projected = project_geometry(polygon)
    assert projected.area > 0
    assert projected.geom_type == "Polygon"


def test_clip_box_is_8_pixel_buffer():
    assert BUFFER_UNITS == 128
    assert tiler.CLIP_BOX.bounds == (-128, -128, 4224, 4224)


def test_point_inside_buffer_is_retained():
    # Slightly outside the tile (x > 4096) but within the 128-unit buffer.
    point = Point(4096 + 100, 2048)
    kept = tiler._clip_and_normalize(point, POINT_TYPES)
    assert kept is not None and kept.geom_type == "Point"


def test_point_outside_buffer_is_dropped():
    point = Point(4096 + BUFFER_UNITS + 1, 2048)
    assert tiler._clip_and_normalize(point, POINT_TYPES) is None


def test_line_clipped_to_buffer_box():
    line = LineString([(-500, 2048), (5000, 2048)])
    clipped = tiler._clip_and_normalize(line, LINE_TYPES)
    assert clipped is not None
    minx, miny, maxx, maxy = clipped.bounds
    assert minx >= -BUFFER_UNITS and maxx <= TILE_EXTENT + BUFFER_UNITS


def test_simplification_tolerance_is_half_pixel():
    # 0.5 px == 8 extent units; tiny wobbles under that tolerance collapse.
    line = LineString([(0, 0), (100, 1), (200, 0), (300, 1), (400, 0)])
    simplified = tiler._simplify(line, LINE_TYPES)
    # Interior points only 1 unit off the straight baseline are removed.
    coords = list(simplified.coords)
    assert len(coords) < len(list(line.coords))
    assert coords[0] == (0, 0)
    assert coords[-1] == (400, 0)


def test_simplification_kept_when_spike_exceeds_tolerance():
    big_spike = LineString([(0, 0), (100, SIMPLIFY_TOLERANCE_UNITS * 2), (200, 0)])
    simplified = tiler._simplify(big_spike, LINE_TYPES)
    assert len(list(simplified.coords)) == 3


def test_quantize_snaps_to_integer_grid():
    point = Point(123.7, -4.4)
    snapped = tiler._quantize(point)
    assert (snapped.x, snapped.y) == (124, -4)


def test_polygon_family_filter_after_collection_result():
    # Intersection of an overlapping polygon can be a polygon directly.
    poly = box(2000, 2000, 5000, 5000)
    kept = tiler._clip_and_normalize(poly, POLYGON_TYPES)
    assert kept is not None
    assert kept.geom_type in {"Polygon", "MultiPolygon"}
    assert kept.bounds[2] <= TILE_EXTENT + BUFFER_UNITS


def test_z0_polygon_covering_world_compiles():
    name, records = prepare_geojson(
        {
            "type": "FeatureCollection",
            "features": [
                {
                    "type": "Feature",
                    "id": 1,
                    "properties": {},
                    "geometry": {
                        "type": "Polygon",
                        "coordinates": [[[-170, -80], [170, -80], [170, 80], [-170, 80], [-170, -80]]],
                    },
                }
            ],
        },
        "land",
    )
    tile, count = tiler.compile_tile(name, records, 0, 0, 0)
    assert count == 1 and len(tile) > 0


def test_compile_tile_deterministic_across_calls():
    _, records = prepare_geojson(
        {
            "type": "FeatureCollection",
            "features": [
                {"type": "Feature", "id": 2, "properties": {"k": "v"},
                 "geometry": {"type": "Point", "coordinates": [10, 10]}},
                {"type": "Feature", "id": 1, "properties": {"k": "w"},
                 "geometry": {"type": "LineString",
                              "coordinates": [[9, 9], [10, 10], [11, 11]]}},
            ],
        },
        "layer",
    )
    first, _ = tiler.compile_tile("layer", records, 2, 2, 2)
    second, _ = tiler.compile_tile("layer", records, 2, 2, 2)
    assert first == second
