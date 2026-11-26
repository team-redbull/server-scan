"""The rename sweep: who is sent, dry run, audit events, cooldown, exit code (ADR-0071)."""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Any

import httpx
import pytest
from tools.rename_fleet import Candidate, SweepSummary, _still_eligible, sweep

from app.application.services.audit_service import AuditService
from app.infrastructure.mongodb import MongoClientHolder
from app.infrastructure.mongodb.audit_event_repository import MongoAuditEventRepository
from app.infrastructure.mongodb.server_repository import MongoServerRepository
from app.infrastructure.renamer.client import RenamerClient
from app.utils.timeutil import utcnow

pytestmark = pytest.mark.integration

_HOUR = timedelta(hours=1)


def _doc(server_id: str, **overrides: Any) -> dict[str, Any]:
    """A free, reachable Dell from OpenManage with an iDRAC IP."""
    doc: dict[str, Any] = {
        "_id": server_id,
        "name": f"old-{server_id}",
        "identity": {"vendor": "dell", "serial": f"SER{server_id}"},
        "network": {"bmc": {"host": f"10.0.0.{len(server_id)}", "host_is_ip": True}},
        "source_provider": "OPENMANAGE",
        "openshift": {"lifecycle_state": "AVAILABLE"},
        "maintenance": {"enabled": False},
        "reachable": True,
    }
    doc.update(overrides)
    return doc


class _Renamer:
    """Answers every batch as renamed (or a dry run), recording the bodies."""

    def __init__(self, status: int = 200) -> None:
        self.status = status
        self.bodies: list[tuple[str, dict[str, Any]]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.bodies.append((request.url.path, body))
        if self.status != 200:
            return httpx.Response(self.status, text="maintenance")
        renamed = []
        failed = []
        for identity in body["idrac_ips"]:
            if identity == "sp-mismatch":
                failed.append(
                    {"idrac_ip": identity, "error": "no conf", "nearest_confs": [{"conf_id": "9"}]}
                )
                continue
            renamed.append(
                {
                    "idrac_ip": identity,
                    "new_profile": "ocp-c1-mtl-tlv-abc",
                    "conf_id": "1",
                    "variant": None,
                    "changed": not body["dry_run"],
                }
            )
        return httpx.Response(200, json={"renamed": renamed, "failed": failed})


def _client(renamer: _Renamer) -> RenamerClient:
    return RenamerClient(
        base_url="https://renamer.test",
        username="svc",
        password="pw",
        verify=False,
        timeout_seconds=5,
        chunk_size=10,
        budget_seconds=600,
        transport=httpx.MockTransport(renamer.handler),
    )


async def _seed(mongo: MongoClientHolder) -> None:
    now = utcnow()
    reserved_until = (now + _HOUR).isoformat().replace("+00:00", "Z")
    await mongo.db["servers"].insert_many(
        [
            _doc("dell"),
            _doc(
                "cisco",
                name="sp-cisco",
                identity={"vendor": "cisco", "serial": "FCH1"},
                network={"bmc": {"host": "ucs-a.example", "host_is_ip": False}},
                source_provider="UCS_CENTRAL",
            ),
            _doc(
                "mismatch",
                name="sp-mismatch",
                identity={"vendor": "cisco", "serial": "FCH2"},
                source_provider="UCS_CENTRAL",
            ),
            _doc("named", name="ocp-c200-mtl-tlv-4b184et"),
            _doc(
                "intersight",
                identity={"vendor": "cisco", "serial": "X"},
                source_provider="INTERSIGHT",
            ),
            _doc("standalone", source_provider="REDFISH_STANDALONE"),
            _doc("nohostip", network={"bmc": {"host": "idrac.example", "host_is_ip": False}}),
            _doc("busy", openshift={"lifecycle_state": "INSTALLED"}),
            _doc("maint", maintenance={"enabled": True}),
            _doc("down", reachable=False),
            _doc("hp", identity={"vendor": "hp", "serial": "H"}, source_provider="ONEVIEW"),
            _doc("held", reservation={"holder": "install-server", "expires_at": reserved_until}),
        ]
    )


async def _sweep(
    mongo: MongoClientHolder,
    renamer: _Renamer,
    *,
    dry_run: bool,
    max_per_run: int = 50,
    skip: frozenset[str] = frozenset(),
) -> Any:
    client = _client(renamer)
    try:
        return await sweep(
            MongoServerRepository(mongo, cursor_secret="test-cursor-secret"),
            AuditService(repo=MongoAuditEventRepository(mongo)),
            client,
            dry_run=dry_run,
            cooldown=timedelta(hours=24),
            max_per_run=max_per_run,
            skip_name_contains=skip,
        )
    finally:
        await client.close()


async def test_dry_run_sends_only_managed_free_servers_and_audits_each(
    mongo_holder: MongoClientHolder,
) -> None:
    """Dell by iDRAC IP, Cisco by profile name, dry_run true; everything else is skipped."""
    await _seed(mongo_holder)
    renamer = _Renamer()
    summary = await _sweep(mongo_holder, renamer, dry_run=True)

    assert renamer.bodies == [
        ("/servers/dell/rename-batch", {"idrac_ips": ["10.0.0.4"], "dry_run": True}),
        (
            "/servers/cisco/rename-batch",
            {"idrac_ips": ["sp-cisco", "sp-mismatch"], "dry_run": True},
        ),
    ]
    assert summary.would_rename == 2
    assert summary.renamed == 0
    assert summary.conf_mismatch == 1
    assert summary.already_in_convention == 1
    assert summary.skipped_unmanaged == 2
    assert summary.skipped_no_bmc_ip == 1
    assert summary.skipped_reserved == 1

    events = (
        await mongo_holder.db["audit_events"]
        .find({"event_type": "RENAME_REQUESTED"})
        .to_list(length=None)
    )
    by_server = {e["server_id"]: e for e in events}
    assert set(by_server) == {"dell", "cisco", "mismatch"}
    assert by_server["dell"]["data"]["outcome"] == "would_rename"
    assert by_server["dell"]["data"]["dry_run"] is True
    assert by_server["dell"]["data"]["name_to"] == "ocp-c1-mtl-tlv-abc"
    assert by_server["mismatch"]["data"]["outcome"] == "conf_mismatch"
    assert by_server["mismatch"]["data"]["nearest_confs"] == [{"conf_id": "9"}]


async def test_cooldown_keeps_an_answered_server_from_being_sent_again(
    mongo_holder: MongoClientHolder,
) -> None:
    """The next run 5 minutes later sends nothing: every answered server is in cooldown."""
    await _seed(mongo_holder)
    await _sweep(mongo_holder, _Renamer(), dry_run=True)
    renamer = _Renamer()
    summary = await _sweep(mongo_holder, renamer, dry_run=False)
    assert renamer.bodies == []
    assert summary.skipped_cooldown == 3


async def test_real_run_is_renamed(mongo_holder: MongoClientHolder) -> None:
    """With report-only off the renamer gets dry_run false and the outcome is renamed."""
    await _seed(mongo_holder)
    summary = await _sweep(mongo_holder, _Renamer(), dry_run=False)
    assert summary.renamed == 2
    event = await mongo_holder.db["audit_events"].find_one({"server_id": "dell"})
    assert event is not None and event["data"]["outcome"] == "renamed"


async def test_an_unsent_chunk_is_not_audited_and_is_retried(
    mongo_holder: MongoClientHolder,
) -> None:
    """A renamer that is down writes no event, so the next run sends the servers again."""
    await _seed(mongo_holder)
    summary = await _sweep(mongo_holder, _Renamer(status=503), dry_run=True)
    assert summary.unsent == 3
    assert await mongo_holder.db["audit_events"].count_documents({}) == 0


async def test_old_documents_expired_reservations_and_old_events(
    mongo_holder: MongoClientHolder,
) -> None:
    """No maintenance key, a null or expired reservation, an event past the cooldown: all sent."""
    now = utcnow()
    expired = (now - _HOUR).isoformat().replace("+00:00", "Z")
    doc = _doc("nomaint")
    del doc["maintenance"]
    await mongo_holder.db["servers"].insert_many(
        [
            doc,
            _doc("nullres", reservation=None),
            _doc("expired", reservation={"holder": "install-server", "expires_at": expired}),
            _doc("oldevent"),
            _doc("newevent"),
        ]
    )
    stamp = (now - timedelta(hours=30)).isoformat().replace("+00:00", "Z")
    fresh = (now - _HOUR).isoformat().replace("+00:00", "Z")
    await mongo_holder.db["audit_events"].insert_many(
        [
            {
                "_id": "e1",
                "event_type": "RENAME_REQUESTED",
                "server_id": "oldevent",
                "created_at": stamp,
            },
            {
                "_id": "e2",
                "event_type": "RENAME_REQUESTED",
                "server_id": "newevent",
                "created_at": fresh,
            },
        ]
    )
    summary = await _sweep(mongo_holder, _Renamer(), dry_run=True)
    assert summary.skipped_cooldown == 1
    assert summary.skipped_reserved == 0
    assert summary.would_rename == 4


async def test_the_cap_leaves_the_rest_for_the_next_run(mongo_holder: MongoClientHolder) -> None:
    """With a cap of 1, the first server by name is sent and the others wait."""
    await _seed(mongo_holder)
    renamer = _Renamer()
    summary = await _sweep(mongo_holder, renamer, dry_run=True, max_per_run=1)
    assert summary.over_cap == 2
    assert renamer.bodies == [
        ("/servers/dell/rename-batch", {"idrac_ips": ["10.0.0.4"], "dry_run": True})
    ]


async def test_two_servers_sharing_an_ip_are_sent_once_and_both_audited(
    mongo_holder: MongoClientHolder,
) -> None:
    """One request per identity; the answer is recorded on every server that has it."""
    await mongo_holder.db["servers"].insert_many([_doc("aa"), _doc("bb")])
    renamer = _Renamer()
    summary = await _sweep(mongo_holder, renamer, dry_run=True)
    assert renamer.bodies == [
        ("/servers/dell/rename-batch", {"idrac_ips": ["10.0.0.2"], "dry_run": True})
    ]
    assert summary.would_rename == 2
    assert await mongo_holder.db["audit_events"].count_documents({}) == 2


async def test_a_server_reserved_after_selection_is_dropped_before_sending(
    mongo_holder: MongoClientHolder,
) -> None:
    """The re-check just before a chunk drops a server reserved or put in maintenance since."""
    await mongo_holder.db["servers"].insert_many([_doc("free"), _doc("late"), _doc("paused")])
    until = (utcnow() + _HOUR).isoformat().replace("+00:00", "Z")
    await mongo_holder.db["servers"].update_one(
        {"_id": "late"},
        {"$set": {"reservation": {"holder": "install-server", "expires_at": until}}},
    )
    await mongo_holder.db["servers"].update_one(
        {"_id": "paused"}, {"$set": {"maintenance": {"enabled": True}}}
    )
    candidates = [
        Candidate(i, f"old-{i}", "dell", "10.0.0.1", None, None) for i in ("free", "late", "paused")
    ]
    summary = SweepSummary()
    repo = MongoServerRepository(mongo_holder, cursor_secret="test-cursor-secret")
    kept = await _still_eligible(repo, candidates, summary)
    assert [c.server_id for c in kept] == ["free"]
    assert summary.dropped_on_recheck == 2


async def test_a_name_with_an_excluded_string_is_never_sent(
    mongo_holder: MongoClientHolder,
) -> None:
    """`-mce-` and `-seed-` keep a server out whatever its case; nothing is audited for it."""
    await mongo_holder.db["servers"].insert_many(
        [
            _doc("a", name="ocp-mce-hub-01"),
            _doc("bb", name="lab-SEED-02"),
            _doc("ccc", name="plain-server"),
        ]
    )
    renamer = _Renamer()
    summary = await _sweep(mongo_holder, renamer, dry_run=True, skip=frozenset({"-mce-", "-seed-"}))
    assert renamer.bodies == [
        ("/servers/dell/rename-batch", {"idrac_ips": ["10.0.0.3"], "dry_run": True})
    ]
    assert summary.skipped_excluded_name == 2
    assert summary.examples["skipped_excluded_name"] == ["lab-SEED-02", "ocp-mce-hub-01"]
    assert (
        await mongo_holder.db["audit_events"].count_documents({"server_id": {"$in": ["a", "bb"]}})
        == 0
    )
