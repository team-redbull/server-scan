"""One bad VM, member link or page never aborts the KubeVirt run (ADR-0062)."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from structlog.testing import capture_logs
from tests.kubevirt_fixture import SYSTEMS, KubeVirtFixture, Vm, kubevirt_tree

from app.domain.enums import ManagerType, UnreachableReason
from app.domain.models.common import AuditFields
from app.domain.models.manager import Manager
from app.domain.ports.provider import ProviderServer
from app.infrastructure.providers.kubevirt import provider as provider_module
from app.infrastructure.providers.kubevirt.client import KubeVirtClient
from app.infrastructure.providers.kubevirt.provider import KubeVirtProvider
from app.infrastructure.providers.redfish.client import RedfishAuthError

pytestmark = pytest.mark.unit

_A = Vm(name="a", serial="SER-A")
_B = Vm(name="b", serial="SER-B")
_C = Vm(name="c", serial="SER-C")


@pytest.fixture(autouse=True)
def _no_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    async def instant(self: object, attempt: int, retry_after: str | None) -> None:
        return None

    monkeypatch.setattr(KubeVirtClient, "_backoff", instant)


@pytest.fixture
def facade() -> Iterator[KubeVirtFixture]:
    with KubeVirtFixture(resources=kubevirt_tree(_A, _B, _C)) as fixture:
        yield fixture


def _provider(url: str, **overrides: Any) -> KubeVirtProvider:
    manager = Manager(
        _id="mgr_kubevirt_x",
        name="kubevirt-x",
        type=ManagerType.KUBEVIRT,
        endpoint="https://kv.example",
        enabled=True,
        audit=AuditFields.new(),
    )
    kwargs: dict[str, Any] = {
        "manager": manager,
        "url": url,
        "username": "scan",
        "password": "secret",
        "verify_tls": True,
        "ca_bundle": "",
        "connect_timeout": 2.0,
        "read_timeout": 5.0,
        "vm_budget_seconds": 10.0,
        "run_budget_seconds": 30.0,
        "concurrency": 4,
    }
    kwargs.update(overrides)
    return KubeVirtProvider(**kwargs)


async def _collect(provider: KubeVirtProvider) -> dict[str, ProviderServer]:
    return {s.name: s async for s in provider.collect()}


def _break_mapper(monkeypatch: pytest.MonkeyPatch, system_id: str) -> None:
    real = provider_module.system_to_provider_server

    def broken(system: dict[str, Any], **kwargs: Any) -> ProviderServer:
        if system.get("Id") == system_id:
            raise TypeError("boom")
        return real(system, **kwargs)

    monkeypatch.setattr(provider_module, "system_to_provider_server", broken)


async def test_a_mapper_bug_stubs_only_that_vm(
    facade: KubeVirtFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    _break_mapper(monkeypatch, _B.system_id)
    provider = _provider(facade.url)

    with capture_logs() as logs:
        servers = await _collect(provider)

    assert set(servers) == {"a", "b", "c"}
    stub = servers["b"]
    assert stub.reachable is False
    assert stub.unreachable_reason is UnreachableReason.COLLECTION_ERROR
    assert "TypeError" in (stub.unreachable_detail or "")
    assert stub.serial == "SER-B"
    assert stub.external_id.endswith(_B.path)
    assert servers["a"].reachable and servers["c"].reachable
    assert provider.server_failures == 1
    (error,) = provider.collection_errors
    assert "b" in error and "TypeError" in error
    assert ": unreachable — " not in error and ": login failed for credential " not in error
    failed = [e for e in logs if e["event"] == "kubevirt.vm_failed_unexpected"]
    assert failed[0]["path"] == _B.path
    summary = next(e for e in logs if e["event"] == "kubevirt.run_summary")
    assert (summary["vms_listed"], summary["vms_collected"], summary["vms_failed"]) == (3, 2, 1)


async def test_an_unreadable_system_is_recorded_without_a_stub(facade: KubeVirtFixture) -> None:
    facade.faults[_B.path] = 500
    provider = _provider(facade.url)

    servers = await _collect(provider)

    assert set(servers) == {"a", "c"}
    assert provider.server_failures == 1


async def test_a_rejected_login_mid_run_still_stops_the_run(facade: KubeVirtFixture) -> None:
    facade.reject_after = 3
    with pytest.raises(RedfishAuthError):
        await _collect(_provider(facade.url, concurrency=1))


async def test_a_vanished_vm_yields_nothing_and_is_not_a_failure(facade: KubeVirtFixture) -> None:
    del facade.resources[_B.path]
    provider = _provider(facade.url)

    with capture_logs() as logs:
        servers = await _collect(provider)

    assert set(servers) == {"a", "c"}
    assert provider.server_failures == 0 and provider.collection_errors == ()
    summary = next(e for e in logs if e["event"] == "kubevirt.run_summary")
    assert summary["vms_failed"] == 0 and summary["vms_vanished"] == 1


async def test_one_bad_member_link_does_not_kill_the_listing(facade: KubeVirtFixture) -> None:
    facade.resources[SYSTEMS]["Members"].insert(0, {"@odata.id": "https://evil.example/x"})
    provider = _provider(facade.url)

    with capture_logs() as logs:
        servers = await _collect(provider)

    assert set(servers) == {"a", "b", "c"}
    (error,) = provider.collection_errors
    assert "evil.example" in error
    assert any(e["event"] == "kubevirt.member_link_skipped" for e in logs)


async def test_members_next_link_is_followed(facade: KubeVirtFixture) -> None:
    page2 = "/redfish/v1/SystemsPage2"
    page1 = facade.resources[SYSTEMS]
    page1["Members"] = [{"@odata.id": _A.path}]
    page1["Members@odata.nextLink"] = page2
    facade.resources[page2] = {"Members": [{"@odata.id": _B.path}, {"@odata.id": _C.path}]}

    servers = await _collect(_provider(facade.url))

    assert set(servers) == {"a", "b", "c"}


async def test_skipped_drive_and_resource_logs_carry_system_and_path(
    facade: KubeVirtFixture,
) -> None:
    facade.faults[f"{_A.path}/Storage/1/Drives/scratch"] = 500
    facade.faults[f"{_B.path}/Memory"] = 503

    with capture_logs() as logs:
        await _collect(_provider(facade.url))

    drive = next(e for e in logs if e["event"] == "kubevirt.drive_skipped")
    assert drive["system"] == _A.system_id
    assert drive["storage"] == "1"
    assert drive["path"].endswith("/Drives/scratch")
    resource = next(e for e in logs if e["event"] == "kubevirt.resource_skipped")
    assert resource["system"] == _B.system_id
    assert resource["path"] == f"{_B.path}/Memory"


async def test_the_run_budget_log_lists_the_unfinished_vms(facade: KubeVirtFixture) -> None:
    for vm in (_A, _B, _C):
        facade.delays[vm.path] = 0.6
    provider = _provider(facade.url, run_budget_seconds=0.2, concurrency=1)

    with capture_logs() as logs:
        await _collect(provider)

    event = next(e for e in logs if e["event"] == "kubevirt.run_budget_exceeded")
    assert event["log_level"] == "error"
    assert set(event["unfinished_systems"]) == {_A.system_id, _B.system_id, _C.system_id}
    assert _A.system_id in provider.collection_errors[0]


async def test_get_one_logs_a_bug_with_facade_and_system_and_raises_it(
    facade: KubeVirtFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.domain.ports.provider import ServerIdentity

    _break_mapper(monkeypatch, _A.system_id)
    provider = _provider(facade.url)
    external_id = f"kubevirt://{facade.url.removeprefix('http://')}{_A.path}"

    with capture_logs() as logs, pytest.raises(TypeError):
        await provider.get_one(ServerIdentity(external_id=external_id))

    event = next(e for e in logs if e["event"] == "kubevirt.recheck_failed_unexpected")
    assert event["system"] == _A.path and event["origin"] == facade.url


async def test_a_listing_that_repeats_page_one_is_recorded_and_keeps_the_first_page(
    facade: KubeVirtFixture,
) -> None:
    facade.resources[SYSTEMS]["Members@odata.nextLink"] = SYSTEMS
    provider = _provider(facade.url)

    with capture_logs() as logs:
        servers = await _collect(provider)

    assert set(servers) == {"a", "b", "c"}
    (error,) = provider.collection_errors
    assert "repeats page" in error
    event = next(e for e in logs if e["event"] == "kubevirt.listing_cycle")
    assert event["log_level"] == "error"
    assert event["page"] == SYSTEMS


async def test_get_one_for_a_vm_that_404s_returns_none_without_an_error_log(
    facade: KubeVirtFixture,
) -> None:
    from app.domain.ports.provider import ServerIdentity

    del facade.resources[_B.path]
    provider = _provider(facade.url)
    external_id = f"kubevirt://{facade.url.removeprefix('http://')}{_B.path}"

    with capture_logs() as logs:
        result = await provider.get_one(ServerIdentity(external_id=external_id))

    assert result is None
    assert not [e for e in logs if e["log_level"] == "error"]
    assert provider.collection_errors == ()
