"""Consistent error bodies: code, safe message, correlation_id, retryability, details."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from opsbridge.auth.permissions import Forbidden

log = logging.getLogger("opsbridge.api")


class ApiError(Exception):
    def __init__(
        self,
        status: int,
        code: str,
        message: str,
        *,
        retryable: bool = False,
        details: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.retryable = retryable
        self.details = details or {}
        self.headers = headers


def unauthenticated() -> ApiError:
    return ApiError(401, "unauthenticated", "Sign in to continue.")


def not_found(what: str = "resource") -> ApiError:
    # Used for both missing and not-permitted resources so existence is not disclosed.
    return ApiError(404, "not_found", f"The requested {what} was not found.")


def conflict(code: str, message: str, details: dict[str, Any] | None = None) -> ApiError:
    return ApiError(409, code, message, details=details)


def invalid(message: str, details: dict[str, Any] | None = None) -> ApiError:
    return ApiError(422, "invalid_input", message, details=details)


def unavailable(dependency: str) -> ApiError:
    return ApiError(503, "dependency_unavailable", f"{dependency} is unavailable.", retryable=True)


def _body(
    request: Request,
    status: int,
    code: str,
    message: str,
    retryable: bool,
    details: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    cid = getattr(request.state, "correlation_id", None)
    return JSONResponse(
        status_code=status,
        content={
            "error": {
                "code": code,
                "message": message,
                "correlation_id": cid,
                "retryable": retryable,
                "details": details or {},
            }
        },
        headers=headers,
    )


def install(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def _api_error(request: Request, exc: ApiError) -> JSONResponse:
        return _body(
            request, exc.status, exc.code, exc.message, exc.retryable, exc.details, exc.headers
        )

    @app.exception_handler(Forbidden)
    async def _forbidden(request: Request, exc: Forbidden) -> JSONResponse:
        return _body(
            request,
            403,
            "forbidden",
            "You do not have permission for this action.",
            False,
            {"required_permission": exc.permission},
        )

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError) -> JSONResponse:
        errors = [
            {
                "loc": [str(p) for p in e.get("loc", ())],
                "msg": str(e.get("msg", "")),
                "type": e.get("type"),
            }
            for e in exc.errors()
        ]
        return _body(
            request, 422, "invalid_input", "The request is invalid.", False, {"errors": errors}
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = {404: "not_found", 405: "method_not_allowed", 413: "payload_too_large"}.get(
            exc.status_code, "http_error"
        )
        return _body(request, exc.status_code, code, str(exc.detail), False)

    @app.exception_handler(Exception)
    async def _unexpected(request: Request, exc: Exception) -> JSONResponse:
        log.exception(
            "unhandled error",
            extra={"correlation_id": getattr(request.state, "correlation_id", None)},
        )
        return _body(request, 500, "internal_error", "An unexpected error occurred.", True)
