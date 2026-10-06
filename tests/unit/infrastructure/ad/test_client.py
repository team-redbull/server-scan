"""`ldap_validate` and `AdApiClient.group_members` (docs/adr/0034).

`ldap3.Connection`/`Server` are monkeypatched at the module level `client.py`
imports them from — no real socket, no test LDAP server required for this
file (`tests/api/test_auth.py` covers the login flow end to end with a fake
`AuthService`; a real Samba AD DC is exercised manually, see
docs/adr/0034's "Local testing" section).
"""

from __future__ import annotations

import socket
import ssl
import threading
from collections.abc import Callable, Iterator

import httpx
import pytest
from ldap3.core.exceptions import LDAPBindError, LDAPSocketOpenError

from app.config.settings import Settings
from app.errors import ServiceUnavailableError
from app.infrastructure.ad import client as ad_client
from app.infrastructure.ad.client import AdApiClient, build_ad_api_http_client, ldap_validate

pytestmark = pytest.mark.unit

_SETTINGS = Settings(
    _env_file=None,
    ldap_server="dc.example.com",
    ldap_domain="EXAMPLE",
    ad_api_url="https://ad-api.example.com",
)


class _FakeConnection:
    def __init__(self, *_args: object, **_kwargs: object) -> None:
        self.unbound = False

    def unbind(self) -> None:
        self.unbound = True


class TestLdapValidate:
    async def test_empty_username_or_password_returns_false_without_a_bind(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _unexpected(*_args: object, **_kwargs: object) -> None:
            raise AssertionError("must not bind with an empty username/password")

        monkeypatch.setattr(ad_client, "Connection", _unexpected)
        assert await ldap_validate(_SETTINGS, "", "pw") is False
        assert await ldap_validate(_SETTINGS, "user", "") is False

    async def test_a_successful_bind_binds_as_domain_backslash_username(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: dict[str, object] = {}

        def _connection(_server: object, **kwargs: object) -> _FakeConnection:
            seen.update(kwargs)
            return _FakeConnection()

        monkeypatch.setattr(ad_client, "Connection", _connection)
        assert await ldap_validate(_SETTINGS, "jdoe", "hunter2") is True
        assert seen["user"] == "EXAMPLE\\jdoe"
        assert seen["password"] == "hunter2"

    async def test_invalid_credentials_returns_false(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _raise(*_args: object, **_kwargs: object) -> None:
            raise LDAPBindError("invalidCredentials: 80090308")

        monkeypatch.setattr(ad_client, "Connection", _raise)
        assert await ldap_validate(_SETTINGS, "jdoe", "wrong") is False

    async def test_a_bind_error_that_is_not_invalid_credentials_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _raise(*_args: object, **_kwargs: object) -> None:
            raise LDAPBindError("unwillingToPerform")

        monkeypatch.setattr(ad_client, "Connection", _raise)
        with pytest.raises(ServiceUnavailableError):
            await ldap_validate(_SETTINGS, "jdoe", "x")

    async def test_a_dead_ldap_server_raises_service_unavailable_not_false(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _raise(*_args: object, **_kwargs: object) -> None:
            raise LDAPSocketOpenError("could not open socket")

        monkeypatch.setattr(ad_client, "Connection", _raise)
        with pytest.raises(ServiceUnavailableError):
            await ldap_validate(_SETTINGS, "jdoe", "x")


def _ad_api(handler: Callable[[httpx.Request], httpx.Response]) -> AdApiClient:
    http = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://ad-api.example.com"
    )
    return AdApiClient(http, client_id="test-client-id")


class TestAdApiClient:
    async def test_group_members_lower_cases_sam_account_names(self) -> None:
        def _handler(request: httpx.Request) -> httpx.Response:
            assert request.url.params["samAccountName"] == "Admins"
            assert request.url.params["isRecursive"] == "true"
            assert request.headers["ClientId"] == "test-client-id"
            return httpx.Response(
                200, json=[{"sAMAccountName": "JDoe"}, {"sAMAccountName": "asmith"}]
            )

        members = await _ad_api(_handler).group_members("Admins")
        assert members == {"jdoe", "asmith"}

    async def test_a_member_with_no_sam_account_name_is_skipped(self) -> None:
        def _handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200, json=[{"distinguishedName": "CN=x"}, {"sAMAccountName": "ok"}]
            )

        members = await _ad_api(_handler).group_members("g")
        assert members == {"ok"}

    async def test_a_non_2xx_response_raises_service_unavailable(self) -> None:
        def _handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(500)

        with pytest.raises(ServiceUnavailableError):
            await _ad_api(_handler).group_members("g")

    async def test_invalid_json_raises_service_unavailable(self) -> None:
        def _handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=b"not json")

        with pytest.raises(ServiceUnavailableError):
            await _ad_api(_handler).group_members("g")

    async def test_a_non_array_body_raises_service_unavailable(self) -> None:
        def _handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"not": "an array"})

        with pytest.raises(ServiceUnavailableError):
            await _ad_api(_handler).group_members("g")

    async def test_a_transport_error_raises_service_unavailable(self) -> None:
        def _handler(_request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused")

        with pytest.raises(ServiceUnavailableError):
            await _ad_api(_handler).group_members("g")


def _flaky_ad_api(
    statuses: list[int | Exception], *, retries: int
) -> tuple[AdApiClient, list[int]]:
    """An `AdApiClient` whose successive attempts yield `statuses` (200 = a one-member group)."""
    calls: list[int] = []

    def _handler(_request: httpx.Request) -> httpx.Response:
        outcome = statuses[len(calls)]
        calls.append(1)
        if isinstance(outcome, Exception):
            raise outcome
        return httpx.Response(outcome, json=[{"sAMAccountName": "ok"}] if outcome == 200 else None)

    http = httpx.AsyncClient(
        transport=httpx.MockTransport(_handler), base_url="https://ad-api.example.com"
    )
    return AdApiClient(http, client_id="id", retries=retries, retry_delay_seconds=0), calls


class TestAdApiRetries:
    async def test_a_read_timeout_is_retried_and_the_second_attempt_wins(self) -> None:
        client, calls = _flaky_ad_api([httpx.ReadTimeout("slow"), 200], retries=1)
        assert await client.group_members("g") == {"ok"}
        assert len(calls) == 2

    async def test_a_503_is_retried(self) -> None:
        client, calls = _flaky_ad_api([503, 200], retries=1)
        assert await client.group_members("g") == {"ok"}
        assert len(calls) == 2

    async def test_a_4xx_is_never_retried(self) -> None:
        client, calls = _flaky_ad_api([403, 200], retries=1)
        with pytest.raises(ServiceUnavailableError) as raised:
            await client.group_members("g")
        assert len(calls) == 1
        assert raised.value.reason == "http_403"

    async def test_invalid_json_is_never_retried(self) -> None:
        calls: list[int] = []

        def _handler(_request: httpx.Request) -> httpx.Response:
            calls.append(1)
            return httpx.Response(200, content=b"not json")

        http = httpx.AsyncClient(
            transport=httpx.MockTransport(_handler), base_url="https://ad-api.example.com"
        )
        with pytest.raises(ServiceUnavailableError) as raised:
            await AdApiClient(http, client_id="id", retries=2).group_members("g")
        assert len(calls) == 1
        assert raised.value.reason == "invalid_json"

    async def test_retries_are_bounded_and_the_failure_names_dependency_and_reason(self) -> None:
        client, calls = _flaky_ad_api([httpx.ReadTimeout("x")] * 3, retries=1)
        with pytest.raises(ServiceUnavailableError) as raised:
            await client.group_members("g")
        assert len(calls) == 2
        assert raised.value.dependency == "ad_api"
        assert raised.value.reason == "read_timeout"
        assert "ReadTimeout" in raised.value.detail

    async def test_a_connect_timeout_gets_its_own_reason(self) -> None:
        client, _calls = _flaky_ad_api([httpx.ConnectTimeout("x")], retries=0)
        with pytest.raises(ServiceUnavailableError) as raised:
            await client.group_members("g")
        assert raised.value.reason == "connect_timeout"


@pytest.fixture
def silent_ldap_port() -> Iterator[int]:
    """A TCP listener that accepts connections and never answers — a hung directory."""
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(5)
    held: list[socket.socket] = []

    def _accept() -> None:
        while True:
            try:
                conn, _addr = listener.accept()
            except OSError:
                return
            held.append(conn)

    threading.Thread(target=_accept, daemon=True).start()
    yield listener.getsockname()[1]
    listener.close()
    for conn in held:
        conn.close()


class TestLdapTimeouts:
    async def test_a_hung_server_fails_at_the_receive_timeout_instead_of_blocking_forever(
        self, silent_ldap_port: int
    ) -> None:
        settings = _SETTINGS.model_copy(
            update={
                "ldap_server": "127.0.0.1",
                "ldap_port": silent_ldap_port,
                "ldap_receive_timeout_seconds": 1,
            }
        )
        with pytest.raises(ServiceUnavailableError) as raised:
            await ldap_validate(settings, "jdoe", "pw")
        assert raised.value.dependency == "ldap"
        assert raised.value.reason == "receive_timeout"

    async def test_a_refused_connection_is_connect_failed(self) -> None:
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
        probe.close()
        settings = _SETTINGS.model_copy(update={"ldap_server": "127.0.0.1", "ldap_port": port})
        with pytest.raises(ServiceUnavailableError) as raised:
            await ldap_validate(settings, "jdoe", "pw")
        assert raised.value.reason == "connect_failed"


def _ssl_verify_mode(client: httpx.AsyncClient) -> int:
    return client._transport._pool._ssl_context.verify_mode  # ty: ignore[unresolved-attribute]


class TestBuildAdApiHttpClient:
    async def test_connect_and_read_timeouts_are_separate_settings(self) -> None:
        settings = _SETTINGS.model_copy(
            update={"auth_connect_timeout_seconds": 1.5, "auth_request_timeout_seconds": 2.5}
        )
        async with build_ad_api_http_client(settings) as http:
            assert http.timeout.connect == 1.5
            assert http.timeout.read == 2.5

    async def test_defaults_to_verifying_the_system_trust_store(self) -> None:
        async with build_ad_api_http_client(_SETTINGS) as http:
            assert _ssl_verify_mode(http) == ssl.CERT_REQUIRED

    async def test_verify_tls_false_skips_certificate_verification(self) -> None:
        settings = _SETTINGS.model_copy(update={"ad_api_verify_tls": False})
        async with build_ad_api_http_client(settings) as http:
            assert _ssl_verify_mode(http) == ssl.CERT_NONE

    async def test_verify_tls_false_overrides_a_configured_ca_bundle(self) -> None:
        # A blank/unset bundle plus verify_tls=False must still skip
        # verification — the override is the escape hatch, not the bundle.
        settings = _SETTINGS.model_copy(update={"ad_api_ca_bundle": "", "ad_api_verify_tls": False})
        async with build_ad_api_http_client(settings) as http:
            assert _ssl_verify_mode(http) == ssl.CERT_NONE
