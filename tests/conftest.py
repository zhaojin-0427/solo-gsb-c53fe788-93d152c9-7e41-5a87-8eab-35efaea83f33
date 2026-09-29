"""Shared pytest fixtures."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.main import create_app  # noqa: E402


@pytest.fixture()
def client(tmp_path):
    db_path = tmp_path / "tiles.db"
    app = create_app(str(db_path))
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def published(client):
    """Publish a mixed point/line/polygon dataset and return its id."""
    geojson = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "id": 40,
                "properties": {"name": "forty", "kind": 1},
                "geometry": {"type": "Point", "coordinates": [0.0, 0.0]},
            },
            {
                "type": "Feature",
                "id": 2,
                "properties": {"name": "two"},
                "geometry": {
                    "type": "LineString",
                    "coordinates": [[-0.01, 0.0], [0.0, 0.001], [0.01, 0.0]],
                },
            },
            {
                "type": "Feature",
                "id": 7,
                "properties": {"name": "seven"},
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [
                        [[-0.02, -0.02], [0.02, -0.02], [0.02, 0.02], [-0.02, 0.02], [-0.02, -0.02]]
                    ],
                },
            },
        ],
    }
    r = client.post("/datasets", json=geojson)
    assert r.status_code == 201
    return r.json()["dataset"]["id"]
