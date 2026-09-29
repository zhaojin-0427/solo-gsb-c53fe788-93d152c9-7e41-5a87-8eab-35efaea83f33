"""Business logic: publishing immutable revisions and serving tiles."""

from __future__ import annotations

import hashlib
import json
import uuid
from pathlib import Path
from typing import Any, Optional

from shapely import wkb

from . import db
from .constants import MAX_ZOOM, MIN_ZOOM, MVT_CONTENT_TYPE
from .errors import ApiError, bad_request, conflict, not_found
from .geo import prepare_geojson
from .tiler import compile_tile


def _dataset_payload(row: dict[str, Any], revision: int | None = None) -> dict[str, Any]:
    return {
        "id": row["id"],
        "name": row["name"],
        "latest_revision": row["latest_revision"],
        "revision": revision if revision is not None else row["latest_revision"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def publish_revision(
    db_path: str | Path,
    raw_body: bytes,
    *,
    dataset_id: str | None,
    layer_name: str | None,
    expected_revision: Optional[int],
    content_type: str | None,
    filename: str | None = None,
) -> dict[str, Any]:
    """Create a dataset or append an immutable revision.

    The whole operation (revision compare-and-swap, feature insert, latest
    pointer flip) happens in one ``BEGIN IMMEDIATE`` transaction.
    """
    payload = _parse_geojson_body(raw_body, content_type, filename)
    name, records = prepare_geojson(payload, layer_name)

    conn = db.connect(db_path)
    try:
        with db.transaction(conn):
            existing = db.get_dataset(conn, dataset_id) if dataset_id else None
            if dataset_id is not None and existing is None:
                raise not_found(f"dataset '{dataset_id}' does not exist")

            if existing is None:
                if expected_revision is not None:
                    raise bad_request(
                        "expected_revision is not allowed when creating a dataset"
                    )
                new_id = uuid.uuid4().hex
                now = db.utcnow()
                next_revision = 1
                conn.execute(
                    "INSERT INTO datasets (id, name, latest_revision, created_at, updated_at)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (new_id, name, next_revision, now, now),
                )
                dataset_row = db.get_dataset(conn, new_id)
            else:
                current = existing["latest_revision"]
                if expected_revision is not None and expected_revision != current:
                    raise conflict(
                        f"revision conflict: expected_revision={expected_revision} "
                        f"but current revision is {current}"
                    )
                next_revision = current + 1
                conn.execute(
                    "UPDATE datasets SET name = ?, latest_revision = ?, updated_at = ?"
                    " WHERE id = ?",
                    (name, next_revision, db.utcnow(), existing["id"]),
                )
                dataset_row = db.get_dataset(conn, existing["id"])

            conn.execute(
                "INSERT INTO revisions (dataset_id, revision, layer_name, feature_count, published_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (
                    dataset_row["id"],
                    next_revision,
                    name,
                    len(records),
                    db.utcnow(),
                ),
            )
            conn.executemany(
                """
                INSERT INTO features
                    (dataset_id, revision, feature_index, feature_id,
                     str_feature_id, geometry_wkb, properties,
                     minx, miny, maxx, maxy)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        dataset_row["id"],
                        next_revision,
                        record["index"],
                        record["id"],
                        record["str_id"],
                        wkb.dumps(record["geometry"]),
                        json.dumps(record["properties"], sort_keys=True, ensure_ascii=False),
                        *record["bounds"],
                    )
                    for record in records
                ],
            )

        return {
            "dataset": _dataset_payload(dataset_row, next_revision),
            "layer_name": name,
            "feature_count": len(records),
        }
    finally:
        conn.close()


def _parse_geojson_body(
    raw: bytes, content_type: str | None, filename: str | None
) -> Any:
    if not raw:
        raise bad_request("request body is empty; send a GeoJSON FeatureCollection")
    # Multipart uploads arrive already extracted by FastAPI; their bytes are
    # raw JSON regardless of the outer Content-Type.
    ctype = (content_type or "").split(";")[0].strip().lower()
    if ctype and ctype not in ("application/json", "application/geo+json", "text/plain"):
        raise ApiError(
            415,
            "unsupported_media_type",
            f"content type '{ctype}' is not supported; send application/geo+json, "
            "application/json, or multipart/form-data with a 'file' part",
        )
    if filename and not filename.lower().endswith((".json", ".geojson")):
        raise bad_request("uploaded file must have a .geojson or .json extension")
    try:
        return json.loads(raw.decode("utf-8"))
    except UnicodeDecodeError:
        raise bad_request("request body is not valid UTF-8 text")
    except json.JSONDecodeError as exc:
        raise bad_request(f"request body is not valid JSON: {exc.msg}")


def _resolve_revision(conn, dataset_id: str, revision: int | None):
    dataset = db.get_dataset(conn, dataset_id)
    if dataset is None:
        raise not_found(f"dataset '{dataset_id}' does not exist")
    rev_no = revision if revision is not None else dataset["latest_revision"]
    row = db.get_revision(conn, dataset_id, rev_no)
    if row is None:
        raise not_found(f"revision {rev_no} of dataset '{dataset_id}' does not exist")
    return dataset, row


def get_dataset_view(db_path: str | Path, dataset_id: str) -> dict[str, Any]:
    conn = db.connect(db_path)
    try:
        dataset = db.get_dataset(conn, dataset_id)
        if dataset is None:
            raise not_found(f"dataset '{dataset_id}' does not exist")
        revisions = [
            {
                "revision": r["revision"],
                "layer_name": r["layer_name"],
                "feature_count": r["feature_count"],
                "published_at": r["published_at"],
            }
            for r in db.list_revisions(conn, dataset_id)
        ]
        return {**_dataset_payload(dataset), "revisions": revisions}
    finally:
        conn.close()


def list_datasets_view(db_path: str | Path) -> list[dict[str, Any]]:
    conn = db.connect(db_path)
    try:
        return [_dataset_payload(r) for r in db.list_datasets(conn)]
    finally:
        conn.close()


def get_revision_view(db_path: str | Path, dataset_id: str, revision: int) -> dict[str, Any]:
    conn = db.connect(db_path)
    try:
        dataset, row = _resolve_revision(conn, dataset_id, revision)
        return {
            "dataset_id": dataset["id"],
            "revision": row["revision"],
            "layer_name": row["layer_name"],
            "feature_count": row["feature_count"],
            "published_at": row["published_at"],
        }
    finally:
        conn.close()


def get_tile_response(
    db_path: str | Path,
    dataset_id: str,
    revision: int | None,
    z: int,
    x: int,
    y: int,
) -> dict[str, Any]:
    """Return tile bytes + metadata, compiling and caching on cache miss."""
    if not (MIN_ZOOM <= z <= MAX_ZOOM):
        raise bad_request(f"zoom level z must be between {MIN_ZOOM} and {MAX_ZOOM}")
    max_index = 2**z - 1
    if not (0 <= x <= max_index and 0 <= y <= max_index):
        raise bad_request(
            f"tile coordinates x and y must satisfy 0 <= x,y <= {max_index} at z={z}"
        )

    conn = db.connect(db_path)
    try:
        dataset, rev_row = _resolve_revision(conn, dataset_id, revision)
        rev_no = rev_row["revision"]

        cached = db.get_tile(conn, dataset["id"], rev_no, z, x, y)
        if cached is not None:
            content = bytes(cached["content"])
            content_hash = cached["content_hash"]
            is_empty = bool(cached["is_empty"])
        else:
            features = list(
                db.iter_revision_features(conn, dataset["id"], rev_no)
            )
            content, emitted = compile_tile(rev_row["layer_name"], features, z, x, y)
            is_empty = emitted == 0
            content_hash = hashlib.sha256(content).hexdigest()
            with db.transaction(conn):
                db.put_tile(
                    conn, dataset["id"], rev_no, z, x, y,
                    content, content_hash, is_empty,
                )

        pinned = revision is not None
        cache_control = (
            "public, max-age=31536000, immutable"
            if pinned
            else "public, max-age=3600"
        )
        return {
            "content": content,
            "etag": f'"{content_hash}"',
            "content_type": MVT_CONTENT_TYPE,
            "cache_control": cache_control,
            "revision": rev_no,
            "layer_name": rev_row["layer_name"],
            "is_empty": is_empty,
        }
    finally:
        conn.close()
