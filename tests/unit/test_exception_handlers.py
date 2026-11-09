"""An error body never carries an unbounded string (ADR-0002, 2026-11-09 addendum)."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.errors import AppError, ServiceUnavailableError
from app.exception_handlers import MAX_ERROR_TEXT_CHARS, register_exception_handlers

_HUGE = "x" * (3 * 1024 * 1024)


async def _body_for(error: Exception) -> dict[str, Any]:
    app = FastAPI()
    register_exception_handlers(app)

    @app.get("/boom")
    async def boom() -> None:
        raise error

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/boom")
    return resp.json()


async def test_a_multi_mb_detail_is_cut_with_a_marker() -> None:
    body = await _body_for(AppError(_HUGE))

    detail = str(body["detail"])
    assert len(detail) < MAX_ERROR_TEXT_CHARS + 200
    assert detail.startswith("x" * MAX_ERROR_TEXT_CHARS)
    assert f"{len(_HUGE) - MAX_ERROR_TEXT_CHARS:,} more characters cut" in detail
    assert "request_id" in detail


async def test_every_long_string_inside_details_is_cut_too() -> None:
    error = ServiceUnavailableError(
        "the vendor failed",
        details={"reason": _HUGE, "nested": {"items": [_HUGE, "short"]}, "count": 3},
    )
    body = await _body_for(error)

    details = body["details"]
    assert isinstance(details, dict)
    assert len(details["reason"]) < MAX_ERROR_TEXT_CHARS + 200
    first, second = details["nested"]["items"]
    assert len(first) < MAX_ERROR_TEXT_CHARS + 200
    assert second == "short"
    assert details["count"] == 3


async def test_a_short_detail_and_details_are_unchanged() -> None:
    error = AppError("nothing to see", details={"server_ids": ["a", "b"], "n": 1})
    body = await _body_for(error)

    assert body["detail"] == "nothing to see"
    assert body["details"] == {"server_ids": ["a", "b"], "n": 1}


async def test_a_detail_of_exactly_the_cap_is_unchanged() -> None:
    text = "y" * MAX_ERROR_TEXT_CHARS
    body = await _body_for(AppError(text))

    assert body["detail"] == text
