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
        already_installed = OpenShiftLifecycle(
            lifecycle_state=OpenShiftState.INSTALLED, cluster_name="ocp4-tlv"
        )
        first = _server("dup", openshift=already_installed, serial="SN-A")
        second = _server("dup", openshift=already_installed, serial="SN-B")
        repo = FakeRepo([first, second])
        reader = FakeSerialReader({})
        summary = await _service(repo, FakeAudit(), serial_reader=reader).reconcile(
            [_seen("dup", address="10.0.0.5")],
            scope={"openshift.cluster_name": "ocp4-tlv"},
            reported_by="ocp4-tlv",
        )

        assert summary.unmatched == ["dup"]
        assert summary.unresolved == ["dup"]
        assert repo.written == []

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

    async def test_unreadable_serial_skips_freeing_for_the_whole_run(self) -> None:
        renamed = _server(
            "elsewhere",
            openshift=OpenShiftLifecycle(
                lifecycle_state=OpenShiftState.INSTALLED, cluster_name="ocp4-tlv"
            ),
        )
        repo = FakeRepo([renamed])
        reader = FakeSerialReader({})
        summary = await _service(repo, FakeAudit(), serial_reader=reader).reconcile(
            [_seen("ocp-tomer-compute-01", address="10.0.0.5")],
            scope={"openshift.cluster_name": "ocp4-tlv"},
            reported_by="ocp4-tlv",
        )

        assert summary.unresolved == ["ocp-tomer-compute-01"]
        assert summary.freed == 0
        assert repo.written == []

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
