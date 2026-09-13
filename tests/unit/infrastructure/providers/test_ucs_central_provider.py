"""`app.infrastructure.providers.ucs_central.provider`.

The only Cisco collector entry point: UCS Central is used to enumerate
registered domains and their service-profile names, and each domain's real
inventory is then read live from that domain's own UCS Manager through
`..ucs_manager` unchanged.

Two behaviours carry essentially all the risk here, and both are locked
down below rather than left to a live run:

1. **Domain pruning must never be stricter than the real name filter.**
   `tools.run_collector._NameFilteredProvider` is the only thing entitled
   to decide which servers get ingested, and it uses `re.search`. A pruner
   using `re.match` — or one that pruned a domain it has no profile names
   for — would silently drop whole domains, with an inexplicably small
   inventory as the only symptom.
2. **A domain with no known profiles is collected, not skipped.**
   ADR-0014's open question is whether Central's `lsServer` lists
   domain-*local* service profiles at all. Pruning on absent evidence would
   silently drop exactly the domains that question is about, so absence of
   evidence is treated as "collect it and let the name filter decide".

Everything runs against fakes injected through `client_factory` /
`domain_provider_factory` — never a live SDK. The per-domain UCS Manager
data path itself is covered by `test_ucs_manager_provider.py` and
`test_ucs_manager_mapping.py`, which are unchanged: this file is about the
multi-domain orchestration layered on top of them.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncGenerator
from types import SimpleNamespace
from typing import Any

import pytest
from structlog.testing import capture_logs
from structlog.typing import EventDict

from app.domain.enums import ManagerType
from app.domain.models.common import AuditFields
from app.domain.models.manager import Manager
from app.domain.ports.credentials import ManagerConnection
from app.domain.ports.provider import ProviderServer, ServerIdentity, ServerInventoryProvider
from app.infrastructure.providers.ucs_central.provider import (
    DomainTarget,
    UcsCentralProvider,
    central_external_id,
    domain_id_from_dn,
    domains_to_collect,
)

pytestmark = pytest.mark.unit


# `ComputeSystem` carries `id`/`name`/`address` and `LsServer` carries
# `domain`, confirmed against the installed `ucscsdk==0.9.0.10` mometa.


def _domain(domain_id: str, name: str, **props: Any) -> SimpleNamespace:
    defaults: dict[str, Any] = {
        "dn": f"compute/sys-{domain_id}",
        "id": domain_id,
        "name": name,
        "address": f"10.0.0.{domain_id[-1]}",
        "inventory_status": "fine",
        "last_refreshed_ts": "2026-08-17T00:00:00",
        "total_physical_cnt": "1",
    }
    defaults.update(props)
    return SimpleNamespace(**defaults)


def _profile(name: str, domain: str, **props: Any) -> SimpleNamespace:
    defaults: dict[str, Any] = {
        "dn": f"org-root/ls-{name}",
        "name": name,
        "type": "instance",
        "domain": domain,
    }
    defaults.update(props)
    return SimpleNamespace(**defaults)


def _manager() -> Manager:
    return Manager(
        _id="mgr_ucs_central",
        name="ucs-central",
        type=ManagerType.UCS_CENTRAL,
        endpoint="central.example.com",
        audit=AuditFields.new(),
    )


class FakeCentralClient:
    """Stands in for `UcsCentralClient`, recording the call sequence so the
    session lifecycle and the query count can both be asserted.
    """

    def __init__(self, responses: dict[str, list[Any]] | None = None) -> None:
        self._responses = responses or {}
        self.calls: list[str] = []

    async def login(self) -> None:
        self.calls.append("login")

    async def logout(self) -> None:
        self.calls.append("logout")

    async def query_classid(self, class_id: str) -> list[Any]:
        self.calls.append(f"query:{class_id}")
        return list(self._responses.get(class_id, []))

    @property
    def queried(self) -> list[str]:
        return [c.removeprefix("query:") for c in self.calls if c.startswith("query:")]


class FakeDomainProvider(ServerInventoryProvider):
    """Stands in for a per-domain `UcsManagerProvider`."""

    def __init__(
        self,
        servers: list[ProviderServer],
        *,
        error: Exception | None = None,
        delay: float = 0.0,
    ) -> None:
        super().__init__()
        self._servers = servers
        self._error = error
        # Only the streaming-order test sets this.
        self._delay = delay
        # Set in `_list_servers`'s own `finally`, so a test can tell a
        # cancelled-and-drained domain (the real equivalent is
        # `client.logout()`) apart from one abandoned mid-collection.
        self.torn_down = False

    async def health_check(self) -> None:
        return None

    async def get_one(self, identity: ServerIdentity) -> ProviderServer | None:
        """Return the stored server whose `external_id` matches, or `None`."""
        if self._error is not None:
            raise self._error
        return next((s for s in self._servers if s.external_id == identity.external_id), None)

    async def _list_servers(self) -> AsyncGenerator[ProviderServer, None]:
        try:
            if self._delay:
                await asyncio.sleep(self._delay)
            if self._error is not None:
                raise self._error
            for server in self._servers:
                yield server
        finally:
            self.torn_down = True


def _server(external_id: str, name: str) -> ProviderServer:
    """A UCS-Manager-shaped `ProviderServer`: its `external_id` is the
    domain-*local* DN (`sys/...`), which is exactly why the collector has
    to re-root it per domain.
    """
    return ProviderServer(
        external_id=external_id,
        vendor="cisco",
        name=name,
        manager_id="mgr_ucs_central",
    )


def _provider(
    client: FakeCentralClient,
    domain_providers: dict[str, Any] | None = None,
    *,
    name_pattern: str = "^ocp",
    concurrency: int = 4,
) -> UcsCentralProvider:
    """`domain_providers` is keyed by `DomainTarget.endpoint` — the value
    the collector actually logs into.
    """
    providers = domain_providers or {}
    return UcsCentralProvider(
        manager=_manager(),
        credentials=ManagerConnection(
            endpoint="central.example.com", username="central-admin", password="secret"
        ),
        timeout_seconds=5.0,
        domain_login=("domain-admin", "domain-secret"),
        name_pattern=name_pattern,
        concurrency=concurrency,
        client_factory=lambda: client,
        domain_provider_factory=lambda target: providers.get(
            target.endpoint, FakeDomainProvider([])
        ),
    )


async def _collect(provider: UcsCentralProvider) -> list[ProviderServer]:
    return [ps async for ps in provider.collect()]


async def _collect_with_logs(
    provider: UcsCentralProvider,
) -> tuple[list[ProviderServer], list[EventDict]]:
    """Collect, capturing structlog events as dicts via `capture_logs` —
    whether events render to stdout or stdlib `logging` depends on which
    config is active, so assert on the payload, not rendered text.
    """
    with capture_logs() as events:
        servers = [ps async for ps in provider.collect()]
    return servers, events


def _events(events: list[EventDict], name: str) -> list[EventDict]:
    return [e for e in events if e.get("event") == name]


@pytest.mark.parametrize(
    ("dn", "expected"),
    [
        ("compute/sys-1009/chassis-1/blade-1", "1009"),
        ("compute/sys-1009/rack-unit-3", "1009"),
        ("compute/sys-1009", "1009"),
        # Global objects (an org's profiles) belong to no domain.
        ("org-root/ls-ocp4-prod-tlv-infra-01", None),
        ("extpol/reg/clients/client-1009", None),
        ("compute", None),
        ("", None),
    ],
)
def test_domain_id_from_dn(dn: str, expected: str | None) -> None:
    """Still the inverse of `central_external_id`, and still used by
    `tools/verify_ucs_central.py` to attribute an object to its domain.
    """
    assert domain_id_from_dn(dn) == expected


class TestCentralExternalId:
    """A UCS Manager DN is domain-local, so `sys/chassis-1/blade-1` repeats
    in every domain; without re-rooting, every domain's servers would share
    one `Server.external_ids` namespace under `mgr_ucs_central`.
    """

    @pytest.mark.parametrize(
        ("external_id", "expected"),
        [
            ("sys/chassis-1/blade-1", "compute/sys-1009/chassis-1/blade-1"),
            ("sys/rack-unit-3", "compute/sys-1009/rack-unit-3"),
            # Already qualified — rewriting again would double the prefix.
            ("compute/sys-1010/chassis-1/blade-1", "compute/sys-1010/chassis-1/blade-1"),
            # Org DNs are global in Central.
            ("org-root/ls-ocp4-prod-tlv-infra-01", "org-root/ls-ocp4-prod-tlv-infra-01"),
            ("", ""),
        ],
    )
    def test_reroots_only_domain_local_dns(self, external_id: str, expected: str) -> None:
        assert central_external_id(external_id, domain_id="1009") == expected

    def test_round_trips_through_domain_id_from_dn(self) -> None:
        """The two halves must agree, or a collected server's owning domain
        stops being recoverable from its external id.
        """
        rerooted = central_external_id("sys/chassis-1/blade-1", domain_id="1009")
        assert domain_id_from_dn(rerooted) == "1009"

    def test_does_not_match_a_sibling_prefix(self) -> None:
        """`sysdebug/...` starts with "sys" but is not under `sys/`."""
        assert central_external_id("sysdebug/foo", domain_id="1009") == "sysdebug/foo"


class TestDomainsToCollect:
    def test_skips_a_domain_whose_profiles_all_fail_the_pattern(self) -> None:
        domains = [_domain("1009", "dc1-a"), _domain("1010", "dc1-b")]
        ls_servers = [
            _profile("ocp4-prod-tlv-infra-01", "dc1-a"),
            _profile("vmware-esx-07", "dc1-b"),
        ]
        collect, skipped = domains_to_collect(domains, ls_servers, name_pattern="^ocp")

        assert [t.domain_id for t in collect] == ["1009"]
        assert [t.domain_id for t in skipped] == ["1010"]

    def test_one_matching_profile_is_enough_to_collect_a_domain(self) -> None:
        """Pruning is per *domain*, not per server — the name filter still
        drops the non-matching servers downstream.
        """
        domains = [_domain("1009", "dc1-a")]
        ls_servers = [
            _profile("vmware-esx-01", "dc1-a"),
            _profile("vmware-esx-02", "dc1-a"),
            _profile("ocp4-prod-tlv-infra-01", "dc1-a"),
        ]
        collect, skipped = domains_to_collect(domains, ls_servers, name_pattern="^ocp")

        assert [t.domain_id for t in collect] == ["1009"]
        assert skipped == []

    def test_a_domain_with_no_known_profiles_is_collected(self) -> None:
        """ADR-0014's open question is whether Central lists domain-local
        profiles at all; pruning on their absence would drop exactly the
        affected domains.
        """
        domains = [_domain("1009", "dc1-a"), _domain("1010", "no-profiles-in-central")]
        ls_servers = [_profile("ocp4-prod-tlv-infra-01", "dc1-a")]
        collect, skipped = domains_to_collect(domains, ls_servers, name_pattern="^ocp")

        assert [t.domain_id for t in collect] == ["1009", "1010"]
        assert skipped == []

    def test_no_profiles_at_all_collects_every_domain(self) -> None:
        domains = [_domain("1009", "a"), _domain("1010", "b")]
        collect, skipped = domains_to_collect(domains, [], name_pattern="^ocp")

        assert [t.domain_id for t in collect] == ["1009", "1010"]
        assert skipped == []

    def test_an_empty_pattern_collects_everything(self) -> None:
        """`INVENTORY_COLLECTOR_NAME_PATTERN` empty means "collect
        everything", so there is nothing to prune on.
        """
        domains = [_domain("1009", "a"), _domain("1010", "b")]
        ls_servers = [_profile("vmware-esx-01", "a"), _profile("vmware-esx-02", "b")]
        collect, skipped = domains_to_collect(domains, ls_servers, name_pattern="")

        assert [t.domain_id for t in collect] == ["1009", "1010"]
        assert skipped == []

    def test_templates_do_not_count_as_profiles(self) -> None:
        """`lsServer` carries profiles and templates alike, told apart only by
        `type` (docs/cisco-collectors.md, "Profiles and templates share one
        class"); a template must not keep an unmatched domain alive.
        """
        domains = [_domain("1009", "dc1-a")]
        ls_servers = [
            _profile("ocp-blade-template", "dc1-a", type="updating-template"),
            _profile("vmware-esx-01", "dc1-a"),
        ]
        collect, skipped = domains_to_collect(domains, ls_servers, name_pattern="^ocp")

        assert collect == []
        assert [t.domain_id for t in skipped] == ["1009"]

    @pytest.mark.parametrize("key", ["dc1-a", "10.0.0.9", "1009"])
    def test_profiles_resolve_by_domain_name_address_or_id(self, key: str) -> None:
        """The SDK does not pin which of the three Central puts in
        `LsServer.domain`; guessing wrong would look exactly like "no
        profiles", the never-prune case.
        """
        domains = [_domain("1009", "dc1-a")]  # address is 10.0.0.9
        ls_servers = [_profile("vmware-esx-01", key)]
        collect, skipped = domains_to_collect(domains, ls_servers, name_pattern="^ocp")

        assert collect == []
        assert [t.domain_id for t in skipped] == ["1009"]

    def test_endpoint_falls_back_to_the_domain_name(self) -> None:
        domains = [_domain("1009", "ucsm-dc1-a.example.com", address="")]
        collect, _ = domains_to_collect(domains, [], name_pattern="")

        assert [t.endpoint for t in collect] == ["ucsm-dc1-a.example.com"]

    def test_a_domain_with_no_address_and_no_name_is_skipped(self) -> None:
        """There is nothing to log into — collecting it would just be a
        connection error per run.
        """
        domains = [_domain("1009", "", address=""), _domain("1010", "b")]
        collect, skipped = domains_to_collect(domains, [], name_pattern="")

        assert [t.domain_id for t in collect] == ["1010"]
        assert [t.domain_id for t in skipped] == ["1009"]

    def test_address_is_preferred_over_name_as_the_endpoint(self) -> None:
        """`ComputeSystem.address` is what Cisco's own
        `ucscsdk/utils/ucscdomain.py` looks a domain up by, so it is the
        reachable value; `name` is a label that need not resolve.
        """
        domains = [_domain("1009", "dc1-a", address="10.20.30.40")]
        collect, _ = domains_to_collect(domains, [], name_pattern="")

        assert [t.endpoint for t in collect] == ["10.20.30.40"]

    def test_targets_carry_id_name_and_endpoint(self) -> None:
        domains = [_domain("1009", "dc1-a", address="10.20.30.40")]
        (target,) = domains_to_collect(domains, [], name_pattern="")[0]

        assert target == DomainTarget(domain_id="1009", name="dc1-a", endpoint="10.20.30.40")


class TestPruningIsNeverStricterThanTheNameFilter:
    """`tools.run_collector._NameFilteredProvider` uses `re.search`; a pruner
    using `re.match` would silently drop whole domains the real filter
    would have kept.
    """

    def test_pattern_matching_mid_name_still_keeps_the_domain(self) -> None:
        domains = [_domain("1009", "dc1-a")]
        ls_servers = [_profile("prod-ocp-1", "dc1-a")]
        collect, skipped = domains_to_collect(domains, ls_servers, name_pattern="ocp")

        # `re.match("ocp", "prod-ocp-1")` is None; `re.search` is not.
        assert [t.domain_id for t in collect] == ["1009"]
        assert skipped == []


class TestListServers:
    async def test_central_is_queried_exactly_twice(self) -> None:
        """Central's whole job here is domain discovery. More class queries
        would mean inventory was being read from the replica again.
        """
        client = FakeCentralClient(
            {
                "computeSystem": [_domain("1009", "a")],
                "lsServer": [_profile("ocp4-prod-tlv-infra-01", "a")],
            }
        )
        await _collect(_provider(client))

        assert sorted(client.queried) == ["computeSystem", "lsServer"]
        assert client.calls[0] == "login"
        assert client.calls[-1] == "logout"

    async def test_domains_stream_as_they_finish_not_after_the_whole_fleet(self) -> None:
        """A run killed at `activeDeadlineSeconds` must lose only the domains
        in flight, so servers stream per domain. The domain submitted first
        finishes last here, so arrival order is what proves it.
        """
        client = FakeCentralClient(
            {
                "computeSystem": [_domain("1", "slow-domain"), _domain("2", "fast-domain")],
                "lsServer": [],
            }
        )
        provider = _provider(
            client,
            {
                "10.0.0.1": FakeDomainProvider(
                    [_server("sys/rack-unit-1", "ocp4-slow-01")], delay=0.05
                ),
                "10.0.0.2": FakeDomainProvider([_server("sys/rack-unit-2", "ocp4-fast-01")]),
            },
            name_pattern="",
        )

        arrival_order = [server.name async for server in provider.collect()]

        assert arrival_order == ["ocp4-fast-01", "ocp4-slow-01"]

    async def test_stopping_early_cancels_and_drains_every_domain(self) -> None:
        """A consumer that stops early throws `GeneratorExit` in at
        `_list_servers`'s `yield`, which must not abandon a still-running
        domain's task and the UCS Manager session it holds.
        """
        client = FakeCentralClient(
            {
                "computeSystem": [_domain("1", "slow-domain"), _domain("2", "fast-domain")],
                "lsServer": [],
            }
        )
        slow = FakeDomainProvider([_server("sys/rack-unit-1", "ocp4-slow-01")], delay=5.0)
        fast = FakeDomainProvider([_server("sys/rack-unit-2", "ocp4-fast-01")])
        provider = _provider(
            client,
            {"10.0.0.1": slow, "10.0.0.2": fast},
            name_pattern="",
        )

        async with contextlib.aclosing(provider.collect()) as servers:
            async for _ in servers:
                break

        assert slow.torn_down
        assert fast.torn_down

    async def test_collects_every_domain_and_reroots_external_ids(self) -> None:
        client = FakeCentralClient(
            {
                "computeSystem": [_domain("1009", "a"), _domain("1010", "b")],
                "lsServer": [
                    _profile("ocp4-prod-tlv-infra-01", "a"),
                    _profile("ocp4-prod-two-infra-01", "b"),
                ],
            }
        )
        servers = await _collect(
            _provider(
                client,
                {
                    "10.0.0.9": FakeDomainProvider(
                        [_server("sys/chassis-1/blade-1", "ocp4-prod-tlv-infra-01")]
                    ),
                    "10.0.0.0": FakeDomainProvider(
                        [_server("sys/chassis-1/blade-1", "ocp4-prod-two-infra-01")]
                    ),
                },
            )
        )

        assert sorted(s.external_id for s in servers) == [
            "compute/sys-1009/chassis-1/blade-1",
            "compute/sys-1010/chassis-1/blade-1",
        ]
        assert sorted(s.name for s in servers) == [
            "ocp4-prod-tlv-infra-01",
            "ocp4-prod-two-infra-01",
        ]

    async def test_manager_id_stays_the_central_manager(self) -> None:
        """Servers collected through a domain's UCS Manager still belong to
        the one UCS Central manager document `run_collector.manager_for`
        writes per type.
        """
        client = FakeCentralClient({"computeSystem": [_domain("1009", "a")]})
        servers = await _collect(
            _provider(
                client,
                {"10.0.0.9": FakeDomainProvider([_server("sys/chassis-1/blade-1", "ocp-1")])},
            )
        )

        assert [s.manager_id for s in servers] == ["mgr_ucs_central"]

    async def test_pruned_domains_are_never_connected_to(self) -> None:
        """The efficiency claim: a domain with no matching profile names
        costs zero logins, not one login and nine queries.
        """
        connected: list[str] = []
        client = FakeCentralClient(
            {
                "computeSystem": [_domain("1009", "a"), _domain("1010", "b")],
                "lsServer": [
                    _profile("ocp4-prod-tlv-infra-01", "a"),
                    _profile("vmware-esx-07", "b"),
                ],
            }
        )
        provider = UcsCentralProvider(
            manager=_manager(),
            credentials=ManagerConnection(
                endpoint="central.example.com", username="u", password="p"
            ),
            timeout_seconds=5.0,
            domain_login=("domain-admin", "domain-secret"),
            name_pattern="^ocp",
            client_factory=lambda: client,
            domain_provider_factory=lambda target: (
                connected.append(target.endpoint),
                FakeDomainProvider([_server("sys/chassis-1/blade-1", "ocp-1")]),
            )[1],
        )
        await _collect(provider)

        assert connected == ["10.0.0.9"]

    async def test_a_failing_domain_does_not_abort_the_run(self) -> None:
        """One unreachable domain must cost that domain only — the same
        isolation `tools.run_collector` gives each manager.
        """
        client = FakeCentralClient({"computeSystem": [_domain("1009", "a"), _domain("1010", "b")]})
        servers, events = await _collect_with_logs(
            _provider(
                client,
                {
                    "10.0.0.9": FakeDomainProvider([], error=RuntimeError("unreachable")),
                    "10.0.0.0": FakeDomainProvider(
                        [_server("sys/chassis-1/blade-1", "ocp4-prod-two-infra-01")]
                    ),
                },
            )
        )

        assert [s.name for s in servers] == ["ocp4-prod-two-infra-01"]
        (failure,) = _events(events, "ucs_central.domain_failed")
        assert failure["endpoint"] == "10.0.0.9"

    async def test_logout_runs_even_when_a_query_fails(self) -> None:
        """Central enforces a per-user session cap, so a leaked session
        costs the *next* run, not this one — easy to miss without this.
        """

        class Failing(FakeCentralClient):
            async def query_classid(self, class_id: str) -> list[Any]:
                self.calls.append(f"query:{class_id}")
                raise RuntimeError("boom")

        client = Failing()
        with pytest.raises(RuntimeError, match="boom"):
            await _collect(_provider(client))
        assert client.calls[-1] == "logout"

    async def test_logout_runs_even_when_login_fails(self) -> None:
        class FailingLogin(FakeCentralClient):
            async def login(self) -> None:
                self.calls.append("login")
                raise RuntimeError("bad credentials")

        client = FailingLogin()
        with pytest.raises(RuntimeError, match="bad credentials"):
            await _collect(_provider(client))
        assert client.calls == ["login", "logout"]

    async def test_no_registered_domains_is_warned_about(self) -> None:
        """Zero servers from an otherwise-successful run is
        indistinguishable from a wrong endpoint without this.
        """
        _, events = await _collect_with_logs(_provider(FakeCentralClient()))

        assert _events(events, "ucs_central.no_domains")

    async def test_profiles_naming_an_unregistered_domain_are_warned_about(self) -> None:
        """A `LsServer.domain` matching no `computeSystem` is inventory the
        collector cannot reach — the one case the per-domain loop cannot
        surface, since it iterates registered domains.
        """
        client = FakeCentralClient(
            {
                "computeSystem": [_domain("1009", "a")],
                "lsServer": [
                    _profile("ocp4-prod-tlv-infra-01", "a"),
                    _profile("ocp4-prod-nine-infra-01", "decommissioned-dc"),
                ],
            }
        )
        _, events = await _collect_with_logs(_provider(client))

        (warning,) = _events(events, "ucs_central.profiles_in_unregistered_domain")
        assert warning["domains"] == ["decommissioned-dc"]

    async def test_health_check_only_touches_central(self) -> None:
        client = FakeCentralClient()
        await _provider(client).health_check()

        assert client.calls == ["login", "logout"]


class TestCollectionErrors:
    """What `tools.run_collector` turns into exit code 3. A failed domain
    contributes nothing to the summary counts, so this is the only channel
    telling it from a smaller estate — its negative cases matter as much.
    """

    async def test_a_complete_run_reports_no_errors(self) -> None:
        client = FakeCentralClient({"computeSystem": [_domain("1009", "a")]})
        provider = _provider(
            client,
            {"10.0.0.9": FakeDomainProvider([_server("sys/chassis-1/blade-1", "ocp-1")])},
        )
        servers = await _collect(provider)

        assert [s.name for s in servers] == ["ocp-1"]
        assert provider.collection_errors == ()

    async def test_a_failing_domain_is_reported_while_the_others_still_collect(self) -> None:
        """The headline case: partial success must be distinguishable from
        complete success, without costing the healthy domains their run.
        """
        client = FakeCentralClient({"computeSystem": [_domain("1009", "a"), _domain("1010", "b")]})
        provider = _provider(
            client,
            {
                "10.0.0.9": FakeDomainProvider([], error=RuntimeError("bad credentials")),
                "10.0.0.0": FakeDomainProvider(
                    [_server("sys/chassis-1/blade-1", "ocp4-prod-two-infra-01")]
                ),
            },
        )
        servers = await _collect(provider)

        assert [s.name for s in servers] == ["ocp4-prod-two-infra-01"]
        (error,) = provider.collection_errors
        assert "10.0.0.9" in error
        assert "bad credentials" in error

    async def test_ordinary_pruning_is_not_an_error(self) -> None:
        """A domain skipped because no profile matches the name pattern is
        the collector working, not a fault — reporting it would make exit 3
        permanent for any Central that also fronts non-OCP domains.
        """
        client = FakeCentralClient(
            {
                "computeSystem": [_domain("1009", "a"), _domain("1010", "b")],
                "lsServer": [
                    _profile("ocp4-prod-tlv-infra-01", "a"),
                    _profile("vmware-esx-07", "b"),
                ],
            }
        )
        provider = _provider(
            client,
            {"10.0.0.9": FakeDomainProvider([_server("sys/chassis-1/blade-1", "ocp-1")])},
        )
        await _collect(provider)

        assert provider.collection_errors == ()

    async def test_a_domain_with_no_address_is_an_error(self) -> None:
        """No address is a fault, not pruning: Central registered the domain
        and gave nothing to connect to. Both `name` and `address` are empty
        because the endpoint falls back to the name.
        """
        client = FakeCentralClient({"computeSystem": [_domain("1009", "", address="")]})
        provider = _provider(client)
        await _collect(provider)

        (error,) = provider.collection_errors
        assert "no address" in error

    async def test_errors_are_a_tuple(self) -> None:
        """Returned by value, so a caller cannot mutate the provider's own
        record of what failed.
        """
        client = FakeCentralClient({"computeSystem": [_domain("1009", "a")]})
        provider = _provider(client, {"10.0.0.9": FakeDomainProvider([], error=RuntimeError("x"))})
        await _collect(provider)

        assert isinstance(provider.collection_errors, tuple)

    async def test_errors_do_not_accumulate_across_iterations(self) -> None:
        """`collection_errors` describes one run, so re-iterating the same
        provider must report each failure once, not once per iteration.
        """
        client = FakeCentralClient({"computeSystem": [_domain("1009", "a")]})
        provider = _provider(
            client, {"10.0.0.9": FakeDomainProvider([], error=RuntimeError("unreachable"))}
        )
        await _collect(provider)
        assert len(provider.collection_errors) == 1

        await _collect(provider)
        assert len(provider.collection_errors) == 1


class TestDomainSummaryDiagnostics:
    """The domain list is the one thing still taken from Central's replica,
    so every run reports what Central believes each domain holds against
    what its own UCS Manager returned.
    """

    async def test_every_registered_domain_gets_one_summary_line(self) -> None:
        client = FakeCentralClient(
            {
                "computeSystem": [
                    _domain("1009", "healthy", total_physical_cnt="2"),
                    _domain("1010", "never-synced", inventory_status="in-progress"),
                ]
            }
        )
        _, events = await _collect_with_logs(
            _provider(
                client,
                {
                    "10.0.0.9": FakeDomainProvider(
                        [
                            _server("sys/chassis-1/blade-1", "ocp-1"),
                            _server("sys/chassis-1/blade-2", "ocp-2"),
                        ]
                    )
                },
            )
        )

        summaries = _events(events, "ucs_central.domain_summary")
        assert len(summaries) == 2
        healthy = next(e for e in summaries if e["domain_id"] == "1009")
        assert healthy["reported_servers"] == "2"
        assert healthy["collected_servers"] == 2
        stalled = next(e for e in summaries if e["domain_id"] == "1010")
        assert stalled["inventory_status"] == "in-progress"

    async def test_never_contacted_and_contacted_but_empty_are_distinguishable(self) -> None:
        """`collected_servers=None` means "we did not ask" (the domain was
        pruned); `0` means "we asked and got nothing". Collapsing the two
        would hide a pruning bug behind what looks like an empty domain.
        """
        client = FakeCentralClient(
            {
                "computeSystem": [_domain("1009", "pruned"), _domain("1010", "contacted")],
                "lsServer": [
                    # 1009's profiles match nothing, so it is pruned; 1010 has
                    # none known, so it is collected and comes back empty.
                    _profile("vmware-esx-07", "pruned")
                ],
            }
        )
        _, events = await _collect_with_logs(_provider(client, {}))

        summaries = {e["domain_id"]: e for e in _events(events, "ucs_central.domain_summary")}
        assert summaries["1009"]["collected_servers"] is None
        assert summaries["1010"]["collected_servers"] == 0

    async def test_a_skipped_domains_summary_logs_before_collection_even_starts(self) -> None:
        """A skipped domain's summary logs during planning, before the
        `as_completed` loop, so it survives a kill while a slow domain is
        still collecting (ADR-0014). Only the first server is read here.
        """
        client = FakeCentralClient(
            {
                "computeSystem": [_domain("1009", "pruned"), _domain("1010", "slow")],
                "lsServer": [_profile("vmware-esx-07", "pruned")],
            }
        )
        provider = _provider(
            client,
            {
                "10.0.0.0": FakeDomainProvider(
                    [_server("sys/rack-unit-1", "ocp4-slow-01")], delay=0.05
                )
            },
        )

        with capture_logs() as events:
            gen = provider.collect()
            await anext(gen)
            await gen.aclose()

        summaries = {e["domain_id"]: e for e in _events(events, "ucs_central.domain_summary")}
        assert summaries["1009"]["collected_servers"] is None

    async def test_warns_when_a_domain_reports_servers_but_returns_none(self) -> None:
        """The signature of an address that resolves, a login that does not
        work on that domain, or pruning that cut too deep.
        """
        client = FakeCentralClient(
            {"computeSystem": [_domain("1009", "a", total_physical_cnt="14")]}
        )
        _, events = await _collect_with_logs(_provider(client, {}))

        (warning,) = _events(events, "ucs_central.domain_collected_nothing")
        assert warning["domain_id"] == "1009"
        assert warning["reported_servers"] == "14"

    async def test_no_warning_when_a_domain_genuinely_holds_nothing(self) -> None:
        """Or the warning is noise on every healthy run and gets ignored."""
        client = FakeCentralClient(
            {"computeSystem": [_domain("1009", "a", total_physical_cnt="0")]}
        )
        _, events = await _collect_with_logs(_provider(client, {}))

        assert _events(events, "ucs_central.domain_collected_nothing") == []
