"""The renamer client: chunking, auth, per-server outcomes, retries (ADR-0071)."""

from __future__ import annotations

import base64
import json
import ssl
from typing import Any

import httpx
import pytest

from app.config.settings import Settings
from app.infrastructure.renamer.client import (
    RenamerClient,
    RenameResult,
    RenameStatus,
    build_verify,
)


class _Fake:
    """Answers each batch request from a handler, recording what was sent."""

    def __init__(self, answer: Any) -> None:
        self.answer = answer
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.answer(request)

    def bodies(self) -> list[dict[str, Any]]:
        return [json.loads(r.content) for r in self.requests]


def _client(fake: _Fake, *, chunk_size: int = 10, budget: float = 600) -> RenamerClient:
    return RenamerClient(
        base_url="https://renamer.test/",
        username="svc",
        password="pw",
        verify=False,
        timeout_seconds=5,
        chunk_size=chunk_size,
        budget_seconds=budget,
        transport=httpx.MockTransport(fake.handler),
    )


def _renamed(request: httpx.Request) -> httpx.Response:
    body = json.loads(request.content)
    return httpx.Response(
        200,
        json={
            "renamed": [
                {
                    "idrac_ip": i,
                    "new_profile": f"ocp-c1-mtl-s-{i[-1]}",
                    "conf_id": "1",
                    "variant": None,
                    "changed": not body["dry_run"],
                }
                for i in body["idrac_ips"]
            ],
            "failed": [],
        },
    )


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    async def instant(attempt: int) -> None:
        return

    monkeypatch.setattr(RenamerClient, "_backoff", staticmethod(instant))


async def test_happy_batch_sends_basic_auth_and_returns_each_name() -> None:
    """A real rename is RENAMED, with the renamer's name and conf."""
    fake = _Fake(_renamed)
    client = _client(fake)
    results = await client.rename_batch_dell(["10.0.0.1", "10.0.0.2"], dry_run=False)
    await client.close()
    assert [r.status for r in results] == [RenameStatus.RENAMED, RenameStatus.RENAMED]
    assert results[0].new_profile == "ocp-c1-mtl-s-1"
    request = fake.requests[0]
    assert request.url.path == "/servers/dell/rename-batch"
    assert request.headers["authorization"] == "Basic " + base64.b64encode(b"svc:pw").decode()
    assert fake.bodies() == [{"idrac_ips": ["10.0.0.1", "10.0.0.2"], "dry_run": False}]


async def test_dry_run_is_would_rename() -> None:
    """`changed` is false on a dry run; that is not a failure."""
    fake = _Fake(_renamed)
    client = _client(fake)
    results = await client.rename_batch_cisco(["sp-a"], dry_run=True)
    await client.close()
    assert results[0].status is RenameStatus.WOULD_RENAME
    assert fake.requests[0].url.path == "/servers/cisco/rename-batch"
    assert fake.bodies() == [{"idrac_ips": ["sp-a"], "dry_run": True}]


async def test_chunks_requests() -> None:
    """25 identities with a chunk of 10 is three requests, results in order."""
    fake = _Fake(_renamed)
    client = _client(fake, chunk_size=10)
    ips = [f"10.0.0.{i}" for i in range(25)]
    results = await client.rename_batch_dell(ips, dry_run=True)
    await client.close()
    assert [len(b["idrac_ips"]) for b in fake.bodies()] == [10, 10, 5]
    assert [r.identity for r in results] == ips


async def test_mixed_failures_are_typed_per_server() -> None:
    """A conf mismatch keeps its nearest confs; another failure is FAILED; a missing one UNSENT."""

    def answer(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "renamed": [],
                "failed": [
                    {
                        "idrac_ip": "10.0.0.1",
                        "error": "no conf matches",
                        "components": {"cpu": "x"},
                        "nearest_confs": [{"conf_id": "7", "differences": ["disks"]}],
                    },
                    {"idrac_ip": "10.0.0.2", "error": "404 unknown host"},
                    {"idrac_ip": "10.0.0.3", "error": "502 OME refused"},
                ],
            },
        )

    client = _client(_Fake(answer))
    results = await client.rename_batch_dell(
        ["10.0.0.1", "10.0.0.2", "10.0.0.3", "10.0.0.4"], dry_run=False
    )
    await client.close()
    assert [r.status for r in results] == [
        RenameStatus.CONF_MISMATCH,
        RenameStatus.FAILED,
        RenameStatus.FAILED,
        RenameStatus.UNSENT,
    ]
    assert results[0].nearest_confs == [{"conf_id": "7", "differences": ["disks"]}]
    assert results[2].error == "502 OME refused"
    assert results[3].error == "missing from the renamer's response"


async def test_a_4xx_is_not_retried_and_leaves_the_chunk_unsent() -> None:
    """A 401 fails the whole chunk once; UNSENT means retry next run, no cooldown."""
    fake = _Fake(lambda r: httpx.Response(401, text="bad credentials"))
    client = _client(fake)
    results = await client.rename_batch_dell(["10.0.0.1"], dry_run=False)
    await client.close()
    assert len(fake.requests) == 1
    assert results[0].status is RenameStatus.UNSENT
    assert results[0].error is not None and "401" in results[0].error


async def test_transport_errors_are_retried_twice() -> None:
    """Connect failures are retried twice, then the chunk is UNSENT."""

    def answer(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    fake = _Fake(answer)
    client = _client(fake)
    results = await client.rename_batch_cisco(["sp-a"], dry_run=False)
    await client.close()
    assert len(fake.requests) == 3
    assert results[0].status is RenameStatus.UNSENT


async def test_a_connect_error_then_success_is_answered() -> None:
    """A failed connect never reached the renamer, so one retry is safe."""
    calls = {"n": 0}

    def answer(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ConnectError("refused", request=request)
        return _renamed(request)

    client = _client(_Fake(answer))
    results = await client.rename_batch_dell(["10.0.0.1"], dry_run=True)
    await client.close()
    assert results[0].status is RenameStatus.WOULD_RENAME


async def test_a_read_timeout_is_failed_and_never_resent() -> None:
    """The renamer may have renamed it already: FAILED (audited, cooled down), one request only."""

    def answer(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    fake = _Fake(answer)
    client = _client(fake)
    results = await client.rename_batch_dell(["10.0.0.1", "10.0.0.2"], dry_run=False)
    await client.close()
    assert len(fake.requests) == 1
    assert [r.status for r in results] == [RenameStatus.FAILED, RenameStatus.FAILED]


async def test_a_real_run_with_changed_false_is_unchanged() -> None:
    """Not a dry run, yet nothing changed: UNCHANGED, never WOULD_RENAME."""

    def answer(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"renamed": [{"idrac_ip": "10.0.0.1", "new_profile": "x", "changed": False}]},
        )

    client = _client(_Fake(answer))
    results = await client.rename_batch_dell(["10.0.0.1"], dry_run=False)
    await client.close()
    assert results[0].status is RenameStatus.UNCHANGED


async def test_each_chunk_is_handed_over_as_it_returns_and_the_budget_stops_sending() -> None:
    """`on_chunk` sees every chunk; with no budget left nothing is sent and all are UNSENT."""
    seen: list[list[str]] = []

    async def on_chunk(results: list[RenameResult]) -> None:
        seen.append([r.identity for r in results])

    fake = _Fake(_renamed)
    client = _client(fake, chunk_size=2)
    await client.rename_batch_dell(["a", "b", "c"], dry_run=True, on_chunk=on_chunk)
    await client.close()
    assert seen == [["a", "b"], ["c"]]

    spent = _Fake(_renamed)
    client = _client(spent, budget=0)
    results = await client.rename_batch_dell(["a"], dry_run=True)
    await client.close()
    assert spent.requests == []
    assert results[0].status is RenameStatus.UNSENT


def test_verify_tls_false_skips_certificate_checks() -> None:
    """`renamer.verifyTls: false` reaches httpx as `verify=False`; true is a verifying context."""
    assert build_verify(verify_tls=False, ca_bundle="") is False
    context = build_verify(verify_tls=True, ca_bundle="")
    assert not isinstance(context, bool) and context.verify_mode is ssl.CERT_REQUIRED


def test_verify_tls_false_is_read_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """The chart renders INVENTORY_RENAMER_VERIFY_TLS="false"; Settings reads it as False."""
    monkeypatch.setenv("INVENTORY_RENAMER_VERIFY_TLS", "false")
    assert Settings().renamer_verify_tls is False


async def test_an_identity_answered_twice_is_failed() -> None:
    """Renamed and failed for the same IP: ambiguous, so FAILED for a human to look at."""

    def answer(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "renamed": [{"idrac_ip": "10.0.0.1", "new_profile": "x", "changed": True}],
                "failed": [{"idrac_ip": "10.0.0.1", "error": "boom"}],
            },
        )

    client = _client(_Fake(answer))
    results = await client.rename_batch_dell(["10.0.0.1"], dry_run=False)
    await client.close()
    assert results[0].status is RenameStatus.FAILED
    assert results[0].error == "the renamer answered it twice"


@pytest.mark.parametrize(
    "body",
    [{"renamed": ["10.0.0.1"], "failed": []}, {"renamed": {"a": 1}}, ["not", "an", "object"]],
)
async def test_a_malformed_body_leaves_the_chunk_unsent(body: Any) -> None:
    """A body the client cannot read never crashes the run; the chunk is retried next run."""
    client = _client(_Fake(lambda r: httpx.Response(200, json=body)))
    results = await client.rename_batch_dell(["10.0.0.1"], dry_run=False)
    await client.close()
    assert results[0].status is RenameStatus.UNSENT
