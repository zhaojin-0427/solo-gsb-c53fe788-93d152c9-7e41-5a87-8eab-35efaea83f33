"""Input validation: bad GeoJSON and bad geometries are rejected at publish."""

from __future__ import annotations


def post_geojson(client, payload_raw=None, *, json_body=None):
    if json_body is not None:
        return client.post("/datasets", json=json_body)
    return client.post(
        "/datasets",
        content=payload_raw,
        headers={"Content-Type": "application/geo+json"},
    )


def test_invalid_json_body(client):
    r = post_geojson(client, b"{not json")
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "bad_request"


def test_empty_body(client):
    r = client.post("/datasets", content=b"", headers={"Content-Type": "application/json"})
    assert r.status_code == 400


def test_non_geojson_document(client):
    r = post_geojson(client, json_body={"hello": "world"})
    assert r.status_code == 400


def test_bare_geometry_rejected(client):
    r = post_geojson(client, json_body={"type": "Point", "coordinates": [0, 0]})
    assert r.status_code == 400


def test_self_intersecting_polygon_rejected(client):
    bowtie = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "properties": {},
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [[[0, 0], [1, 1], [1, 0], [0, 1], [0, 0]]],
                },
            }
        ],
    }
    r = post_geojson(client, json_body=bowtie)
    assert r.status_code == 400
    assert "invalid" in r.json()["error"]["message"]


def test_null_geometry_rejected(client):
    r = post_geojson(client, json_body={
        "type": "FeatureCollection",
        "features": [{"type": "Feature", "properties": {}, "geometry": None}],
    })
    assert r.status_code == 400


def test_empty_geometry_rejected(client):
    r = post_geojson(client, json_body={
        "type": "FeatureCollection",
        "features": [
            {"type": "Feature", "properties": {},
             "geometry": {"type": "Point", "coordinates": []}}
        ],
    })
    assert r.status_code == 400


def test_geometry_collection_rejected(client):
    r = post_geojson(client, json_body={
        "type": "FeatureCollection",
        "features": [
            {"type": "Feature", "properties": {},
             "geometry": {"type": "GeometryCollection", "geometries": []}}
        ],
    })
    assert r.status_code == 400


def test_longitude_out_of_range(client):
    r = post_geojson(client, json_body={
        "type": "FeatureCollection",
        "features": [
            {"type": "Feature", "properties": {},
             "geometry": {"type": "Point", "coordinates": [200, 0]}}
        ],
    })
    assert r.status_code == 400


def test_latitude_beyond_mercator_domain(client):
    r = post_geojson(client, json_body={
        "type": "FeatureCollection",
        "features": [
            {"type": "Feature", "properties": {},
             "geometry": {"type": "Point", "coordinates": [0, 86]}}
        ],
    })
    assert r.status_code == 400


def test_negative_feature_id_rejected(client):
    r = post_geojson(client, json_body={
        "type": "FeatureCollection",
        "features": [
            {"type": "Feature", "id": -3, "properties": {},
             "geometry": {"type": "Point", "coordinates": [0, 0]}}
        ],
    })
    assert r.status_code == 400


def test_float_feature_id_rejected(client):
    r = post_geojson(client, json_body={
        "type": "FeatureCollection",
        "features": [
            {"type": "Feature", "id": 1.5, "properties": {},
             "geometry": {"type": "Point", "coordinates": [0, 0]}}
        ],
    })
    assert r.status_code == 400


def test_nested_properties_rejected(client):
    r = post_geojson(client, json_body={
        "type": "FeatureCollection",
        "features": [
            {"type": "Feature", "id": 1, "properties": {"nested": {"a": 1}},
             "geometry": {"type": "Point", "coordinates": [0, 0]}}
        ],
    })
    assert r.status_code == 400


def test_null_property_value_is_dropped_not_rejected(client):
    r = post_geojson(client, json_body={
        "type": "FeatureCollection",
        "features": [
            {"type": "Feature", "id": 1, "properties": {"x": None, "y": "kept"},
             "geometry": {"type": "Point", "coordinates": [0, 0]}}
        ],
    })
    assert r.status_code == 201


def test_bad_layer_name_rejected(client):
    r = client.post("/datasets?layer=9bad!", json={
        "type": "FeatureCollection", "features": []
    })
    assert r.status_code == 400


def test_missing_feature_type_rejected(client):
    r = post_geojson(client, json_body={
        "type": "FeatureCollection",
        "features": [{"properties": {}, "geometry": {"type": "Point", "coordinates": [0, 0]}}],
    })
    assert r.status_code == 400


def test_unsupported_content_type(client):
    r = client.post(
        "/datasets",
        content=b"{}",
        headers={"Content-Type": "application/octet-stream"},
    )
    assert r.status_code == 415
