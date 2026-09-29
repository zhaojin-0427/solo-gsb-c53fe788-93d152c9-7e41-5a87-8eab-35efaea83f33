"""Application-wide constants for the MVT compilation pipeline.

Tiles are built on the standard XYZ web map tiling scheme:

* geographic input ........... EPSG:4326 (lon/lat, GeoJSON)
* projected tiling surface ... EPSG:3857 (Web Mercator)
* tile local grid ............ 4096 x 4096 extent units (MVT specification)
* screen space ............... 256 x 256 CSS pixels per tile

One tile-local extent unit therefore equals 256 / 4096 = 1/16 of a pixel.
"""

from __future__ import annotations

#: Tiling zoom range supported by this service (inclusive).
MIN_ZOOM = 0
MAX_ZOOM = 14

#: MVT tile extent in tile-local units (the standard 4096 grid).
TILE_EXTENT = 4096

#: Tile side length in CSS pixels.
TILE_PIXELS = 256

#: Scale factor between tile-local extent units and CSS pixels (16 units/px).
UNITS_PER_PIXEL = TILE_EXTENT // TILE_PIXELS

#: Geometry clipping buffer, 8 pixels on every side -> 8 * 16 = 128 units.
BUFFER_PIXELS = 8
BUFFER_UNITS = BUFFER_PIXELS * UNITS_PER_PIXEL

#: Topology-preserving simplification tolerance (Douglas-Peucker), 0.5 px.
SIMPLIFY_TOLERANCE_UNITS = 0.5 * UNITS_PER_PIXEL

#: MVT content type as registered in the vector tile spec.
MVT_CONTENT_TYPE = "application/vnd.mapbox-vector-tile"

#: Web Mercator ellipsoid radius (spherical Mercator, EPSG:3857).
MERCATOR_RADIUS = 6378137.0

#: Half of the Web Mercator world edge in metres.
MERCATOR_MAX = MERCATOR_RADIUS * 3.14159265358979323846

#: Clipped latitude domain that projects to a finite Mercator y.
MAX_LATITUDE = 85.05112877980660

#: Maximum request body size accepted for a GeoJSON upload (16 MiB).
MAX_UPLOAD_BYTES = 16 * 1024 * 1024

#: SQLite feature id values must fit in the MVT (protobuf uint64) range.
MAX_FEATURE_ID = 2**63 - 1
