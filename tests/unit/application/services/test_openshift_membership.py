"""Reconciling one cluster's view against the inventory.

The behaviour worth pinning is not "it writes what it saw" — it is what it
does about what it *did not* see. Nothing in Kubernetes reports a removal,
so a server freed from a cluster simply stops appearing, and the only way
to notice is to compare the cluster's list against the servers already
claiming it.

That comparison is also the dangerous part: a job that frees too widely
would mark in-use machines available. So the scope is tested as carefully
as the happy path.

The hardware-serial fallback (ADR-0036) adds a second dangerous part: a
name match that is not unique (a rename, or two servers sharing a name)
must resolve to the *right* server, and an unreadable serial must not be
guessed at — see `TestSerialFallback`.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from structlog.testing import capture_logs

from app.application.services.openshift_membership import OpenShiftMembershipService
from app.domain.enums import OpenShiftState, Vendor
from app.domain.models.audit_event import Actor, ActorType
from app.domain.models.openshift import OpenShiftLifecycle
from app.domain.models.server import Identity, Server
from app.domain.ports.repository import Page
from app.domain.services.normalize import normalize_text
from app.infrastructure.openshift.records import ClusterObservation
from app.utils.timeutil import utcnow

pytestmark = pytest.mark.asyncio


def _server(
    name: str,
    *,
    openshift: OpenShiftLifecycle | None = None,
    serial: str | None = None,
) -> Server:
    """
    A stored server with only the fields this service reads.

    Args:
        name (str): The server's name; `name_normalized` mirrors it.
        openshift (OpenShiftLifecycle | None): Existing membership.
        serial (str | None): The hardware serial, for the fallback tests.

    Returns:
        Server: The stored document.
    """
    now = utcnow()
    return Server(
        _id=f"srv_{name}_{serial or 'noserial'}",
        name=name,
        name_normalized=name.lower(),
        identity=Identity(
            vendor=Vendor.DELL,
            serial=serial,
            serial_normalized=normalize_text(serial),
        ),
        openshift=openshift or OpenShiftLifecycle(),
        created_at=now,
        updated_at=now,
    )


class FakeSerialReader:
    """Answers a fixed serial per address, or `None` for an unreadable node."""

    def __init__(self, answers: dict[str, str | None]) -> None:
        """
        Args:
            answers (dict[str, str | None]): Address -> serial (or `None`
                for a node that fails to read).
        """
        self._answers = answers
        self.calls: list[str] = []

    async def read(self, address: str) -> str | None:
        """
        Args:
            address (str): The node's `InternalIP`.

        Returns:
            str | None: The scripted answer for this address.
        """
        self.calls.append(address)
        return self._answers.get(address)


class FakeRepo:
    """Serves `list_page` from a list, and records every upsert."""

    def __init__(self, servers: list[Server]) -> None:
        """
        Args:
            servers (list[Server]): The stored fleet.
        """
        self.servers = servers
        self.written: list[Server] = []

    async def list_page(self, **kwargs: Any) -> Page:
        """
        Answer either lookup this service makes.

        Args:
            **kwargs (Any): `list_page`'s keyword arguments.

        Returns:
            Page: Matching servers, unpaginated — the fakes here are small
                enough that one page is the whole answer.
        """
        filters: dict[str, Any] = kwargs["filters"]
        matched = [s for s in self.servers if self._matches(s, filters)]
        return Page(items=matched, next_cursor=None, has_more=False, total_count=None)

    @staticmethod
    def _matches(server: Server, filters: dict[str, Any]) -> bool:
        if "name_normalized" in filters:
            return server.name_normalized == filters["name_normalized"]
        if "identity.serial_normalized" in filters:
            return server.identity.serial_normalized == filters["identity.serial_normalized"]
        if "openshift.cluster_name" in filters:
            return server.openshift.cluster_name == filters["openshift.cluster_name"]
        if "openshift.mce_name" in filters:
            return server.openshift.mce_name == filters["openshift.mce_name"]
        return False

    async def upsert_with_revision_check(self, server: Server, **_: Any) -> Server:
        """
        Record a write.

        Args:
            server (Server): The document written.

        Returns:
            Server: The same document.
        """
        self.written.append(server)
        return server


class FakeAudit:
    """Records audit calls without a database."""

    def __init__(self) -> None:
        self.events: list[Any] = []

    async def record(self, event_type: Any, **kwargs: Any) -> None:
        """
        Args:
            event_type (Any): The event type.
            **kwargs (Any): The event payload.
        """
        self.events.append((event_type, kwargs))


def _service(
    repo: FakeRepo, audit: FakeAudit, *, serial_reader: FakeSerialReader | None = None
) -> OpenShiftMembershipService:
    """
    Build the service against the fakes.

    Args:
        repo (FakeRepo): The stored fleet.
        audit (FakeAudit): The audit sink.
        serial_reader (FakeSerialReader | None): The SSH fallback
            (ADR-0036), or `None` to test today's hostname-only behaviour.

    Returns:
        OpenShiftMembershipService: The service under test.
    """
    return OpenShiftMembershipService(
        server_repo=repo,  # type: ignore
        audit=audit,  # type: ignore
        actor=Actor(type=ActorType.SYSTEM, id="openshift:test"),
        serial_reader=serial_reader,
    )


def _seen(
    hostname: str,
    *,
    cluster: str = "ocp4-tlv",
    address: str | None = None,
    serial: str | None = None,
) -> ClusterObservation:
    """
    One node observation.

    Args:
        hostname (str): The reported hostname.
        cluster (str): The reporting cluster.
        address (str | None): The node's `InternalIP`, for the SSH
            fallback (ADR-0036).
        serial (str | None): A serial already known without SSH — the
            agents path's shape, unused by a plain node observation but
            handy for exercising the fallback without a reader.

    Returns:
        ClusterObservation: An INSTALLED observation.
    """
    return ClusterObservation(
        hostname=hostname,
        lifecycle_state=OpenShiftState.INSTALLED,
        cluster_name=cluster,
        address=address,
        serial=serial,
    )


class TestClaiming:
    """What the cluster reported."""

    async def test_a_reported_server_is_marked_installed(self) -> None:
        repo = FakeRepo([_server("ocp4-tlv-worker-01")])
        summary = await _service(repo, FakeAudit()).reconcile(
            [_seen("ocp4-tlv-worker-01")],
            scope={"openshift.cluster_name": "ocp4-tlv"},
            reported_by="ocp4-tlv",
        )

        assert summary.matched == 1
        assert summary.claimed == 1
        assert repo.written[0].openshift.lifecycle_state is OpenShiftState.INSTALLED
        assert repo.written[0].openshift.cluster_name == "ocp4-tlv"

    async def test_an_unmatched_host_is_reported_never_created(self) -> None:
        """The vendor collectors are the only source of what hardware
        exists. A cluster host the inventory has never seen is a gap worth
        surfacing, not a server to invent from a hostname.
        """
        repo = FakeRepo([])
        summary = await _service(repo, FakeAudit()).reconcile(
            [_seen("ocp4-tlv-worker-99")],
            scope={"openshift.cluster_name": "ocp4-tlv"},
            reported_by="ocp4-tlv",
        )

        assert summary.unmatched == ["ocp4-tlv-worker-99"]
        assert repo.written == []

    async def test_each_unmatched_host_is_logged_as_an_error(self) -> None:
        """The job's own log is where an operator finds out a cluster host
        has no server, so each one is an ERROR line naming the host — not
        only a count in the summary.
        """
        repo = FakeRepo([_server("ocp4-tlv-worker-01")])
        with capture_logs() as logs:
            await _service(repo, FakeAudit()).reconcile(
                [
                    _seen("ocp4-tlv-worker-01"),
                    _seen("ocp4-tlv-worker-98"),
                    _seen("ocp4-tlv-worker-99"),
                ],
                scope={"openshift.cluster_name": "ocp4-tlv"},
                reported_by="ocp4-tlv",
            )

        errors = [entry for entry in logs if entry["log_level"] == "error"]
        assert {entry["hostname"] for entry in errors} == {
            "ocp4-tlv-worker-98",
            "ocp4-tlv-worker-99",
        }
        assert all(entry["event"] == "openshift.host_not_in_inventory" for entry in errors)

    async def test_an_unchanged_server_is_not_rewritten(self) -> None:
        """Writing unconditionally would bump `revision` on every server
        four times an hour and fill the audit trail with non-events.
        """
        stored = _server(
            "ocp4-tlv-worker-01",
            openshift=OpenShiftLifecycle(
                lifecycle_state=OpenShiftState.INSTALLED,
                cluster_name="ocp4-tlv",
            ),
        )
        repo = FakeRepo([stored])
        summary = await _service(repo, FakeAudit()).reconcile(
            [_seen("ocp4-tlv-worker-01")],
            scope={"openshift.cluster_name": "ocp4-tlv"},
            reported_by="ocp4-tlv",
        )

        assert summary.claimed == 0
        assert repo.written == []


class TestFreeing:
    """What the cluster stopped reporting — the half nothing else can do."""

    async def test_a_server_this_cluster_no_longer_lists_is_freed(self) -> None:
        """Nothing reports a removal, so this comparison is the only way a
        freed machine ever stops looking in use.
        """
        gone = _server(
            "ocp4-tlv-worker-02",
            openshift=OpenShiftLifecycle(
                lifecycle_state=OpenShiftState.INSTALLED, cluster_name="ocp4-tlv"
            ),
        )
        repo = FakeRepo([_server("ocp4-tlv-worker-01"), gone])
        summary = await _service(repo, FakeAudit()).reconcile(
            [_seen("ocp4-tlv-worker-01")],
            scope={"openshift.cluster_name": "ocp4-tlv"},
            reported_by="ocp4-tlv",
        )

        assert summary.freed == 1
        freed = [s for s in repo.written if s.name == "ocp4-tlv-worker-02"]
        assert freed[0].openshift.lifecycle_state is OpenShiftState.AVAILABLE
        assert freed[0].openshift.cluster_name is None

    async def test_another_cluster_s_servers_are_never_touched(self) -> None:
        """The property that makes per-cluster deployment safe: a job may
        only release servers already naming its own cluster (ADR-0024).
        """
        other = _server(
            "ocp4-nyc-worker-01",
            openshift=OpenShiftLifecycle(
                lifecycle_state=OpenShiftState.INSTALLED, cluster_name="ocp4-nyc"
            ),
        )
        repo = FakeRepo([other])
        summary = await _service(repo, FakeAudit()).reconcile(
            [],
            scope={"openshift.cluster_name": "ocp4-tlv"},
            reported_by="ocp4-tlv",
        )

        assert summary.freed == 0
        assert repo.written == []

    async def test_dry_run_writes_nothing_but_still_counts(self) -> None:
        """`--dry-run` has to answer "what would this change" honestly, or
        it cannot be used to check a first run before it happens.
        """
        gone = _server(
            "ocp4-tlv-worker-02",
            openshift=OpenShiftLifecycle(
                lifecycle_state=OpenShiftState.INSTALLED, cluster_name="ocp4-tlv"
            ),
        )
        repo = FakeRepo([gone])
        summary = await _service(repo, FakeAudit()).reconcile(
            [],
            scope={"openshift.cluster_name": "ocp4-tlv"},
            reported_by="ocp4-tlv",
            dry_run=True,
        )

        assert summary.freed == 1
        assert repo.written == []


class TestAudit:
    """Transitions only."""

    async def test_a_state_change_is_recorded(self) -> None:
        repo = FakeRepo([_server("ocp4-tlv-worker-01")])
        audit = FakeAudit()
        await _service(repo, audit).reconcile(
            [_seen("ocp4-tlv-worker-01")],
            scope={"openshift.cluster_name": "ocp4-tlv"},
            reported_by="ocp4-tlv",
        )

        assert len(audit.events) == 1
        _, payload = audit.events[0]
        assert payload["data"]["from"] == "AVAILABLE"
        assert payload["data"]["to"] == "INSTALLED"

    async def test_a_cluster_rename_writes_without_an_event(self) -> None:
        """Moved cluster but not state: worth persisting, not worth an
        event — `OPENSHIFT_STATE_CHANGED` must mean the state changed.
        """
        stored = _server(
            "ocp4-tlv-worker-01",
            openshift=OpenShiftLifecycle(
                lifecycle_state=OpenShiftState.INSTALLED,
                cluster_name="ocp4-tlv-old",
            ),
        )
        repo = FakeRepo([stored])
        audit = FakeAudit()
        await _service(repo, audit).reconcile(
            [_seen("ocp4-tlv-worker-01")],
            scope={"openshift.cluster_name": "ocp4-tlv"},
            reported_by="ocp4-tlv",
        )

        assert len(repo.written) == 1
        assert audit.events == []


class TestSerialFallback:
    """ADR-0036: a hostname that is not a unique match falls back to the hardware serial."""

    async def test_a_renamed_server_is_matched_by_serial_and_flagged(self) -> None:
        renamed = _server("ocp-toto-compute-01", serial="SN123")
        repo = FakeRepo([renamed])
        reader = FakeSerialReader({"10.0.0.5": "SN123"})
        summary = await _service(repo, FakeAudit(), serial_reader=reader).reconcile(
            [_seen("ocp-tomer-compute-01", address="10.0.0.5")],
            scope={"openshift.cluster_name": "ocp4-tlv"},
            reported_by="ocp4-tlv",
        )

        assert summary.matched == 1
        assert summary.matched_by_serial == 1
        assert summary.unmatched == []
        assert repo.written[0].name == "ocp-toto-compute-01"
        assert repo.written[0].openshift.lifecycle_state is OpenShiftState.INSTALLED
        assert repo.written[0].openshift.reported_name == "ocp-tomer-compute-01"

    async def test_the_mismatch_clears_once_the_name_is_fixed(self) -> None:
        fixed = _server(
            "ocp-tomer-compute-01",
            openshift=OpenShiftLifecycle(
                lifecycle_state=OpenShiftState.INSTALLED,
                cluster_name="ocp4-tlv",
                reported_name="ocp-tomer-compute-01",
            ),
        )
        repo = FakeRepo([fixed])
        summary = await _service(repo, FakeAudit()).reconcile(
            [_seen("ocp-tomer-compute-01")],
            scope={"openshift.cluster_name": "ocp4-tlv"},
            reported_by="ocp4-tlv",
        )

        assert summary.matched_by_serial == 0
        assert repo.written[0].openshift.reported_name is None

    async def test_duplicate_hostname_is_disambiguated_by_serial(self) -> None:
        wrong = _server("ocp-tomer-compute-01", serial="SN-WRONG")
        right = _server("ocp-tomer-compute-01", serial="SN-RIGHT")
        repo = FakeRepo([wrong, right])
        reader = FakeSerialReader({"10.0.0.5": "SN-RIGHT"})
        summary = await _service(repo, FakeAudit(), serial_reader=reader).reconcile(
            [_seen("ocp-tomer-compute-01", address="10.0.0.5")],
            scope={"openshift.cluster_name": "ocp4-tlv"},
            reported_by="ocp4-tlv",
        )

        assert summary.matched_by_serial == 1
        assert len(repo.written) == 1
        assert repo.written[0].identity.serial == "SN-RIGHT"
        assert repo.written[0].openshift.reported_name is None

    async def test_a_previously_wrong_duplicate_is_freed_the_same_run_the_right_one_is_claimed(
        self,
    ) -> None:
        """Self-healing: a wrong pre-existing claim from before this
        fallback existed is freed the same run the right server is
        claimed, with no separate cleanup pass needed.
        """
        already_wrong = _server(
            "ocp-tomer-compute-01",
            serial="SN-WRONG",
            openshift=OpenShiftLifecycle(
                lifecycle_state=OpenShiftState.INSTALLED, cluster_name="ocp4-tlv"
            ),
        )
        right = _server("ocp-tomer-compute-01", serial="SN-RIGHT")
        repo = FakeRepo([already_wrong, right])
        reader = FakeSerialReader({"10.0.0.5": "SN-RIGHT"})
        summary = await _service(repo, FakeAudit(), serial_reader=reader).reconcile(
            [_seen("ocp-tomer-compute-01", address="10.0.0.5")],
            scope={"openshift.cluster_name": "ocp4-tlv"},
            reported_by="ocp4-tlv",
        )

        assert summary.matched_by_serial == 1
        assert summary.freed == 1
        written_by_name = {s.identity.serial: s.openshift.lifecycle_state for s in repo.written}
        assert written_by_name["SN-RIGHT"] is OpenShiftState.INSTALLED
        assert written_by_name["SN-WRONG"] is OpenShiftState.AVAILABLE

    async def test_duplicate_hostname_with_unreadable_serial_claims_neither(self) -> None:
        """The ambiguous pair is held back; an unrelated server is freed
        normally in the same run (2026-09-27 correction: scoped, not
        cluster-wide).
        """
        already_installed = OpenShiftLifecycle(
            lifecycle_state=OpenShiftState.INSTALLED, cluster_name="ocp4-tlv"
        )
        first = _server("dup", openshift=already_installed, serial="SN-A")
        second = _server("dup", openshift=already_installed, serial="SN-B")
        unrelated = _server("unrelated-worker-03", openshift=already_installed)
        repo = FakeRepo([first, second, unrelated])
        reader = FakeSerialReader({})
        summary = await _service(repo, FakeAudit(), serial_reader=reader).reconcile(
            [_seen("dup", address="10.0.0.5")],
            scope={"openshift.cluster_name": "ocp4-tlv"},
            reported_by="ocp4-tlv",
        )

        assert summary.unmatched == ["dup"]
        assert summary.unresolved == ["dup"]
        assert summary.freed == 1
        assert repo.written == [unrelated]
        assert unrelated.openshift.lifecycle_state is OpenShiftState.AVAILABLE

    async def test_duplicate_hostname_with_no_reader_keeps_first_by_name_pick(self) -> None:
        first = _server("dup", serial="SN-A")
        second = _server("dup", serial="SN-B")
        repo = FakeRepo([first, second])
        summary = await _service(repo, FakeAudit()).reconcile(
            [_seen("dup")],
            scope={"openshift.cluster_name": "ocp4-tlv"},
            reported_by="ocp4-tlv",
        )

        assert summary.matched == 1
        assert summary.matched_by_serial == 0
        assert len(repo.written) == 1

    async def test_a_miss_with_unreadable_serial_does_not_block_freeing_elsewhere(self) -> None:
        """2026-09-27 correction: one permanently-unreachable node used to
        freeze the *entire* cluster's freeing indefinitely. A miss has no
        candidate to protect, so it protects nothing else.
        """
        unrelated = _server(
            "elsewhere",
            openshift=OpenShiftLifecycle(
                lifecycle_state=OpenShiftState.INSTALLED, cluster_name="ocp4-tlv"
            ),
        )
        repo = FakeRepo([unrelated])
        reader = FakeSerialReader({})
        summary = await _service(repo, FakeAudit(), serial_reader=reader).reconcile(
            [_seen("ocp-tomer-compute-01", address="10.0.0.5")],
            scope={"openshift.cluster_name": "ocp4-tlv"},
            reported_by="ocp4-tlv",
        )

        assert summary.unresolved == ["ocp-tomer-compute-01"]
        assert summary.freed == 1
        assert repo.written[0].name == "elsewhere"
        assert repo.written[0].openshift.lifecycle_state is OpenShiftState.AVAILABLE

    async def test_ambiguous_serial_match_stays_unmatched(self) -> None:
        one = _server("srv-1", serial="SN-DUP")
        two = _server("srv-2", serial="SN-DUP")
        repo = FakeRepo([one, two])
        reader = FakeSerialReader({"10.0.0.5": "SN-DUP"})
        summary = await _service(repo, FakeAudit(), serial_reader=reader).reconcile(
            [_seen("ocp-tomer-compute-01", address="10.0.0.5")],
            scope={"openshift.cluster_name": "ocp4-tlv"},
            reported_by="ocp4-tlv",
        )

        assert summary.unmatched == ["ocp-tomer-compute-01"]
        assert summary.unresolved == []
        assert repo.written == []

    async def test_placeholder_serial_is_treated_as_unreadable(self) -> None:
        repo = FakeRepo([_server("ocp-toto-compute-01", serial="0123456789")])
        reader = FakeSerialReader({"10.0.0.5": None})
        summary = await _service(repo, FakeAudit(), serial_reader=reader).reconcile(
            [_seen("ocp-tomer-compute-01", address="10.0.0.5")],
            scope={"openshift.cluster_name": "ocp4-tlv"},
            reported_by="ocp4-tlv",
        )

        assert summary.unresolved == ["ocp-tomer-compute-01"]

    async def test_agent_source_resolves_serial_with_no_reader(self) -> None:
        """Agents carry their own serial (assisted-service inventory); no SSH needed."""
        renamed = _server("ocp-toto-compute-01", serial="SN123")
        repo = FakeRepo([renamed])
        summary = await _service(repo, FakeAudit()).reconcile(
            [_seen("ocp-tomer-compute-01", serial="SN123")],
            scope={"openshift.cluster_name": "ocp4-tlv"},
            reported_by="ocp4-tlv",
        )

        assert summary.matched_by_serial == 1
        assert repo.written[0].openshift.reported_name == "ocp-tomer-compute-01"

    async def test_a_change_to_reported_name_alone_is_written(self) -> None:
        stored = _server(
            "ocp-toto-compute-01",
            serial="SN123",
            openshift=OpenShiftLifecycle(
                lifecycle_state=OpenShiftState.INSTALLED,
                cluster_name="ocp4-tlv",
                reported_name="ocp-old-name",
            ),
        )
        repo = FakeRepo([stored])
        reader = FakeSerialReader({"10.0.0.5": "SN123"})
        summary = await _service(repo, FakeAudit(), serial_reader=reader).reconcile(
            [_seen("ocp-tomer-compute-01", address="10.0.0.5")],
            scope={"openshift.cluster_name": "ocp4-tlv"},
            reported_by="ocp4-tlv",
        )

        assert summary.claimed == 1
        assert repo.written[0].openshift.reported_name == "ocp-tomer-compute-01"

    async def test_an_unrelated_down_node_does_not_block_a_duplicate_from_self_healing(
        self,
    ) -> None:
        """The reported incident: an unrelated down node used to block
        freeing for the whole cluster (ADR-0036's 2026-09-27 update).
        """
        wrong_hp = _server(
            "ocp-tomer-compute-06",
            serial="HP-SERIAL",
            openshift=OpenShiftLifecycle(
                lifecycle_state=OpenShiftState.INSTALLED, cluster_name="ocp4-tlv"
            ),
        )
        right_cisco = _server("ocp-tomer-compute-06", serial="CISCO-SERIAL")
        repo = FakeRepo([wrong_hp, right_cisco])
        reader = FakeSerialReader({"10.0.0.6": "CISCO-SERIAL"})  # compute-80 stays unanswered
        summary = await _service(repo, FakeAudit(), serial_reader=reader).reconcile(
            [
                _seen("ocp-tomer-compute-06", address="10.0.0.6"),
                _seen("compute-80", address="10.0.0.80"),
            ],
            scope={"openshift.cluster_name": "ocp4-tlv"},
            reported_by="ocp4-tlv",
        )

        assert summary.matched_by_serial == 1
        assert summary.unresolved == ["compute-80"]
        written_by_serial = {s.identity.serial: s.openshift.lifecycle_state for s in repo.written}
        assert written_by_serial["CISCO-SERIAL"] is OpenShiftState.INSTALLED
        assert written_by_serial["HP-SERIAL"] is OpenShiftState.AVAILABLE


class TestAgentAlwaysResolvesBySerial:
    """2026-09-28: an Agent's free serial is authoritative even over a
    unique-looking hostname — closing half of ADR-0036's "known gap."
    """

    async def test_a_unique_but_wrong_hostname_match_is_overridden_by_serial(self) -> None:
        """The exact case a hostname-first design can never catch: the name
        genuinely is unique in the fleet, but it belongs to the wrong
        physical machine — only the serial can prove that.
        """
        looks_right_by_name = _server("ocp-tomer-compute-01", serial="SN-WRONG")
        actually_this_one = _server("ocp-toto-compute-01", serial="SN-RIGHT")
        repo = FakeRepo([looks_right_by_name, actually_this_one])
        await _service(repo, FakeAudit()).reconcile(
            [_seen("ocp-tomer-compute-01", serial="SN-RIGHT")],
            scope={"openshift.cluster_name": "ocp4-tlv"},
            reported_by="ocp4-tlv",
        )

        assert len(repo.written) == 1
        assert repo.written[0].identity.serial == "SN-RIGHT"
        assert repo.written[0].openshift.reported_name == "ocp-tomer-compute-01"

    async def test_agreement_between_name_and_serial_does_not_count_as_a_mismatch(self) -> None:
        """The routine case: an agent resolves by serial every time, but
        `matched_by_serial` must not fire on ordinary traffic — only on a
        genuine name disagreement.
        """
        server = _server("ocp-tomer-compute-01", serial="SN123")
        repo = FakeRepo([server])
        summary = await _service(repo, FakeAudit()).reconcile(
            [_seen("ocp-tomer-compute-01", serial="SN123")],
            scope={"openshift.cluster_name": "ocp4-tlv"},
            reported_by="ocp4-tlv",
        )

        assert summary.matched == 1
        assert summary.matched_by_serial == 0
        assert repo.written[0].openshift.reported_name is None

    async def test_an_unmatched_serial_falls_back_to_a_unique_hostname(self) -> None:
        """A brand-new agent whose serial is not yet in inventory must not
        be stranded when its hostname is otherwise unambiguous.
        """
        server = _server("ocp-tomer-compute-01", serial="SN-OLD")
        repo = FakeRepo([server])
        summary = await _service(repo, FakeAudit()).reconcile(
            [_seen("ocp-tomer-compute-01", serial="SN-NEVER-SEEN")],
            scope={"openshift.cluster_name": "ocp4-tlv"},
            reported_by="ocp4-tlv",
        )

        assert summary.matched == 1
        assert repo.written[0].name == "ocp-tomer-compute-01"

    async def test_an_unmatched_serial_and_a_duplicate_hostname_is_not_guessed_at(self) -> None:
        """No safe fallback exists here: the serial found nothing, and the
        name does not narrow it to one machine either.
        """
        first = _server("dup", serial="SN-A")
        second = _server("dup", serial="SN-B")
        repo = FakeRepo([first, second])
        summary = await _service(repo, FakeAudit()).reconcile(
            [_seen("dup", serial="SN-NEVER-SEEN")],
            scope={"openshift.cluster_name": "ocp4-tlv"},
            reported_by="ocp4-tlv",
        )

        assert summary.unmatched == ["dup"]
        assert repo.written == []


class TestContestedClaim:
    """ADR-0041: two jobs flipping one server back and forth is flagged; a real move is not."""

    @staticmethod
    async def _claim(repo: FakeRepo, cluster: str, hostname: str = "ocp4-x-worker-01") -> None:
        await _service(repo, FakeAudit()).reconcile(
            [_seen(hostname, cluster=cluster)],
            scope={"openshift.cluster_name": cluster},
            reported_by=cluster,
        )

    async def test_flipping_back_to_the_earlier_claimant_marks_it_contested(self) -> None:
        server = _server("ocp4-x-worker-01")
        repo = FakeRepo([server])

        await self._claim(repo, "ocp4-a")
        await self._claim(repo, "ocp4-b")
        assert server.openshift.contested_with is None

        await self._claim(repo, "ocp4-a")
        assert server.openshift.contested_with == "ocp4-b"

        await self._claim(repo, "ocp4-b")
        assert server.openshift.contested_with == "ocp4-a"

    async def test_the_other_claimants_hostname_is_recorded_when_it_differs(self) -> None:
        server = _server(
            "ocp4-x-worker-01",
            openshift=OpenShiftLifecycle(
                lifecycle_state=OpenShiftState.INSTALLED,
                cluster_name="ocp4-b",
                reported_by_agent_id="ocp4-b",
                reported_name="old-name-07",
                previous_reporter="ocp4-a",
                claim_changed_at=utcnow() - timedelta(minutes=15),
            ),
        )
        repo = FakeRepo([server])

        await self._claim(repo, "ocp4-a")

        assert server.openshift.contested_with == "ocp4-b"
        assert server.openshift.contested_name == "old-name-07"

    async def test_the_other_claimant_defaults_to_the_servers_own_name(self) -> None:
        server = _server("ocp4-x-worker-01")
        repo = FakeRepo([server])
        for cluster in ("ocp4-a", "ocp4-b", "ocp4-a"):
            await self._claim(repo, cluster)

        assert server.openshift.contested_name == "ocp4-x-worker-01"

    async def test_the_same_server_under_different_names_in_upi_and_mce_shows_both(self) -> None:
        """One serial, three names: the inventory's, the UPI cluster's, the MCE's."""
        server = _server("inventory-name-01", serial="SN1")
        repo = FakeRepo([server])
        reader = FakeSerialReader({"10.0.0.1": "SN1"})
        node = _seen("upi-host-1", cluster="upi-a", address="10.0.0.1")
        agent = ClusterObservation(
            hostname="mce-host-9",
            lifecycle_state=OpenShiftState.INSTALLED_TO_INVENTORY,
            mce_name="mce-x",
            serial="SN1",
        )

        for who in ("agent", "node", "agent"):
            if who == "node":
                await _service(repo, FakeAudit(), serial_reader=reader).reconcile(
                    [node], scope={"openshift.cluster_name": "upi-a"}, reported_by="upi-a"
                )
            else:
                await _service(repo, FakeAudit()).reconcile(
                    [agent], scope={"openshift.mce_name": "mce-x"}, reported_by="mce-x"
                )

        assert server.openshift.contested_with == "upi-a"
        assert server.openshift.contested_name == "upi-host-1"
        assert server.openshift.reported_name == "mce-host-9"

    async def test_a_real_move_between_clusters_is_never_flagged(self) -> None:
        server = _server("ocp4-x-worker-01")
        repo = FakeRepo([server])

        await self._claim(repo, "ocp4-a")
        await self._claim(repo, "ocp4-b")
        for _ in range(3):
            await self._claim(repo, "ocp4-b")

        assert server.openshift.cluster_name == "ocp4-b"
        assert server.openshift.contested_with is None

    async def test_an_mce_agent_moving_to_a_upi_cluster_is_never_flagged(self) -> None:
        server = _server("ocp4-x-worker-01", serial="SN1")
        repo = FakeRepo([server])
        agent = ClusterObservation(
            hostname="ocp4-x-worker-01",
            lifecycle_state=OpenShiftState.INSTALLED_TO_INVENTORY,
            mce_name="mce-x",
            serial="SN1",
        )
        await _service(repo, FakeAudit()).reconcile(
            [agent], scope={"openshift.mce_name": "mce-x"}, reported_by="mce-x"
        )
        await self._claim(repo, "upi-x")
        await self._claim(repo, "upi-x")

        assert server.openshift.cluster_name == "upi-x"
        assert server.openshift.contested_with is None

    async def test_a_stale_agent_fighting_a_node_is_flagged(self) -> None:
        """The 2026-10-04 incident: an unbound Agent CR and the real node."""
        server = _server("ocp4-x-worker-01", serial="SN1")
        repo = FakeRepo([server])
        agent = ClusterObservation(
            hostname="ocp4-x-worker-01",
            lifecycle_state=OpenShiftState.INSTALLED_TO_INVENTORY,
            mce_name="mce-x",
            serial="SN1",
        )
        for _ in range(2):
            await self._claim(repo, "ocp4-five")
            await _service(repo, FakeAudit()).reconcile(
                [agent], scope={"openshift.mce_name": "mce-x"}, reported_by="mce-x"
            )

        assert server.openshift.contested_with == "ocp4-five"

    async def test_the_flag_is_logged_once_with_both_claimants(self) -> None:
        repo = FakeRepo([_server("ocp4-x-worker-01")])
        with capture_logs() as logs:
            for cluster in ("ocp4-a", "ocp4-b", "ocp4-a", "ocp4-a"):
                await self._claim(repo, cluster)

        flagged = [entry for entry in logs if entry["event"] == "openshift.contested_claim"]
        assert len(flagged) == 1
        assert flagged[0]["claimed_by"] == "ocp4-a"
        assert flagged[0]["also_claimed_by"] == "ocp4-b"

    async def test_the_flag_clears_once_the_flipping_has_stopped_for_an_hour(self) -> None:
        old = utcnow() - timedelta(hours=2)
        server = _server(
            "ocp4-x-worker-01",
            openshift=OpenShiftLifecycle(
                lifecycle_state=OpenShiftState.INSTALLED,
                cluster_name="ocp4-a",
                reported_by_agent_id="ocp4-a",
                previous_reporter="ocp4-b",
                claim_changed_at=old,
                contested_with="ocp4-b",
            ),
        )
        repo = FakeRepo([server])

        await self._claim(repo, "ocp4-a")

        assert server.openshift.contested_with is None

    async def test_moving_back_after_more_than_an_hour_is_a_move_not_a_contest(self) -> None:
        server = _server(
            "ocp4-x-worker-01",
            openshift=OpenShiftLifecycle(
                lifecycle_state=OpenShiftState.INSTALLED,
                cluster_name="ocp4-b",
                reported_by_agent_id="ocp4-b",
                previous_reporter="ocp4-a",
                claim_changed_at=utcnow() - timedelta(hours=2),
            ),
        )
        repo = FakeRepo([server])

        await self._claim(repo, "ocp4-a")

        assert server.openshift.cluster_name == "ocp4-a"
        assert server.openshift.contested_with is None

    async def test_a_freed_server_forgets_its_history(self) -> None:
        server = _server("ocp4-x-worker-01")
        repo = FakeRepo([server])
        await self._claim(repo, "ocp4-a")
        await self._claim(repo, "ocp4-b")
        await _service(repo, FakeAudit()).reconcile(
            [], scope={"openshift.cluster_name": "ocp4-b"}, reported_by="ocp4-b"
        )
        await self._claim(repo, "ocp4-a")

        assert server.openshift.contested_with is None


def _agent(mce: str, cluster: str | None = None) -> ClusterObservation:
    """
    One MCE agent observation for the shared test server.

    Args:
        mce (str): The reporting MCE.
        cluster (str | None): The hosted cluster it is bound to, or `None` if unbound.

    Returns:
        ClusterObservation: The agent's claim.
    """
    return ClusterObservation(
        hostname="ocp4-x-worker-01",
        lifecycle_state=(
            OpenShiftState.INSTALLED if cluster else OpenShiftState.INSTALLED_TO_INVENTORY
        ),
        cluster_name=cluster,
        mce_name=mce,
        serial="SN1",
    )


# One claimant per kind of source: a UPI cluster's nodes job, an MCE's unbound agent, an
# MCE's agent bound to a hosted cluster. Two of each, so every pairing exists.
_CLAIMANTS: dict[str, tuple[str, str, ClusterObservation]] = {
    "upi-a": ("upi-a", "openshift.cluster_name", _seen("ocp4-x-worker-01", cluster="upi-a")),
    "upi-b": ("upi-b", "openshift.cluster_name", _seen("ocp4-x-worker-01", cluster="upi-b")),
    "mce-x unbound": ("mce-x", "openshift.mce_name", _agent("mce-x")),
    "mce-y unbound": ("mce-y", "openshift.mce_name", _agent("mce-y")),
    "mce-x hosted h1": ("mce-x", "openshift.mce_name", _agent("mce-x", "h1")),
    "mce-y hosted h2": ("mce-y", "openshift.mce_name", _agent("mce-y", "h2")),
}
_PAIRS = [(a, b) for a in _CLAIMANTS for b in _CLAIMANTS if a != b]


async def _report(repo: FakeRepo, who: str) -> None:
    reporter, scope_key, observation = _CLAIMANTS[who]
    await _service(repo, FakeAudit()).reconcile(
        [observation], scope={scope_key: reporter}, reported_by=reporter
    )


class TestEveryPairingOfClaimants:
    """ADR-0041: any two sources fighting over one server are flagged; a real move never is."""

    @pytest.mark.parametrize(("first", "second"), _PAIRS)
    async def test_two_sources_flipping_are_flagged(self, first: str, second: str) -> None:
        server = _server("ocp4-x-worker-01", serial="SN1")
        repo = FakeRepo([server])

        for who in (first, second, first):
            await _report(repo, who)

        assert server.openshift.contested_with is not None

    @pytest.mark.parametrize(("first", "second"), _PAIRS)
    async def test_a_move_from_one_source_to_another_is_not_flagged(
        self, first: str, second: str
    ) -> None:
        server = _server("ocp4-x-worker-01", serial="SN1")
        repo = FakeRepo([server])

        for who in (first, second, second, second):
            await _report(repo, who)

        assert server.openshift.contested_with is None

    async def test_contested_with_names_the_other_claim(self) -> None:
        server = _server("ocp4-x-worker-01", serial="SN1")
        repo = FakeRepo([server])
        for who in ("mce-x hosted h1", "upi-a", "mce-x hosted h1"):
            await _report(repo, who)

        assert server.openshift.contested_with == "upi-a"
        await _report(repo, "upi-a")
        assert server.openshift.contested_with == "mce-x/h1"

    async def test_two_agents_for_one_serial_on_one_mce_are_flagged(self) -> None:
        """A stale unbound Agent CR beside the live bound one: flagged on the second run."""
        server = _server("ocp4-x-worker-01", serial="SN1")
        repo = FakeRepo([server])

        for _ in range(2):
            await _service(repo, FakeAudit()).reconcile(
                [_agent("mce-x"), _agent("mce-x", "h1")],
                scope={"openshift.mce_name": "mce-x"},
                reported_by="mce-x",
            )

        assert server.openshift.contested_with == "mce-x"

    async def test_a_hosted_cluster_reported_by_its_own_job_and_its_mce_is_not_contested(
        self,
    ) -> None:
        """Both name cluster `h1`: they agree, so this is one claim reported twice."""
        server = _server("ocp4-x-worker-01", serial="SN1")
        repo = FakeRepo([server])
        own_job = _seen("ocp4-x-worker-01", cluster="h1")

        for _ in range(3):
            await _service(repo, FakeAudit()).reconcile(
                [own_job], scope={"openshift.cluster_name": "h1"}, reported_by="h1"
            )
            await _report(repo, "mce-x hosted h1")

        assert server.openshift.contested_with is None
