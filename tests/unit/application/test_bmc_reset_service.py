"""`BmcResetService`: cooldown, report-only, busy BMC, failures and the audit event (ADR-0050)."""

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from app.application.services.bmc_reset_service import BmcResetService
from app.domain.enums import LinkState, Vendor
from app.domain.models.audit_event import EventType
from app.domain.models.hardware import Hardware, Storage, StorageDrive
from app.domain.models.network import NetworkInfo, NetworkInterface
from app.domain.models.server import Identity, Server
from app.domain.ports.bmc_reset import BmcResetPolicy, BmcResetResult, ResetOutcome

NOW = datetime.now(UTC)
POLICY = BmcResetPolicy(report_only=False, cooldown=timedelta(hours=12), max_per_run=10)


class _Audit:
    """Records what the service writes and answers the cooldown question."""

    def __init__(self, *, recently_reset: bool = False) -> None:
        self.recently_reset = recently_reset
        self.events: list[dict[str, Any]] = []

    async def has_event_since(self, event_type: EventType, server_id: str, since: datetime) -> bool:
        assert event_type is EventType.IDRAC_RESET
        assert since < datetime.now(UTC) - timedelta(hours=11)
        return self.recently_reset

    async def record(self, event_type: EventType, **kwargs: Any) -> None:
        self.events.append({"event_type": event_type, **kwargs})


class _Resetter:
    """Returns a canned result, or raises."""

    def __init__(self, result: BmcResetResult | Exception) -> None:
        self.result = result
        self.calls: list[bool] = []

    async def reset(self, server: Server, *, apply: bool) -> BmcResetResult:
        self.calls.append(apply)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def _server(*, unknown: bool = True) -> Server:
    state = LinkState.UNKNOWN if unknown else LinkState.UP
    return Server(
        _id="srv_1",
        name="ocp4-tlv-01",
        identity=Identity(vendor=Vendor.DELL),
        created_at=NOW,
        updated_at=NOW,
        source_provider="OPENMANAGE",
        hardware=Hardware(storage=Storage(drives=[StorageDrive(id="d0", health="HEALTHY")])),
        network=NetworkInfo(interfaces=[NetworkInterface(name="eno1", link_state=state)]),
    )


def _service(
    result: BmcResetResult | Exception, audit: _Audit, policy: BmcResetPolicy = POLICY
) -> tuple[BmcResetService, _Resetter]:
    resetter = _Resetter(result)
    return BmcResetService(resetter=resetter, audit=audit, policy=policy), resetter  # ty: ignore[invalid-argument-type]


async def test_a_requested_reset_is_audited_with_what_triggered_it() -> None:
    audit = _Audit()
    service, resetter = _service(BmcResetResult(ResetOutcome.REQUESTED, "10.0.0.5"), audit)

    assert await service.maybe_reset(_server()) is True

    assert resetter.calls == [True]
    [event] = audit.events
    assert event["event_type"] is EventType.IDRAC_RESET
    assert event["server_id"] == "srv_1"
    assert event["data"] == {
        "trigger": "nics",
        "outcome": "requested",
        "bmc_host": "10.0.0.5",
        "detail": "",
        "active_jobs": [],
    }


@pytest.mark.parametrize("outcome", [ResetOutcome.FAILED, ResetOutcome.UNCONFIRMED])
async def test_a_failed_or_unconfirmed_attempt_is_audited_so_it_starts_the_cooldown(
    outcome: ResetOutcome,
) -> None:
    audit = _Audit()
    service, _ = _service(BmcResetResult(outcome, "10.0.0.5", "why"), audit)

    assert await service.maybe_reset(_server()) is True

    assert [e["data"]["outcome"] for e in audit.events] == [outcome.value]


async def test_a_server_reset_inside_the_cooldown_is_left_alone() -> None:
    audit = _Audit(recently_reset=True)
    service, resetter = _service(BmcResetResult(ResetOutcome.REQUESTED), audit)

    assert await service.maybe_reset(_server()) is False

    assert resetter.calls == []
    assert audit.events == []


async def test_a_busy_job_queue_skips_without_an_event_or_using_the_budget() -> None:
    audit = _Audit()
    busy = BmcResetResult(
        ResetOutcome.SKIPPED_ACTIVE_JOBS, "10.0.0.5", active_jobs=("JID_1 (Running)",)
    )
    service, _ = _service(busy, audit)

    assert await service.maybe_reset(_server()) is False

    assert audit.events == []


async def test_report_only_sends_nothing_writes_no_event_but_uses_the_budget() -> None:
    audit = _Audit()
    policy = BmcResetPolicy(report_only=True, cooldown=POLICY.cooldown, max_per_run=10)
    service, resetter = _service(
        BmcResetResult(ResetOutcome.WOULD_RESET, "10.0.0.5"), audit, policy
    )

    assert await service.maybe_reset(_server()) is True

    assert resetter.calls == [False]
    assert audit.events == []


async def test_a_server_with_readable_disks_and_nics_never_reaches_the_bmc() -> None:
    audit = _Audit()
    service, resetter = _service(BmcResetResult(ResetOutcome.REQUESTED), audit)

    assert await service.maybe_reset(_server(unknown=False)) is False

    assert resetter.calls == []


async def test_a_resetter_that_raises_never_breaks_the_ingest() -> None:
    audit = _Audit()
    service, _ = _service(RuntimeError("boom"), audit)

    assert await service.maybe_reset(_server()) is False

    assert audit.events == []
