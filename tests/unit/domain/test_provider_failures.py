"""`ServerInventoryProvider._contain_server_failure` and the systemic-failure rule (ADR-0062)."""

from __future__ import annotations

from collections.abc import AsyncGenerator

import pytest
from structlog.testing import capture_logs

from app.domain.enums import UnreachableReason
from app.domain.ports.provider import (
    SERVER_FAILED_MARKER,
    ProviderServer,
    ServerIdentity,
    ServerInventoryProvider,
    failed_server,
    is_systemic_failure,
    redact_secrets,
)

_BENIGN_MARKERS = (": unreachable — ", ": login failed for credential ")


class _Provider(ServerInventoryProvider):
    """A provider that only exposes the failure helper."""

    def __init__(self, provider_type: str = "ONEVIEW") -> None:
        super().__init__()
        self.provider_type = provider_type

    async def health_check(self) -> None:
        return

    async def get_one(self, identity: ServerIdentity) -> ProviderServer | None:
        return None

    def _list_servers(self) -> AsyncGenerator[ProviderServer, None]:
        async def _gen() -> AsyncGenerator[ProviderServer, None]:
            yield failed_server(external_id="a", vendor="dell", name="a", detail="x")

        return _gen()


def _fail(provider: _Provider, **overrides: object) -> ProviderServer | None:
    kwargs: dict[str, object] = {
        "event": "test.server_failed",
        "stage": "mapping hardware",
        "vendor": "hp",
        "external_id": "/rest/server-hardware/7",
        "name": "ocp4-tlv-worker-07",
        "serial": "SN-7",
    }
    kwargs.update(overrides)
    return provider._contain_server_failure(KeyError("processorCount"), **kwargs)  # type: ignore[arg-type]  # ty: ignore[invalid-argument-type]


def test_a_failed_server_becomes_a_stub_that_keeps_its_identity_and_states_the_cause() -> None:
    provider = _Provider()

    stub = _fail(provider)

    assert stub is not None
    assert stub.reachable is False
    assert stub.unreachable_reason is UnreachableReason.COLLECTION_ERROR
    assert stub.serial == "SN-7"
    assert stub.external_id == "/rest/server-hardware/7"
    assert stub.unreachable_detail == "mapping hardware: KeyError: 'processorCount'"
    assert stub.cpu_sockets is None
    assert stub.memory_total_bytes is None


def test_the_failure_is_counted_and_recorded_without_a_benign_marker() -> None:
    provider = _Provider()

    _fail(provider)

    assert provider.server_failures == 1
    (message,) = provider.collection_errors
    assert "ocp4-tlv-worker-07" in message
    assert "SN-7" in message
    assert "KeyError" in message
    assert not any(marker in message for marker in _BENIGN_MARKERS)


def test_the_failure_is_logged_with_the_server_the_stage_and_the_error() -> None:
    provider = _Provider()

    with capture_logs() as logs:
        _fail(provider, endpoint="https://oneview.example")

    (entry,) = logs
    assert entry["event"] == "test.server_failed"
    assert entry["log_level"] == "error"
    assert entry["name"] == "ocp4-tlv-worker-07"
    assert entry["serial"] == "SN-7"
    assert entry["stage"] == "mapping hardware"
    assert entry["endpoint"] == "https://oneview.example"
    assert "KeyError" in entry["error"]


def test_a_server_with_no_serial_gets_no_stub_because_ingest_would_duplicate_it() -> None:
    provider = _Provider()

    assert _fail(provider, serial=None) is None
    assert provider.server_failures == 1
    assert len(provider.collection_errors) == 1


def test_a_server_with_no_external_id_gets_no_stub() -> None:
    assert _fail(_Provider(), external_id=None) is None


def test_a_standalone_redfish_host_gets_a_serial_less_stub() -> None:
    stub = _fail(_Provider("REDFISH_STANDALONE"), serial=None)

    assert stub is not None
    assert stub.serial is None
    assert stub.unreachable_reason is UnreachableReason.COLLECTION_ERROR


def test_a_long_error_is_truncated() -> None:
    provider = _Provider()

    stub = provider._contain_server_failure(
        ValueError("x" * 5000),
        event="test.server_failed",
        stage="mapping",
        vendor="hp",
        external_id="e",
        name="n",
        serial="s",
    )

    assert stub is not None
    assert stub.unreachable_detail is not None
    assert len(stub.unreachable_detail) < 500


@pytest.mark.parametrize(
    ("failed", "seen", "systemic"),
    [
        (0, 100, False),
        (4, 4, False),  # everything failed, but too few to call it a pattern
        (5, 5, True),
        (5, 100, False),  # 5 %
        (21, 100, True),
        (20, 100, False),  # exactly the ratio is not above it
        (500, 2500, False),
        (501, 2500, True),
    ],
)
def test_the_systemic_failure_rule(failed: int, seen: int, systemic: bool) -> None:
    assert is_systemic_failure(failed, seen) is systemic


async def test_collect_counts_the_servers_it_yields_and_resets_each_run() -> None:
    provider = _Provider()
    provider._server_failures = 3

    first = [s async for s in provider.collect()]
    second = [s async for s in provider.collect()]

    assert len(first) == len(second) == 1
    assert provider.servers_seen == 1
    assert provider.server_failures == 0


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("GET https://user:pw@host/x failed", "GET https://***@host/x failed"),
        ("http://admin@10.0.0.1:8443/a", "http://***@10.0.0.1:8443/a"),
        ("https://h/x?token=abc&y=1", "https://h/x?token=***&y=1"),
        ("https://h/x?a=1&password=x", "https://h/x?a=1&password=***"),
        ("https://h/x?API_KEY=k1&Secret=s2", "https://h/x?API_KEY=***&Secret=***"),
        ("nothing secret: https://host/path?a=1", "nothing secret: https://host/path?a=1"),
    ],
)
def test_redact_secrets_masks_url_userinfo_and_secret_query_values(
    text: str, expected: str
) -> None:
    assert redact_secrets(text) == expected


def test_the_log_the_message_and_the_stub_detail_never_carry_a_secret() -> None:
    provider = _Provider()
    exc = RuntimeError("GET https://u:pw@bmc/x?token=abc failed")

    with capture_logs() as logs:
        stub = provider._contain_server_failure(
            exc,
            event="test.server_failed",
            stage="mapping",
            vendor="hp",
            external_id="e",
            name="n",
            serial="s",
        )

    assert stub is not None
    (message,) = provider.collection_errors
    texts = [str(logs[0]["error"]), message, str(stub.unreachable_detail)]
    for text in texts:
        assert "pw" not in text
        assert "abc" not in text
        assert "https://***@bmc/x?token=***" in text


async def test_dropped_failures_stay_in_the_systemic_denominator() -> None:
    class _Mixed(_Provider):
        def _list_servers(self) -> AsyncGenerator[ProviderServer, None]:
            async def _gen() -> AsyncGenerator[ProviderServer, None]:
                for i in range(5):
                    self._contain_server_failure(
                        KeyError("x"),
                        event="t",
                        stage="mapping",
                        vendor="hp",
                        external_id=f"e{i}",
                        name=f"n{i}",
                        serial=None,
                    )
                for i in range(20):
                    yield failed_server(external_id=f"g{i}", vendor="hp", name=f"g{i}", detail="d")

            return _gen()

    provider = _Mixed()

    yielded = [s async for s in provider.collect()]

    assert len(yielded) == 20
    assert provider.server_failures == 5
    assert provider.servers_seen == 25
    assert is_systemic_failure(provider.server_failures, provider.servers_seen) is False
    assert is_systemic_failure(5, 20) is True  # what the old count would have said


def test_a_no_external_id_failure_is_counted_as_seen() -> None:
    provider = _Provider()

    assert _fail(provider, external_id=None) is None
    assert provider.servers_seen == 1


def test_every_recorded_message_carries_the_failed_marker() -> None:
    provider = _Provider()

    _fail(provider)
    _fail(provider, serial=None)
    _fail(provider, external_id=None)

    assert len(provider.collection_errors) == 3
    assert all(SERVER_FAILED_MARKER in m for m in provider.collection_errors)
