"""Tile fetching: parameters, ETags, determinism and caching."""

from __future__ import annotations

import mapbox_vector_tile

TILE_URL = "/datasets/{ds}/tiles/{z}/{x}/{y}.mvt"


def fetch(client, ds, z, x, y, **kw):
    return client.get(TILE_URL.format(ds=ds, z=z, x=x, y=y), **kw)


def test_tile_200_with_mvt_content_type_and_etag(client, published):
    r = fetch(client, published, 0, 0, 0)
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/vnd.mapbox-vector-tile"
    etag = r.headers["etag"]
    assert etag.startswith('"') and etag.endswith('"')
    assert len(etag) == 66  # " + 64 hex sha256 + "
    # The layer advertises the 4096 extent.
    decoded = mapbox_vector_tile.decode(r.content)
    assert decoded["default"]["extent"] == 4096


def test_repeated_requests_byte_identical(client, published):
    r1 = fetch(client, published, 0, 0, 0)
    r2 = fetch(client, published, 0, 0, 0)
    assert r1.content == r2.content
    assert r1.headers["etag"] == r2.headers["etag"]


def test_if_none_match_returns_304(client, published):
    first = fetch(client, published, 0, 0, 0)
    second = fetch(client, published, 0, 0, 0, headers={"If-None-Match": first.headers["etag"]})
    assert second.status_code == 304
    assert second.content == b""
    assert second.headers["etag"] == first.headers["etag"]


def test_if_none_match_star(client, published):
    r = fetch(client, published, 0, 0, 0, headers={"If-None-Match": "*"})
    assert r.status_code == 304


def test_non_matching_if_none_match_returns_full_body(client, published):
    r = fetch(client, published, 0, 0, 0, headers={"If-None-Match": '"deadbeef"'})
    assert r.status_code == 200
    assert len(r.content) > 0


def test_empty_tile_is_204_and_conditional_304(client, published):
    # Nothing near the Aleutian area of this high-zoom tile.
    first = fetch(client, published, 14, 0, 0)
    assert first.status_code == 204
    assert first.content == b""
    assert first.headers.get("etag")
    second = fetch(client, published, 14, 0, 0,
                   headers={"If-None-Match": first.headers["etag"]})
    assert second.status_code == 304


def test_zoom_above_14_is_400(client, published):
    assert fetch(client, published, 15, 0, 0).status_code == 400


def test_negative_zoom_returns_400(client, published):
    assert client.get(f"/datasets/{published}/tiles/-1/0/0.mvt").status_code == 400


def test_xy_out_of_range_is_400(client, published):
    assert fetch(client, published, 2, 4, 0).status_code == 400
    assert fetch(client, published, 2, 0, 4).status_code == 400
    assert fetch(client, published, 2, -1, 0).status_code in (400, 404)


def test_non_integer_zoom_is_400(client, published):
    assert client.get(f"/datasets/{published}/tiles/x/0/0.mvt").status_code == 400


def test_tile_for_unknown_dataset_404(client):
    assert fetch(client, "nope", 0, 0, 0).status_code == 404


def test_features_sorted_by_id(client):
    # Deliberately publish ids out of order with overlapping z=0 footprint.
    geojson = {
        "type": "FeatureCollection",
        "features": [
            {"type": "Feature", "id": 30, "properties": {},
             "geometry": {"type": "Point", "coordinates": [0, 0]}},
            {"type": "Feature", "id": 5, "properties": {},
             "geometry": {"type": "Point", "coordinates": [1, 1]}},
            {"type": "Feature", "id": 12, "properties": {},
             "geometry": {"type": "Point", "coordinates": [-1, -1]}},
        ],
    }
    ds = client.post("/datasets", json=geojson).json()["dataset"]["id"]
    decoded = mapbox_vector_tile.decode(fetch(client, ds, 0, 0, 0).content)
    ids = [f["id"] for f in decoded["default"]["features"]]
    assert ids == sorted(ids) == [5, 12, 30]


def test_separate_datasets_with_same_input_produce_identical_bytes(client):
    """Determinism must not depend on database-assigned row ids."""

    def make():
        geojson = {
            "type": "FeatureCollection",
            "features": [
                {"type": "Feature", "id": 2, "properties": {"b": 1, "a": "x"},
                 "geometry": {"type": "Point", "coordinates": [0.1, 0.1]}},
                {"type": "Feature", "id": 1, "properties": {"a": "y", "b": 2},
                 "geometry": {"type": "Point", "coordinates": [0.2, 0.2]}},
            ],
        }
        ds = client.post("/datasets", json=geojson).json()["dataset"]["id"]
        return fetch(client, ds, 0, 0, 0).content

    assert make() == make()


def test_pinned_revision_immutable(client, published):
    rev1_tile = client.get(f"/datasets/{published}/revisions/1/tiles/0/0/0.mvt")
    client.post(f"/datasets/{published}?expected_revision=1",
                json={"type": "FeatureCollection", "features": []})
    rev1_again = client.get(f"/datasets/{published}/revisions/1/tiles/0/0/0.mvt")
    assert rev1_tile.content == rev1_again.content
    # Latest now points at the empty revision -> empty tile.
    latest = fetch(client, published, 0, 0, 0)
    assert latest.status_code == 204
    assert latest.headers["x-revision"] == "2"


def test_pinned_missing_revision_404(client, published):
    r = client.get(f"/datasets/{published}/revisions/99/tiles/0/0/0.mvt")
    assert r.status_code == 404


def test_pinned_tile_is_immutable_cache(client, published):
    r = client.get(f"/datasets/{published}/revisions/1/tiles/0/0/0.mvt")
    assert "immutable" in r.headers["cache-control"]


def test_tile_coordinates_are_on_4096_grid(client):
    geojson = {
        "type": "FeatureCollection",
        "features": [
            {"type": "Feature", "id": 1, "properties": {},
             "geometry": {"type": "Point", "coordinates": [0.0, 0.0]}}
        ],
    }
    ds = client.post("/datasets", json=geojson).json()["dataset"]["id"]
    decoded = mapbox_vector_tile.decode(fetch(client, ds, 0, 0, 0).content)
    x, y = decoded["default"]["features"][0]["geometry"]["coordinates"]
    # Origin of the world is the NW corner -> (2048, 2048) at z0.
    assert (x, y) == (2048, 2048)


def test_tile_cache_served_from_database(client, published):
    # First request compiles, second is cached; bytes must be identical.
    r1 = fetch(client, published, 3, 4, 4)
    r2 = fetch(client, published, 3, 4, 4)
    assert r1.status_code == r2.status_code == 200
    assert r1.content == r2.content
