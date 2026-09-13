"""`app.infrastructure.providers.oneview.client`.

Transport behaviour only — the JSON-to-DTO mapping has its own test.
What matters here is that the version header is negotiated and clamped,
that the session is always deleted, that paging terminates and is
complete, and that a truncated collection is *detected* rather than
silently returned as the whole estate.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest
from structlog.testing import capture_logs

from app.infrastructure.providers.oneview.client import (
    MAX_TESTED_API_VERSION,
    OneViewClient,
    OneViewConnectionError,
)

pytestmark = pytest.mark.unit


def _client(handler: Callable[[httpx.Request], httpx.Response], **kwargs: Any) -> OneViewClient:
    """
    A client wired to a scripted transport.

    Args:
        handler (Callable[[httpx.Request], httpx.Response]): An
            `httpx.MockTransport` request handler.
        **kwargs: Overrides for the client constructor.

    Returns:
        OneViewClient: The client under test.
    """
    defaults: dict[str, Any] = {
        "endpoint": "oneview-1.example.net",
        "username": "collector",
        "password": "secret",
        "timeout_seconds": 5.0,
    }
    defaults.update(kwargs)
    return OneViewClient(transport=httpx.MockTransport(handler), **defaults)


def _version(current: int = 8000, minimum: int = 1) -> httpx.Response:
    """
    The unauthenticated version document.

    Args:
        current (int): `currentVersion`.
        minimum (int): `minimumVersion`.

    Returns:
        httpx.Response: A 200 carrying them.
    """
    return httpx.Response(200, json={"currentVersion": current, "minimumVersion": minimum})


def _page(
    members: list[dict[str, Any]],
    *,
    total: int | None = None,
    next_page_uri: str | None = None,
    uri: str = "/rest/things",
) -> httpx.Response:
    """
    One page of a OneView collection.

    Args:
        members (list[dict[str, Any]]): The page's members.
        total (int | None): The collection's reported total; defaults to
            the page length.
        next_page_uri (str | None): The next page, or `None` for the last.
        uri (str): This page's own `uri`.

    Returns:
        httpx.Response: A 200 carrying the collection envelope.
    """
    return httpx.Response(
        200,
        json={
            "start": 0,
            "count": len(members),
            "total": len(members) if total is None else total,
            "members": members,
            "uri": uri,
            "nextPageUri": next_page_uri,
        },
    )


def _logged_in(
    collections: dict[str, list[httpx.Response]],
    *,
    version: httpx.Response | None = None,
    seen: list[httpx.Request] | None = None,
) -> Callable[[httpx.Request], httpx.Response]:
    """
    A handler that answers version discovery, login, logout, and a
    scripted sequence of pages per path.

    Args:
        collections (dict[str, list[httpx.Response]]): Path (without
            query) -> the responses to return, in order.
        version (httpx.Response | None): The `/rest/version` answer.
        seen (list[httpx.Request] | None): Collects every request made.

    Returns:
        Callable[[httpx.Request], httpx.Response]: The handler.
    """

    def handle(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        path = request.url.path
        if path == "/rest/version":
            return version or _version()
        if path == "/rest/login-sessions":
            if request.method == "DELETE":
                return httpx.Response(204)
            return httpx.Response(200, json={"sessionID": "token-abc", "partnerData": {}})
        pages = collections.get(path)
        if not pages:
            return httpx.Response(404, json={"errorCode": "NOT_FOUND"})
        return pages.pop(0)

    return handle


class TestApiVersion:
    async def test_discovered_version_is_clamped_to_what_this_code_was_written_against(
        self,
    ) -> None:
        """An appliance newer than this mapping hands us a contract we
        have never read. HPE guarantees an older version keeps working,
        so we ask for the newest one we actually know.
        """
        async with _client(_logged_in({}, version=_version(current=99000, minimum=5600))) as client:
            assert client.api_version == MAX_TESTED_API_VERSION

    async def test_an_older_appliance_gets_its_own_current_version(self) -> None:
        async with _client(_logged_in({}, version=_version(current=4600, minimum=2400))) as client:
            assert client.api_version == 4600

    async def test_an_appliance_whose_floor_is_above_the_tested_version_uses_that_floor(
        self,
    ) -> None:
        """The one case the clamp must not apply: sending a version the
        appliance no longer accepts is a 412, not a safe fallback.
        """
        async with _client(_logged_in({}, version=_version(current=99000, minimum=9000))) as client:
            assert client.api_version == 9000

    async def test_a_configured_version_outside_the_supported_range_is_rejected(self) -> None:
        client = _client(
            _logged_in({}, version=_version(current=8000, minimum=5600)), api_version=1
        )
        with pytest.raises(OneViewConnectionError, match="outside the range"):
            await client.login()
        await client.logout()

    async def test_a_configured_version_inside_the_range_wins_over_the_clamp(self) -> None:
        async with _client(
            _logged_in({}, version=_version(current=8000, minimum=5600)), api_version=6000
        ) as client:
            assert client.api_version == 6000

    async def test_every_authenticated_request_carries_the_version_and_session(self) -> None:
        seen: list[httpx.Request] = []
        handler = _logged_in({"/rest/things": [_page([])]}, seen=seen)
        async with _client(handler) as client:
            await client.get_all("/rest/things")

        collection = [r for r in seen if r.url.path == "/rest/things"]
        assert collection
        for request in collection:
            assert request.headers["X-Api-Version"] == str(MAX_TESTED_API_VERSION)
            assert request.headers["Auth"] == "token-abc"

    async def test_version_discovery_sends_neither_auth_nor_version(self) -> None:
        """HPE documents `GET /rest/version` as needing neither, which is
        what lets `health_check` prove reachability without spending a
        session.
        """
        seen: list[httpx.Request] = []
        async with _client(_logged_in({}, seen=seen)):
            pass

        probe = next(r for r in seen if r.url.path == "/rest/version")
        assert "Auth" not in probe.headers
        assert "X-Api-Version" not in probe.headers


class TestSession:
    async def test_logout_deletes_the_session(self) -> None:
        """2400 sessions per appliance and 960 per source IP, each living
        24 idle hours: a CronJob that leaks one per run burns that budget.
        """
        seen: list[httpx.Request] = []
        async with _client(_logged_in({}, seen=seen)):
            pass

        deletes = [r for r in seen if r.method == "DELETE" and r.url.path == "/rest/login-sessions"]
        assert len(deletes) == 1
        assert deletes[0].headers["Auth"] == "token-abc"

    async def test_login_sends_the_acknowledgement_flag(self) -> None:
        """An appliance configured to require login-message
        acknowledgement rejects a login without it.
        """
        seen: list[httpx.Request] = []
        async with _client(_logged_in({}, seen=seen)):
            pass

        login = next(r for r in seen if r.method == "POST" and r.url.path == "/rest/login-sessions")
        assert json.loads(login.content)["loginMsgAck"] is True

    async def test_a_login_without_a_session_id_is_an_error(self) -> None:
        def handle(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/rest/version":
                return _version()
            return httpx.Response(200, json={"partnerData": {}})

        client = _client(handle)
        with pytest.raises(OneViewConnectionError, match="no sessionID"):
            await client.login()
        await client.logout()

    async def test_rejected_credentials_name_the_appliance(self) -> None:
        def handle(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/rest/version":
                return _version()
            return httpx.Response(401, json={"errorCode": "AUTHORIZATION"})

        client = _client(handle)
        with pytest.raises(OneViewConnectionError, match=r"oneview-1\.example\.net"):
            await client.login()
        await client.logout()

    async def test_a_failed_logout_never_masks_the_real_error(self) -> None:
        def handle(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/rest/version":
                return _version()
            if request.method == "POST":
                return httpx.Response(200, json={"sessionID": "token-abc"})
            raise httpx.ConnectError("appliance went away")

        client = _client(handle)
        await client.login()
        await client.logout()  # must not raise


class TestPagination:
    async def test_never_sends_count_minus_one(self) -> None:
        """`count=-1` means *64* on /rest/server-profiles, not "all" —
        the single most likely way to ship a collector that silently sees
        a fraction of the estate.
        """
        seen: list[httpx.Request] = []
        handler = _logged_in({"/rest/server-profiles": [_page([])]}, seen=seen)
        async with _client(handler) as client:
            await client.get_all("/rest/server-profiles", page_size=256)

        request = next(r for r in seen if r.url.path == "/rest/server-profiles")
        assert request.url.params["count"] == "256"

    async def test_follows_next_page_uri_to_the_end(self) -> None:
        pages = {
            "/rest/server-profiles": [
                _page(
                    [{"uri": f"/p{i}"} for i in range(256)],
                    total=300,
                    next_page_uri="/rest/server-profiles?start=256&count=256",
                    uri="/rest/server-profiles?start=0&count=256",
                ),
                _page(
                    [{"uri": f"/p{i}"} for i in range(256, 300)],
                    total=300,
                    uri="/rest/server-profiles?start=256&count=256",
                ),
            ]
        }
        async with _client(_logged_in(pages)) as client:
            members = await client.get_all("/rest/server-profiles", page_size=256)

        assert len(members) == 300

    async def test_truncation_past_the_256_cap_is_detected_and_logged(self) -> None:
        """The failure this collector must never hide: the appliance
        reports 900 profiles, hands back 256 and no `nextPageUri`, and a
        naive loop returns a third of the estate as if it were all of it.
        """
        pages = {
            "/rest/server-profiles": [
                _page([{"uri": f"/p{i}"} for i in range(256)], total=900, next_page_uri=None)
            ]
        }
        with capture_logs() as events:
            async with _client(_logged_in(pages)) as client:
                members = await client.get_all("/rest/server-profiles", page_size=256)

        assert len(members) == 256
        truncated = [e for e in events if e["event"] == "oneview.collection_truncated"]
        assert truncated == [
            {
                "event": "oneview.collection_truncated",
                "log_level": "error",
                "endpoint": "oneview-1.example.net",
                "path": "/rest/server-profiles",
                "fetched": 256,
                "reported_total": 900,
                "page_size": 256,
                "hint": truncated[0]["hint"],
            }
        ]

    async def test_a_complete_collection_logs_no_truncation(self) -> None:
        pages = {"/rest/server-profiles": [_page([{"uri": "/p0"}], total=1)]}
        with capture_logs() as events:
            async with _client(_logged_in(pages)) as client:
                await client.get_all("/rest/server-profiles", page_size=256)

        assert not [e for e in events if e["event"] == "oneview.collection_truncated"]

    async def test_a_self_referential_next_page_uri_terminates(self) -> None:
        """An appliance has been observed returning a `nextPageUri` equal
        to the page's own `uri`; without the guard the loop never ends.
        """
        pages = {
            "/rest/things": [
                _page(
                    [{"uri": "/t0"}],
                    total=1,
                    uri="/rest/things?start=0&count=256",
                    next_page_uri="/rest/things?start=0&count=256",
                )
            ]
        }
        async with _client(_logged_in(pages)) as client:
            assert len(await client.get_all("/rest/things")) == 1

    async def test_extra_params_reach_the_query(self) -> None:
        seen: list[httpx.Request] = []
        handler = _logged_in({"/rest/server-hardware": [_page([])]}, seen=seen)
        async with _client(handler) as client:
            await client.get_all("/rest/server-hardware", page_size=25, params={"expand": "all"})

        request = next(r for r in seen if r.url.path == "/rest/server-hardware")
        assert request.url.params["expand"] == "all"
        assert request.url.params["count"] == "25"

    async def test_a_failed_page_names_the_path_and_status(self) -> None:
        async with _client(_logged_in({})) as client:
            with pytest.raises(OneViewConnectionError, match="404"):
                await client.get_all("/rest/missing")


class TestRawGet:
    async def test_send_version_false_drops_only_the_version_header(self) -> None:
        """`tools/verify_oneview.py` isolates the version header to settle
        what an appliance does when it is omitted.
        """
        seen: list[httpx.Request] = []
        async with _client(_logged_in({"/rest/things": [_page([])]}, seen=seen)) as client:
            await client.raw_get("/rest/things", send_version=False)

        request = next(r for r in seen if r.url.path == "/rest/things")
        assert "X-Api-Version" not in request.headers
        assert request.headers["Auth"] == "token-abc"

    async def test_a_non_2xx_is_returned_rather_than_raised(self) -> None:
        async with _client(_logged_in({})) as client:
            assert (await client.raw_get("/rest/missing")).status_code == 404
