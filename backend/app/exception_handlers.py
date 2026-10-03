"""Exception -> RFC 9457 problem-details response.

`register_exception_handlers(app)` is the single place that turns any raised
exception into a client-facing response. Route handlers and services never
build error responses themselves — they raise `AppError` subclasses (or let
Pydantic/Starlette raise their own), and this module is the only place that
knows how to render one.

Log level is deliberately different per exception class: an `AppError` is an
*expected* condition (a 404, a 409, a validation failure) and is logged at
INFO; an unhandled exception is a bug and is logged at ERROR with a
traceback. Treating both the same either buries real errors in expected-4xx
noise or hides bugs at a log level nobody alerts on.
"""

from __future__ import annotations

import structlog
from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.errors import AppError, ErrorCode

logger = structlog.get_logger(__name__)

_PROBLEM_JSON = "application/problem+json"


def _request_id(request: Request) -> str | None:
    return getattr(request.state, "request_id", None)


def _problem_response(
    *,
    request: Request,
    status_code: int,
    code: str,
    title: str,
    detail: str,
    details: dict[str, object] | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    body = {
        "type": f"/problems/{code.lower().replace('_', '-')}",
        "title": title,
        "status": status_code,
        "detail": detail,
        "instance": request.url.path,
        "code": code,
        "request_id": _request_id(request),
        "details": details or {},
    }
    return JSONResponse(
        status_code=status_code, content=body, media_type=_PROBLEM_JSON, headers=headers
    )


def _retry_after(exc: AppError) -> dict[str, str] | None:
    seconds = getattr(exc, "retry_after_seconds", None)
    return {"Retry-After": str(seconds)} if seconds else None


def register_exception_handlers(app: FastAPI) -> None:
    """
    Wire every exception type this app raises to an RFC 9457 response.

    Args:
        app (FastAPI): The application to register handlers on.
    """

    @app.exception_handler(AppError)
    async def handle_app_error(request: Request, exc: AppError) -> JSONResponse:
        logger.info(
            "request.app_error",
            error_code=exc.code,
            status_code=exc.status_code,
            path=request.url.path,
            request_id=_request_id(request),
        )
        return _problem_response(
            request=request,
            status_code=exc.status_code,
            code=exc.code,
            title=exc.title,
            detail=exc.detail,
            details=exc.details,
            headers=_retry_after(exc),
        )

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        logger.info(
            "request.validation_error",
            path=request.url.path,
            request_id=_request_id(request),
            error_count=len(exc.errors()),
        )
        return _problem_response(
            request=request,
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            code=ErrorCode.VALIDATION_ERROR,
            title="Validation Error",
            detail="The request did not pass schema validation.",
            details={"errors": exc.errors()},
        )

    @app.exception_handler(StarletteHTTPException)
    async def handle_http_exception(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        # Starlette/FastAPI raise this internally (404 route-not-found, 405,
        # auth dependencies that haven't been migrated to AppError yet, ...).
        code = {
            404: ErrorCode.NOT_FOUND,
            401: ErrorCode.UNAUTHORIZED,
            403: ErrorCode.FORBIDDEN,
            429: ErrorCode.RATE_LIMITED,
        }.get(exc.status_code, ErrorCode.INTERNAL_ERROR)
        logger.info(
            "request.http_exception",
            status_code=exc.status_code,
            path=request.url.path,
            request_id=_request_id(request),
        )
        return _problem_response(
            request=request,
            status_code=exc.status_code,
            code=code,
            title=code.replace("_", " ").title(),
            detail=str(exc.detail),
        )

    @app.exception_handler(Exception)
    async def handle_unexpected_error(request: Request, exc: Exception) -> JSONResponse:
        logger.error(
            "request.unhandled_exception",
            path=request.url.path,
            request_id=_request_id(request),
            exc_info=exc,
        )
        # Never leak the exception message or a traceback to the client.
        return _problem_response(
            request=request,
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            code=ErrorCode.INTERNAL_ERROR,
            title="Internal Error",
            detail="An unexpected error occurred. It has been logged for investigation.",
        )
