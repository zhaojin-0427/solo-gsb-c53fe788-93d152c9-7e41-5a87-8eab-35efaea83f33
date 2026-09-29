"""Parsing and validation of published EPSG:4326 GeoJSON layers.

Validation is deliberately strict: everything that cannot be turned into a
well-defined MVT feature is rejected with HTTP 400 at publish time rather
than failing later, lazily, during tile compilation.
"""

from __future__ import annotations

import math
import re
from typing import Any, Iterator

from shapely import force_2d
from shapely.geometry import shape as shapely_shape
from shapely.geometry.base import BaseGeometry

from .constants import MAX_FEATURE_ID, MAX_LATITUDE
from .errors import bad_request

#: Geometry types that map onto the three MVT geometry encodings.
POINT_TYPES = {"Point", "MultiPoint"}
LINE_TYPES = {"LineString", "MultiLineString"}
POLYGON_TYPES = {"Polygon", "MultiPolygon"}
SUPPORTED_TYPES = POINT_TYPES | LINE_TYPES | POLYGON_TYPES

_LAYER_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_\-.:]{0,127}$")
DEFAULT_LAYER_NAME = "default"


def validate_layer_name(name: str) -> str:
    if not isinstance(name, str) or not _LAYER_NAME_RE.match(name):
        raise bad_request(
            "layer name must start with a letter or underscore and contain "
            "only letters, digits, '_', '-', '.' or ':' (max 128 chars)"
        )
    return name


def _walk_coordinates(coords: Any) -> Iterator[tuple[float, float]]:
    """Yield every (lon, lat) pair of a GeoJSON coordinates element."""
    if (
        isinstance(coords, (list, tuple))
        and len(coords) >= 2
        and not isinstance(coords[0], (list, tuple))
    ):
        yield coords[0], coords[1]
        return
    for element in coords:
        yield from _walk_coordinates(element)


def _validate_coordinate_domain(geometry: dict) -> None:
    if geometry.get("coordinates") is None:
        raise bad_request("feature geometry is missing 'coordinates'")
    count = 0
    for lon, lat in _walk_coordinates(geometry["coordinates"]):
        count += 1
        if not (isinstance(lon, (int, float)) and isinstance(lat, (int, float))):
            raise bad_request("geometry coordinates must be numbers")
        if not (math.isfinite(lon) and math.isfinite(lat)):
            raise bad_request("geometry coordinates must be finite numbers")
        if not (-180.0 <= lon <= 180.0):
            raise bad_request(f"longitude {lon} is outside [-180, 180]")
        if not (-MAX_LATITUDE <= lat <= MAX_LATITUDE):
            raise bad_request(
                f"latitude {lat} is outside the Web Mercator domain "
                f"[-{MAX_LATITUDE:.3f}, {MAX_LATITUDE:.3f}]"
            )
    if count == 0:
        raise bad_request("feature geometry has no coordinates")


def _validate_properties(raw: Any) -> dict[str, Any]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise bad_request("feature 'properties' must be an object")
    clean: dict[str, Any] = {}
    for key, value in raw.items():
        if not isinstance(key, str):
            raise bad_request("feature property keys must be strings")
        # MVT values are string/int/float/bool only; JSON null is dropped.
        if value is None:
            continue
        if isinstance(value, bool):
            clean[key] = value
        elif isinstance(value, int):
            if not (-(2**63) <= value <= MAX_FEATURE_ID):
                raise bad_request(f"property '{key}' integer value is out of range")
            clean[key] = value
        elif isinstance(value, float):
            if not math.isfinite(value):
                raise bad_request(f"property '{key}' value must be finite")
            clean[key] = value
        elif isinstance(value, str):
            clean[key] = value
        else:
            raise bad_request(
                f"property '{key}' has an unsupported type "
                f"{type(value).__name__}; only primitive values are allowed"
            )
    return clean


def _validate_feature_id(raw: Any, index: int) -> tuple[int | None, str | None]:
    if raw is None:
        return None, None
    if isinstance(raw, bool):
        raise bad_request(f"feature at index {index} has an invalid boolean id")
    if isinstance(raw, int):
        if raw < 0:
            raise bad_request(f"feature at index {index} has a negative id")
        if raw > MAX_FEATURE_ID:
            raise bad_request(f"feature at index {index} id is larger than uint64")
        return raw, None
    if isinstance(raw, str):
        if len(raw) > 512:
            raise bad_request(f"feature at index {index} string id is too long")
        return None, raw
    raise bad_request(
        f"feature at index {index} id must be a non-negative integer or string"
    )


def _build_geometry(geometry: dict, index: int) -> BaseGeometry:
    gtype = geometry.get("type")
    if not isinstance(gtype, str):
        raise bad_request(f"feature at index {index} geometry is missing a type")
    if gtype == "GeometryCollection":
        raise bad_request(
            f"feature at index {index}: GeometryCollection is not supported "
            "in vector tiles; explode it into separate features"
        )
    if gtype not in SUPPORTED_TYPES:
        raise bad_request(f"feature at index {index}: geometry type '{gtype}' is invalid")
    _validate_coordinate_domain(geometry)
    try:
        geom = shapely_shape(geometry)
    except Exception as exc:  # malformed rings/coordinate structures
        raise bad_request(f"feature at index {index} geometry is malformed: {exc}")
    geom = force_2d(geom)
    if geom.is_empty:
        raise bad_request(f"feature at index {index} has an empty geometry")
    if not geom.is_valid:
        raise bad_request(
            f"feature at index {index} geometry is topologically invalid "
            "(self-intersecting rings, duplicate points, ...); repair it "
            "before publishing"
        )
    return geom


def _features(payload: dict) -> list[dict]:
    doc_type = payload.get("type")
    if doc_type == "FeatureCollection":
        features = payload.get("features")
        if not isinstance(features, list):
            raise bad_request("FeatureCollection 'features' must be a list")
        return features
    if doc_type == "Feature":
        return [payload]
    if doc_type in SUPPORTED_TYPES:
        raise bad_request(
            "publishing a bare geometry is not supported; wrap it in a Feature"
        )
    raise bad_request("request body must be a GeoJSON FeatureCollection or Feature")


def prepare_geojson(
    payload: Any, layer_name: str | None
) -> tuple[str, list[dict[str, Any]]]:
    """Validate a parsed GeoJSON document and return records ready to store.

    Each record contains the Shapely geometry in EPSG:4326, the cleaned
    properties and the normalized feature id.
    """
    if not isinstance(payload, dict):
        raise bad_request("GeoJSON document must be a JSON object")
    name = validate_layer_name(layer_name or DEFAULT_LAYER_NAME)
    records: list[dict[str, Any]] = []
    for index, feature in enumerate(_features(payload)):
        if not isinstance(feature, dict) or feature.get("type") != "Feature":
            raise bad_request(f"element at index {index} is not a GeoJSON Feature")
        geometry = feature.get("geometry")
        if not isinstance(geometry, dict):
            raise bad_request(f"feature at index {index} is missing a valid geometry")
        int_id, str_id = _validate_feature_id(feature.get("id"), index)
        geom = _build_geometry(geometry, index)
        records.append(
            {
                "index": index,
                "id": int_id,
                "str_id": str_id,
                "geometry": geom,
                "properties": _validate_properties(feature.get("properties")),
                "bounds": geom.bounds,
            }
        )
    return name, records
