"""FastAPI application: dataset publishing and immutable MVT tile serving."""

from __future__ import annotations

import os
from typing import Optional

from fastapi import FastAPI, Header, Query, Request, UploadFile, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response

from . import db, services
from .constants import (
    MAX_UPLOAD_BYTES,
    MAX_ZOOM,
    MIN_ZOOM,
    MVT_CONTENT_TYPE,
)
from .errors import ApiError, bad_request

def _default_db_path() -> str:
    env = os.environ.get("MVT_DB_PATH")
    if env:
        return env
    # Prefer the mounted Docker volume; fall back to a local directory so the
    # package is also importable for tests without /data.
    if os.access("/", os.W_OK) or os.path.isdir("/data"):
        return "/data/tiles.db"
    return os.path.join(os.getcwd(), "data", "tiles.db")


DB_PATH = _default_db_path()


def create_app(db_path: str | None = None) -> FastAPI:
    path = db_path or DB_PATH
    db.init_db(path)
    app = FastAPI(
        title="Offline Vector Tile Compilation API",
        version="1.0.0",
        description=(
            "Publish EPSG:4326 GeoJSON layers as immutable revisions and read "
            "Web Mercator (EPSG:3857) Mapbox Vector Tiles for z=0..14."
        ),
    )
    app.state.db_path = path

    @app.exception_handler(ApiError)
    async def _api_error_handler(request: Request, exc: ApiError):
        return exc.to_response()

    @app.exception_handler(RequestValidationError)
    async def _validation_handler(request: Request, exc: RequestValidationError):
        # The spec asks for 400 on bad parameters rather than FastAPI's 422.
        detail = exc.errors()[0] if exc.errors() else {}
        loc = ".".join(str(p) for p in detail.get("loc", []) if p != "query")
        msg = detail.get("msg", "invalid request parameter")
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={"error": {"code": "bad_request", "message": f"{loc}: {msg}"}},
        )

    @app.get("/")
    async def root():
        return {
            "service": "offline-vector-tile-api",
            "zoom_range": [MIN_ZOOM, MAX_ZOOM],
            "tile_extent": 4096,
            "docs": "/docs",
        }

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    @app.post("/datasets", status_code=status.HTTP_201_CREATED)
    async def create_dataset(request: Request):
        raw, content_type, filename = await _read_geojson(request)
        layer_name = request.query_params.get("layer")
        expected = _parse_expected(
            request.query_params.get("expected_revision"),
            request.headers.get("x-expected-revision"),
        )
        result = await _run(
            services.publish_revision,
            app.state.db_path,
            raw,
            dataset_id=None,
            layer_name=layer_name,
            expected_revision=expected,
            content_type=content_type,
            filename=filename,
        )
        return JSONResponse(status_code=status.HTTP_201_CREATED, content=result)

    @app.post("/datasets/{dataset_id}", status_code=status.HTTP_201_CREATED)
    async def publish_dataset(
        dataset_id: str,
        request: Request,
        layer: Optional[str] = Query(None),
        expected_revision: Optional[int] = Query(None),
        x_expected_revision: Optional[str] = Header(None, alias="X-Expected-Revision"),
        file: Optional[UploadFile] = None,
    ):
        if file is not None:
            raw = await file.read()
            content_type = file.content_type
            filename = file.filename
        else:
            raw, content_type, filename = await _read_geojson(request)
        expected = _parse_expected(
            str(expected_revision) if expected_revision is not None else None,
            x_expected_revision,
        )
        result = await _run(
            services.publish_revision,
            app.state.db_path,
            raw,
            dataset_id=dataset_id,
            layer_name=layer,
            expected_revision=expected,
            content_type=content_type,
            filename=filename,
        )
        return JSONResponse(status_code=status.HTTP_201_CREATED, content=result)

    @app.get("/datasets")
    async def list_datasets():
        return {"datasets": await _run(services.list_datasets_view, app.state.db_path)}

    @app.get("/datasets/{dataset_id}")
    async def get_dataset(dataset_id: str):
        return await _run(services.get_dataset_view, app.state.db_path, dataset_id)

    @app.get("/datasets/{dataset_id}/revisions")
    async def list_revisions(dataset_id: str):
        view = await _run(services.get_dataset_view, app.state.db_path, dataset_id)
        return {"dataset_id": dataset_id, "revisions": view["revisions"]}

    @app.get("/datasets/{dataset_id}/revisions/{revision}")
    async def get_revision(dataset_id: str, revision: int):
        return await _run(
            services.get_revision_view, app.state.db_path, dataset_id, revision
        )

    @app.get("/datasets/{dataset_id}/revisions/{revision}/tiles/{z}/{x}/{y}.mvt")
    async def get_pinned_tile(
        dataset_id: str,
        revision: int,
        z: int,
        x: int,
        y: int,
        if_none_match: Optional[str] = Header(None, alias="If-None-Match"),
    ):
        return await _tile_response(
            app.state.db_path, dataset_id, revision, z, x, y, if_none_match
        )

    @app.get("/datasets/{dataset_id}/tiles/{z}/{x}/{y}.mvt")
    async def get_latest_tile(
        dataset_id: str,
        z: int,
        x: int,
        y: int,
        if_none_match: Optional[str] = Header(None, alias="If-None-Match"),
        revision: Optional[int] = Query(None),
    ):
        return await _tile_response(
            app.state.db_path, dataset_id, revision, z, x, y, if_none_match
        )

    return app


def _parse_expected(from_query: Optional[str], from_header: Optional[str]) -> Optional[int]:
    raw = from_query if from_query is not None else from_header
    if raw is None or raw == "":
        return None
    try:
        value = int(raw)
    except ValueError:
        raise bad_request("expected_revision must be an integer revision number")
    if value < 1:
        raise bad_request("expected_revision must be >= 1")
    return value


async def _read_geojson(request: Request) -> tuple[bytes, str | None, str | None]:
    form = None
    content_type = request.headers.get("content-type", "")
    if content_type.startswith("multipart/form-data"):
        form = await request.form()
        upload = form.get("file") or form.get("geojson")
        if upload is None or not hasattr(upload, "read"):
            raise bad_request("multipart body must contain a 'file' part")
        raw = await upload.read()
        return raw, upload.content_type, upload.filename
    raw = await request.body()
    if len(raw) > MAX_UPLOAD_BYTES:
        raise bad_request(
            f"uploaded GeoJSON exceeds the {MAX_UPLOAD_BYTES // (1024 * 1024)} MiB limit"
        )
    return raw, content_type, None


async def _tile_response(db_path, dataset_id, revision, z, x, y, if_none_match):
    result = await _run(
        services.get_tile_response, db_path, dataset_id, revision, z, x, y
    )
    etag = result["etag"]
    if if_none_match and _etags_match(if_none_match, etag):
        return Response(
            status_code=status.HTTP_304_NOT_MODIFIED,
            headers={
                "ETag": etag,
                "Cache-Control": result["cache_control"],
            },
        )
    headers = {
        "ETag": etag,
        "Cache-Control": result["cache_control"],
        "X-Revision": str(result["revision"]),
        "X-Layer-Name": result["layer_name"],
        "Vary": "If-None-Match",
    }
    content = result["content"]
    if result["is_empty"]:
        # An empty tile is still a valid, cachable representation.
        return Response(status_code=status.HTTP_204_NO_CONTENT, headers=headers)
    return Response(content=content, media_type=MVT_CONTENT_TYPE, headers=headers)


def _etags_match(header_value: str, etag: str) -> bool:
    tokens = [t.strip() for t in header_value.split(",")]
    return "*" in tokens or etag in tokens


async def _run(func, *args, **kwargs):
    """Run blocking SQLite/geometry work outside the event loop."""
    import anyio

    return await anyio.to_thread.run_sync(lambda: func(*args, **kwargs))


app = create_app()
