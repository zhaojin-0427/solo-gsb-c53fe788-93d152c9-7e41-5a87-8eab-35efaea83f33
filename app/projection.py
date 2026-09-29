"""EPSG:4326 lon/lat -> EPSG:3857 Web Mercator projection helpers."""

from __future__ import annotations

import math

from shapely.geometry.base import BaseGeometry
from shapely.ops import transform

from .constants import MERCATOR_MAX, MERCATOR_RADIUS


def lonlat_to_mercator(lon: float, lat: float) -> tuple[float, float]:
    """Project a single lon/lat point to spherical Web Mercator metres."""
    x = MERCATOR_RADIUS * math.radians(lon)
    y = MERCATOR_RADIUS * math.log(math.tan(math.pi / 4.0 + math.radians(lat) / 2.0))
    return x, y


def project_geometry(geom: BaseGeometry) -> BaseGeometry:
    """Project a Shapely geometry from EPSG:4326 to EPSG:3857."""
    return transform(lambda x, y, z=None: lonlat_to_mercator(x, y), geom)


def tile_meters(z: int) -> float:
    """Edge length of one tile at zoom ``z`` in Mercator metres."""
    return (2.0 * MERCATOR_MAX) / (2**z)


def tile_bounds_mercator(z: int, x: int, y: int) -> tuple[float, float, float, float]:
    """Return (minx, miny, maxx, maxy) of an XYZ tile in Mercator metres."""
    tile = tile_meters(z)
    minx = -MERCATOR_MAX + x * tile
    maxx = minx + tile
    # TMS y=0 is at the top (northern edge), so y runs downward.
    maxy = MERCATOR_MAX - y * tile
    miny = maxy - tile
    return minx, miny, maxx, maxy


def mercator_to_lonlat(x: float, y: float) -> tuple[float, float]:
    """Inverse projection used to derive cheap lon/lat pre-filter boxes."""
    lon = math.degrees(x / MERCATOR_RADIUS)
    lat = math.degrees(2.0 * math.atan(math.exp(y / MERCATOR_RADIUS)) - math.pi / 2.0)
    return lon, lat


def tile_bounds_lonlat(z: int, x: int, y: int) -> tuple[float, float, float, float]:
    """Return (min_lon, min_lat, max_lon, max_lat) of a tile in EPSG:4326."""
    minx, miny, maxx, maxy = tile_bounds_mercator(z, x, y)
    lon0, lat0 = mercator_to_lonlat(minx, miny)
    lon1, lat1 = mercator_to_lonlat(maxx, maxy)
    return lon0, lat0, lon1, lat1
