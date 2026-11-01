"""The MCP tools over a mocked Server Scan API (ADR-0054)."""

from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from unittest import mock

import httpx
import pytest
from mcp.shared.memory import create_connected_server_and_client_session
from tools.mcp_server.api import ScanApi
from tools.mcp_server.server import build_server

from app.domain.services.search import FILTER_FIELDS

_SERVER = {
    "id": "srv_abc",
    "name": "ocp4-tlv-worker-01",
    "vendor": "dell",
    "model": "R750",
    "site_id": "tlv",
    "health": {"overall": "HEALTHY"},
    "openshift": {"lifecycle_state": "AVAILABLE", "cluster_name": None},
    "maintenance": {"enabled": False},
    "stale": False,
    "reachable": True,
}


class FakeApi:
    """Answers the paths the tools read, and remembers every request."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.status = 200
        self.pages = 1

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.status != 200:
            return httpx.Response(self.status, json={"detail": "nope"})
        path = request.url.path
        if path == "/api/v1/servers":
            cursor = request.url.params.get("cursor")
            page = int(cursor) if cursor else 1
            return httpx.Response(
                200,
                json={
                    "items": [{**_SERVER, "id": f"srv_{page}", "name": f"srv-{page}-a"}],
                    "page": {"has_more": page < self.pages, "next_cursor": str(page + 1)},
                },
            )
        if path == "/api/v1/servers/rows":
            rows = [{"site_id": "tlv", "vendor": "dell", "health": "CRITICAL"}] * 3
            rows += [{"site_id": "nyc", "vendor": "hp", "health": "HEALTHY"}]
            return httpx.Response(200, json={"items": rows})
        if path == "/api/v1/servers/srv_abc":
            return httpx.Response(200, json={**_SERVER, "hardware": {"big": True}})
        return httpx.Response(404, json={"detail": "unknown"})


@pytest.fixture
def fake() -> FakeApi:
    return FakeApi()


@asynccontextmanager
async def _connected(fake: FakeApi) -> AsyncIterator[Any]:
    """A client session on the MCP server, reading the fake API with token `ssk_test`."""
    client = httpx.AsyncClient(transport=httpx.MockTransport(fake.handler))
    server = build_server(ScanApi("http://api", client))
    with mock.patch.dict(os.environ, {"SERVER_SCAN_TOKEN": "ssk_test"}):
        async with create_connected_server_and_client_session(server._mcp_server) as session:
            yield session


async def _call(session: Any, name: str, **args: Any) -> Any:
    result = await session.call_tool(name, args)
    text = result.content[0].text
    return result.isError, (json.loads(text) if not result.isError else text)


async def test_every_tool_is_read_only(fake: FakeApi) -> None:
    async with _connected(fake) as session:
        tools = (await session.list_tools()).tools

        assert {t.name for t in tools} == {
                "list_servers", "find_servers", "get_server", "fleet_summary",
                "list_sites", "server_events", "recent_events", "list_health_policies",
            }  # fmt: skip
        assert all(t.annotations and t.annotations.readOnlyHint for t in tools)


async def test_list_servers_passes_filters_and_the_callers_token(fake: FakeApi) -> None:
    async with _connected(fake) as session:
        _, body = await _call(
            session, "list_servers", vendor="dell", site_id="tlv", maintenance=False
        )

        request = fake.requests[0]
        assert dict(request.url.params) == {
                "vendor": "dell", "site_id": "tlv", "maintenance": "false", "page_size": "50",
            }  # fmt: skip
        assert request.headers["authorization"] == "Bearer ssk_test"
        assert body["servers"][0]["health"] == "HEALTHY"


async def test_list_servers_follows_cursors_up_to_the_limit(fake: FakeApi) -> None:
    async with _connected(fake) as session:
        fake.pages = 5

        _, body = await _call(session, "list_servers", limit=3)

        assert (body["count"], body["truncated"]) == (3, True)


async def test_fleet_summary_counts_server_side(fake: FakeApi) -> None:
    async with _connected(fake) as session:
        _, body = await _call(session, "fleet_summary", group_by="health")

        assert body == {"group_by": "health", "total": 4, "counts": {"CRITICAL": 3, "HEALTHY": 1}}


async def test_get_server_by_id_drops_the_bulky_detail(fake: FakeApi) -> None:
    async with _connected(fake) as session:
        _, body = await _call(session, "get_server", name_or_id="srv_abc")

        assert body["name"] == "ocp4-tlv-worker-01"
        assert "hardware" not in body


async def test_a_refused_token_is_explained(fake: FakeApi) -> None:
    async with _connected(fake) as session:
        fake.status = 401

        is_error, text = await _call(session, "list_sites")

        assert is_error
        assert "expired" in text


async def test_a_missing_token_says_how_to_add_one(
    fake: FakeApi, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("SERVER_SCAN_TOKEN", raising=False)
    server = build_server(
        ScanApi("http://api", httpx.AsyncClient(transport=httpx.MockTransport(fake.handler)))
    )
    async with create_connected_server_and_client_session(server._mcp_server) as s:
        is_error, text = await _call(s, "list_sites")

    assert is_error
    assert "Authorization" in text
    assert fake.requests == []


def test_every_list_servers_filter_is_one_the_api_accepts() -> None:
    server = build_server(ScanApi("http://api"))
    tool = server._tool_manager.get_tool("list_servers")
    assert tool is not None
    params = set(tool.parameters["properties"]) - {"limit"}

    assert params <= set(FILTER_FIELDS) | {"stale"}


async def test_the_http_app_forwards_the_authorization_header_and_guards_the_host(
    fake: FakeApi,
) -> None:
    server = build_server(
        ScanApi("http://api", httpx.AsyncClient(transport=httpx.MockTransport(fake.handler))),
        allowed_hosts=["scan.example"],
    )
    app = server.streamable_http_app()
    call = {
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "list_sites", "arguments": {}},
    }  # fmt: skip
    headers = {
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
        "Authorization": "Bearer ssk_from_header",
    }
    async with server.session_manager.run():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://scan.example") as http:
            await http.post("/mcp", json=call, headers=headers)
            wrong = await http.post("/mcp", json=call, headers={**headers, "Host": "evil.example"})

    assert fake.requests[0].headers["authorization"] == "Bearer ssk_from_header"
    assert wrong.status_code in (400, 421)
