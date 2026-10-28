"""`IdracResetter`: the job-queue check, the one POST and every way it can fail (ADR-0050)."""

from datetime import UTC, datetime
from typing import Any, Self

import pytest

from app.domain.enums import Vendor
from app.domain.models.network import BmcInfo, NetworkInfo
from app.domain.models.server import Identity, Server
from app.domain.ports.bmc_reset import ResetOutcome
from app.infrastructure.providers.redfish.client import (
    RedfishError,
    RedfishForbiddenError,
    RedfishUnreachableError,
)
from app.infrastructure.providers.redfish.idrac_reset import IdracResetter
from app.infrastructure.providers.redfish.targets import RedfishCredential, RedfishTarget

MANAGER = "/redfish/v1/Managers/iDRAC.Embedded.1"
SYSTEM = "/redfish/v1/Systems/System.Embedded.1"
POWER_ON = (f"{SYSTEM}/Actions/ComputerSystem.Reset", {"ResetType": "On"})
RESTART = (f"{MANAGER}/Actions/Manager.Reset", {"ResetType": "GracefulRestart"})


class _FakeClient:
    """A scripted Redfish session that records every POST."""

    def __init__(
        self,
        jobs: list[dict[str, Any]],
        post_error: Exception | None = None,
        power: list[str] | None = None,
    ) -> None:
        self._jobs = jobs
        self._power = list(power or ["On"])
        self._post_error = post_error
        self.posts: list[tuple[str, dict[str, str]]] = []

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def get(self, path: str) -> dict[str, Any]:
        if path == "/redfish/v1/Systems":
            return {"Members": [{"@odata.id": SYSTEM}]}
        if path == SYSTEM:
            return {"PowerState": self._power.pop(0) if len(self._power) > 1 else self._power[0]}
        assert path == "/redfish/v1/Managers"
        return {
            "Members": [
                {"@odata.id": "/redfish/v1/Managers/System.Embedded.1"},
                {"@odata.id": MANAGER},
            ]
        }

    async def get_collection(self, path: str) -> list[dict[str, Any]]:
        assert path.startswith("/redfish/v1/JobService/Jobs")
        return self._jobs

    async def post(self, path: str, body: dict[str, str]) -> dict[str, Any]:
        self.posts.append((path, body))
        if self._post_error:
            raise self._post_error
        return {}


def _server(host: str | None = "10.0.0.5") -> Server:
    now = datetime.now(UTC)
    return Server(
        _id="srv_1",
        name="x",
        identity=Identity(vendor=Vendor.DELL),
        created_at=now,
        updated_at=now,
        network=NetworkInfo(bmc=BmcInfo(host=host)),
    )


def _resetter(client: _FakeClient) -> IdracResetter:
    def factory(target: RedfishTarget) -> Any:
        assert target.host == "10.0.0.5"
        return client

    return IdracResetter(
        credential=RedfishCredential(name="ome-bmc", username="root", password="x"),
        port=443,
        verify_tls=False,
        ca_bundle=None,
        connect_timeout=1,
        read_timeout=1,
        client_factory=factory,
        power_on_wait_seconds=1,
        poll_seconds=0.01,
    )


async def test_an_idle_queue_posts_one_graceful_restart_to_the_idrac_manager() -> None:
    client = _FakeClient([{"Id": "JID_1", "JobState": "Completed"}])

    result = await _resetter(client).reset(_server(), apply=True)

    assert result.outcome is ResetOutcome.REQUESTED
    assert result.bmc_host == "10.0.0.5"
    assert client.posts == [(f"{MANAGER}/Actions/Manager.Reset", {"ResetType": "GracefulRestart"})]


@pytest.mark.parametrize("state", ["Running", "Downloading", "Waiting", "New", "Paused", None])
async def test_an_unfinished_or_unknown_job_blocks_the_reset(state: str | None) -> None:
    client = _FakeClient([{"Id": "JID_9", "JobState": state}] if state else [{"Id": "JID_9"}])

    result = await _resetter(client).reset(_server(), apply=True)

    assert result.outcome is ResetOutcome.SKIPPED_ACTIVE_JOBS
    assert result.active_jobs == (f"JID_9 ({state})",)
    assert client.posts == []


async def test_a_scheduled_job_waiting_for_a_host_reboot_does_not_block() -> None:
    client = _FakeClient([{"Id": "JID_2", "JobState": "Scheduled"}])

    assert (await _resetter(client).reset(_server(), apply=True)).outcome is ResetOutcome.REQUESTED


async def test_report_only_reads_the_queue_and_sends_nothing() -> None:
    client = _FakeClient([])

    result = await _resetter(client).reset(_server(), apply=False)

    assert result.outcome is ResetOutcome.WOULD_RESET
    assert client.posts == []


async def test_an_idrac_that_drops_the_connection_after_the_post_is_unconfirmed_not_failed() -> (
    None
):
    client = _FakeClient([], post_error=RedfishUnreachableError("gone"))

    assert (
        await _resetter(client).reset(_server(), apply=True)
    ).outcome is ResetOutcome.UNCONFIRMED


async def test_an_account_without_the_privilege_is_a_failure_with_a_reason() -> None:
    client = _FakeClient([], post_error=RedfishForbiddenError("403"))

    result = await _resetter(client).reset(_server(), apply=True)

    assert result.outcome is ResetOutcome.FAILED
    assert "forbidden" in result.detail


async def test_any_other_redfish_error_is_a_failure_not_an_exception() -> None:
    client = _FakeClient([], post_error=RedfishError("boom"))

    assert (await _resetter(client).reset(_server(), apply=True)).outcome is ResetOutcome.FAILED


async def test_a_server_without_a_bmc_host_fails_before_any_connection() -> None:
    client = _FakeClient([])

    result = await _resetter(client).reset(_server(host=None), apply=True)

    assert result.outcome is ResetOutcome.FAILED
    assert client.posts == []


async def test_a_powered_off_host_is_powered_on_before_the_idrac_restart() -> None:
    client = _FakeClient([], power=["Off", "Off", "On"])

    result = await _resetter(client).reset(_server(), apply=True)

    assert result.outcome is ResetOutcome.REQUESTED
    assert result.powered_on is True
    assert client.posts == [POWER_ON, RESTART]


async def test_a_host_that_never_comes_on_leaves_the_idrac_alone() -> None:
    client = _FakeClient([], power=["Off"])

    result = await _resetter(client).reset(_server(), apply=True)

    assert result.outcome is ResetOutcome.FAILED
    assert result.powered_on is True
    assert client.posts == [POWER_ON]


async def test_a_powered_off_host_in_maintenance_is_not_powered_on() -> None:
    client = _FakeClient([], power=["Off"])
    server = _server()
    server.maintenance.enabled = True

    result = await _resetter(client).reset(server, apply=True)

    assert result.outcome is ResetOutcome.FAILED
    assert client.posts == []


async def test_report_only_never_powers_a_host_on() -> None:
    client = _FakeClient([], power=["Off"])

    result = await _resetter(client).reset(_server(), apply=False)

    assert result.outcome is ResetOutcome.WOULD_RESET
    assert "power the host on" in result.detail
    assert client.posts == []
