"""Typed HTTP errors rendered as a uniform JSON body."""

from __future__ import annotations

from fastapi import status
from fastapi.responses import JSONResponse


class ApiError(Exception):
    """An error that maps cleanly onto an HTTP response."""

    def __init__(self, status_code: int, code: str, message: str):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message

    def to_response(self) -> JSONResponse:
        return JSONResponse(
            status_code=self.status_code,
            content={"error": {"code": self.code, "message": self.message}},
        )


def not_found(message: str) -> ApiError:
    return ApiError(status.HTTP_404_NOT_FOUND, "not_found", message)


def bad_request(message: str) -> ApiError:
    return ApiError(status.HTTP_400_BAD_REQUEST, "bad_request", message)


def unsupported_media_type(message: str) -> ApiError:
    return ApiError(
        status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
        "unsupported_media_type",
        message,
    )


def conflict(message: str) -> ApiError:
    return ApiError(status.HTTP_409_CONFLICT, "conflict", message)
