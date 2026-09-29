"""GeoJSON publishing, revisions and validation."""

from __future__ import annotations


def test_publish_creates_dataset_and_revision(client):
    body = {
        "type": "FeatureCollection",
        "features": [
            {"type": "Feature", "id": 1, "properties": {}, "geometry": {"type": "Point", "coordinates": [10, 20]}}
        ],
    }
    r = client.post("/datasets", json=body)
    assert r.status_code == 201
    data = r.json()
    assert data["layer_name"] == "default"
    assert data["feature_count"] == 1
    assert data["dataset"]["latest_revision"] == 1
    assert data["dataset"]["revision"] == 1
    assert len(data["dataset"]["id"]) == 32


def test_publish_with_custom_layer_name(client):
    body = {"type": "FeatureCollection", "features": []}
    r = client.post("/datasets?layer=roads", json=body)
    assert r.status_code == 201
    assert r.json()["layer_name"] == "roads"


def test_publish_multipart_file(client):
    geojson = (
        b'{"type":"FeatureCollection","features":['
        b'{"type":"Feature","id":1,"properties":{"a":1},"geometry":{"type":"Point","coordinates":[0,0]}}]}'
    )
    r = client.post(
        "/datasets",
        files={"file": ("layer.geojson", geojson, "application/geo+json")},
    )
    assert r.status_code == 201
    assert r.json()["feature_count"] == 1


def test_single_feature_document_is_accepted(client):
    body = {
        "type": "Feature",
        "id": 9,
        "properties": {},
        "geometry": {"type": "Point", "coordinates": [1, 1]},
    }
    r = client.post("/datasets", json=body)
    assert r.status_code == 201
    assert r.json()["feature_count"] == 1


def test_revision_history_is_immutable_and_monotonic(client, published):
    r1 = client.post(f"/datasets/{published}?expected_revision=1", json={
        "type": "FeatureCollection",
        "features": [
            {"type": "Feature", "id": 1, "properties": {}, "geometry": {"type": "Point", "coordinates": [2, 2]}}
        ],
    })
    assert r1.status_code == 201
    assert r1.json()["dataset"]["revision"] == 2

    # Revision 1 is still addressable and reports the original feature count.
    rev1 = client.get(f"/datasets/{published}/revisions/1").json()
    rev2 = client.get(f"/datasets/{published}/revisions/2").json()
    assert rev1["feature_count"] == 3
    assert rev2["feature_count"] == 1
    assert rev1["published_at"] != rev2["published_at"]

    listing = client.get(f"/datasets/{published}").json()
    assert [r["revision"] for r in listing["revisions"]] == [1, 2]
    assert listing["latest_revision"] == 2


def test_publish_to_unknown_dataset_is_404(client):
    r = client.post("/datasets/deadbeef", json={"type": "FeatureCollection", "features": []})
    assert r.status_code == 404


def test_expected_revision_conflict_returns_409(client, published):
    # Pretend we are still on revision 1 after revision 2 already exists.
    client.post(f"/datasets/{published}?expected_revision=1",
                json={"type": "FeatureCollection", "features": []})
    r = client.post(f"/datasets/{published}?expected_revision=1",
                    json={"type": "FeatureCollection", "features": []})
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "conflict"


def test_expected_revision_via_header(client, published):
    r = client.post(
        f"/datasets/{published}",
        json={"type": "FeatureCollection", "features": []},
        headers={"X-Expected-Revision": "1"},
    )
    assert r.status_code == 201
    assert r.json()["dataset"]["revision"] == 2


def test_expected_revision_on_create_is_rejected(client):
    r = client.post("/datasets?expected_revision=3",
                    json={"type": "FeatureCollection", "features": []})
    assert r.status_code == 400


def test_concurrent_publish_exactly_one_winner(client, published):
    import threading

    from app.errors import ApiError
    from app import services

    body = (b'{"type":"FeatureCollection","features":['
            b'{"type":"Feature","id":1,"properties":{},"geometry":{"type":"Point","coordinates":[1,1]}}]}')
    outcomes: list[int] = []
    lock = threading.Lock()

    def publish():
        try:
            services.publish_revision(
                client.app.state.db_path,
                body,
                dataset_id=published,
                layer_name=None,
                expected_revision=1,
                content_type="application/json",
            )
            code = 201
        except ApiError as exc:
            code = exc.status_code
        with lock:
            outcomes.append(code)

    threads = [threading.Thread(target=publish) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sorted(outcomes).count(201) == 1
    assert sorted(outcomes).count(409) == 3
    assert client.get(f"/datasets/{published}").json()["latest_revision"] == 2
