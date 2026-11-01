"""`GET /api/v1/skill` serves the skill the repo carries (ADR-0052)."""

from __future__ import annotations

import io
import zipfile
from collections.abc import AsyncGenerator

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import create_app


@pytest.fixture
async def client() -> AsyncGenerator[AsyncClient, None]:
    app = create_app()
    async with (
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac,
        app.router.lifespan_context(app),
    ):
        yield ac


async def test_the_skill_is_listed_with_its_files(client: AsyncClient) -> None:
    body = (await client.get("/api/v1/skill")).json()

    assert body["name"] == "server-scan-api"
    assert body["files"][0]["path"] == "SKILL.md"
    assert body["version"]


async def test_the_download_is_a_zip_with_this_origin(client: AsyncClient) -> None:
    resp = await client.get("/api/v1/skill/download", params={"base_url": "https://scan.corp"})

    assert resp.headers["content-type"] == "application/zip"
    with zipfile.ZipFile(io.BytesIO(resp.content)) as archive:
        assert b"BASE_URL=https://scan.corp" in archive.read("server-scan-api/SKILL.md")


async def test_a_bad_base_url_is_refused(client: AsyncClient) -> None:
    resp = await client.get("/api/v1/skill/download", params={"base_url": "x\nAUDITOR_TOKEN=1"})

    assert resp.status_code == 422
