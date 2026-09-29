"""SQLite persistence layer.

A single database file holds three groups of state:

* ``datasets``  - identity + pointer to the currently published revision
* ``revisions`` - immutable, append-only published versions
* ``features``   - the GeoJSON features captured by a revision (stored as WKB
  in the original EPSG:4326 coordinates and JSON properties)
* ``tiles``      - compiled MVT bytes, cached by (revision, z, x, y)

Revisions are never mutated; publishing creates a new revision row and, only
after it is fully written, flips ``datasets.latest_revision``. The same
transaction performs the ``expected_revision`` compare-and-swap so that a
lost update produces a 409 instead of silently overwriting a version.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Optional

from shapely import wkb

SCHEMA = """
CREATE TABLE IF NOT EXISTS datasets (
    id               TEXT PRIMARY KEY,
    name             TEXT NOT NULL,
    latest_revision  INTEGER NOT NULL,
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS revisions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    dataset_id    TEXT NOT NULL REFERENCES datasets(id),
    revision      INTEGER NOT NULL,
    layer_name    TEXT NOT NULL,
    feature_count INTEGER NOT NULL,
    published_at  TEXT NOT NULL,
    UNIQUE(dataset_id, revision)
);

CREATE TABLE IF NOT EXISTS features (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    dataset_id     TEXT NOT NULL,
    revision       INTEGER NOT NULL,
    feature_index  INTEGER NOT NULL,
    feature_id     INTEGER,
    str_feature_id TEXT,
    geometry_wkb   BLOB NOT NULL,
    properties     TEXT NOT NULL,
    minx           REAL NOT NULL,
    miny           REAL NOT NULL,
    maxx           REAL NOT NULL,
    maxy           REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_features_revision
    ON features(dataset_id, revision);

CREATE TABLE IF NOT EXISTS tiles (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    dataset_id    TEXT NOT NULL,
    revision      INTEGER NOT NULL,
    z             INTEGER NOT NULL,
    x             INTEGER NOT NULL,
    y             INTEGER NOT NULL,
    content       BLOB NOT NULL,
    content_hash  TEXT NOT NULL,
    is_empty      INTEGER NOT NULL DEFAULT 0,
    UNIQUE(dataset_id, revision, z, x, y)
);
"""

_init_lock = threading.Lock()
_initialized: set[str] = set()


def utcnow() -> str:
    """Canonical timestamp format used in all API responses.

    Microsecond precision keeps revisions published back-to-back in tests and
    batch jobs from colliding on the same timestamp.
    """
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def connect(db_path: str | Path) -> sqlite3.Connection:
    """Open a connection tuned for small concurrent writes."""
    conn = sqlite3.connect(str(db_path), timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def init_db(db_path: str | Path) -> None:
    """Create the schema on first use (idempotent)."""
    path = str(db_path)
    with _init_lock:
        if path in _initialized:
            return
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with connect(path) as conn:
            conn.executescript(SCHEMA)
        _initialized.add(path)


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Serialize writers up front so the revision CAS is race-free.

    ``BEGIN IMMEDIATE`` acquires the SQLite reserved lock immediately; a
    competing publish waits on ``busy_timeout`` and then performs the
    ``expected_revision`` check against the freshly committed state.
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except Exception:
        conn.rollback()
        raise
    else:
        conn.commit()


def get_dataset(conn: sqlite3.Connection, dataset_id: str) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM datasets WHERE id = ?", (dataset_id,)
    ).fetchone()


def get_revision(
    conn: sqlite3.Connection, dataset_id: str, revision: int
) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM revisions WHERE dataset_id = ? AND revision = ?",
        (dataset_id, revision),
    ).fetchone()


def list_datasets(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(conn.execute("SELECT * FROM datasets ORDER BY id"))


def list_revisions(conn: sqlite3.Connection, dataset_id: str) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            "SELECT * FROM revisions WHERE dataset_id = ? ORDER BY revision",
            (dataset_id,),
        )
    )


def iter_revision_features(
    conn: sqlite3.Connection, dataset_id: str, revision: int
) -> Iterator[dict]:
    """Yield a revision's features ordered by their stable insertion index."""
    rows = conn.execute(
        """
        SELECT feature_index, feature_id, str_feature_id, geometry_wkb, properties,
               minx, miny, maxx, maxy
          FROM features
         WHERE dataset_id = ? AND revision = ?
         ORDER BY feature_index
        """,
        (dataset_id, revision),
    )
    for row in rows:
        yield {
            "index": row["feature_index"],
            "id": row["feature_id"],
            "str_id": row["str_feature_id"],
            "geometry": wkb.loads(row["geometry_wkb"]),
            "properties": json.loads(row["properties"]),
            "bounds": (row["minx"], row["miny"], row["maxx"], row["maxy"]),
        }


def get_tile(
    conn: sqlite3.Connection, dataset_id: str, revision: int, z: int, x: int, y: int
) -> Optional[sqlite3.Row]:
    return conn.execute(
        """
        SELECT content, content_hash, is_empty FROM tiles
         WHERE dataset_id = ? AND revision = ? AND z = ? AND x = ? AND y = ?
        """,
        (dataset_id, revision, z, x, y),
    ).fetchone()


def put_tile(
    conn: sqlite3.Connection,
    dataset_id: str,
    revision: int,
    z: int,
    x: int,
    y: int,
    content: bytes,
    content_hash: str,
    is_empty: bool,
) -> None:
    """Persist a compiled tile.

    The unique index plus ``OR IGNORE`` makes a racing duplicate compile a
    no-op; the bytes are deterministic anyway so either row is identical.
    """
    conn.execute(
        """
        INSERT OR IGNORE INTO tiles
            (dataset_id, revision, z, x, y, content, content_hash, is_empty)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (dataset_id, revision, z, x, y, content, content_hash, 1 if is_empty else 0),
    )
