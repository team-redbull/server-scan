"""`app.infrastructure.providers.intersight.provider`.

The provider's whole job is the fleet-wide join: read each sub-resource
once for the entire estate and attach it to the right server by following
the inverse reference every child object carries. Two things about that
carry the risk, and both are pinned here rather than left to a live run.

**A join that silently attaches nothing** looks exactly like a fleet with
no drives and no NICs — a run that reports success while destroying
detail. **A sub-resource query that fails** must degrade to `None` (which
`IngestService` preserves) and be reported, never to `[]` (which
overwrites).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest

from app.domain.enums import ManagerType
from app.domain.models.common import AuditFields
from app.domain.models.manager import Manager
from app.infrastructure.providers.intersight.client import IntersightError
from app.infrastructure.providers.intersight.provider import IntersightProvider

pytestmark = pytest.mark.unit


def _ref(moid: str) -> dict[str, str]:
    """
    An `mo.MoRef` relationship value.

    Args:
        moid (str): The referenced object.

    Returns:
        dict[str, str]: The reference.
    """
    return {"ClassId": "mo.MoRef", "Moid": moid}


# One rack server with one adapter (one uplink, one vNIC), one controller
# with one disk, one GPU, one CPU, two PSUs and one BMC interface — the
# smallest estate exercising every join, the two-hop ones included.
_TABLES: dict[str, list[dict[str, Any]]] = {
    "compute/PhysicalSummaries": [
        {
            "Moid": "server1",
            "Name": "UCSC-C240-WZP1",
            "Model": "UCSC-C240-M6SX",
            "Serial": "WZP1",
            "TotalMemory": 1024,
            "ManagementMode": "Intersight",
            "MgmtIpAddress": "10.0.0.1",
        },
        {
            "Moid": "server2",
            "Name": "UCSC-C220-WZP2",
            "Serial": "WZP2",
            "ManagementMode": "IntersightStandalone",
        },
    ],
    "server/Profiles": [
        {
            "Moid": "profile1",
            "Name": "ocp4-prod-tlv-infra-01",
            "Dn": "profile/1",
            "AssignedServer": _ref("server1"),
            "SrcTemplate": _ref("template1"),
        }
    ],
    "server/ProfileTemplates": [{"Moid": "template1", "Name": "ocp-infra-template"}],
    "adapter/Units": [{"Moid": "adapter1", "ComputeRackUnit": _ref("server1")}],
    "adapter/ExtEthInterfaces": [
        {
            "Moid": "ext1",
            "AdapterUnit": _ref("adapter1"),
            "SwitchId": "A",
            "ExtEthInterfaceId": "1",
            "MacAddress": "00:11:22:33:44:00",
            "OperState": "up",
        }
    ],
    "adapter/HostEthInterfaces": [
        {
            "Moid": "host1",
            "AdapterUnit": _ref("adapter1"),
            "Name": "eth0",
            "MacAddress": "00:AA:BB:CC:DD:EE",
        }
    ],
    "storage/Controllers": [{"Moid": "ctrl1", "ComputeRackUnit": _ref("server1")}],
    "storage/PhysicalDisks": [
        {
            "Moid": "disk1",
            "DiskId": "1",
            "Model": "MZ7LH960",
            "NonCoercedSizeBytes": 960197124096,
            "Health": "Good",
            "StorageController": _ref("ctrl1"),
        }
    ],
    "graphics/Cards": [
        {"Moid": "gpu1", "Model": "NVIDIA A100", "ComputeRackUnit": _ref("server1")}
    ],
    "processor/Units": [
        {"Moid": "cpu1", "Model": "UCS-CPU-I6338", "ComputeRackUnit": _ref("server1")}
    ],
    "equipment/Psus": [
        {
            "Moid": "psu1",
            "PsuId": "1",
            "Model": "PSU-750W",
            "OperState": "operable",
            "ComputeRackUnit": _ref("server1"),
        },
        {
            "Moid": "psu2",
            "PsuId": "2",
            "Model": "PSU-750W",
            "OperState": "inoperable",
            "ComputeRackUnit": _ref("server1"),
        },
    ],
    "management/Controllers": [{"Moid": "bmc1", "ComputeRackUnit": _ref("server1")}],
    "management/Interfaces": [
        {"Moid": "mgmt1", "MacAddress": "00:BB:CC:DD:EE:FF", "ManagementController": _ref("bmc1")}
    ],
}


class _FakeClient:
    """
    A scripted stand-in for `IntersightClient`.

    Records every resource asked for, so the request plan itself — one
    list call per resource, never one per server — is assertable.
    """

    def __init__(
        self,
        tables: dict[str, list[dict[str, Any]]] | None = None,
        *,
        failing: frozenset[str] = frozenset(),
    ) -> None:
        """
        Args:
            tables (dict[str, list[dict[str, Any]]] | None): Rows per
                resource, defaulting to the estate above.
            failing (frozenset[str]): Resources whose query raises.
        """
        self.tables = _TABLES if tables is None else tables
        self.failing = failing
        self.requested: list[str] = []
        self.closed = False

    async def list_all(
        self, resource: str, *, select: str | None = None, filter_expr: str | None = None
    ) -> AsyncIterator[dict[str, Any]]:
        """
        Yield the scripted rows for one resource.

        Args:
            resource (str): The resource path.
            select (str | None): Ignored.
            filter_expr (str | None): Applied only for management modes.

        Yields:
            dict[str, Any]: Each row.

        Raises:
            IntersightError: If this resource is scripted to fail.
        """
        self.requested.append(resource)
        if resource in self.failing:
            raise IntersightError(f"{resource} exploded")
        for row in self.tables.get(resource, []):
            if filter_expr and f"'{row.get('ManagementMode')}'" not in filter_expr:
                continue
            yield row

    async def health_check(self) -> None:
        """Always healthy."""
        return

    async def aclose(self) -> None:
        """Record that the client was released."""
        self.closed = True


def _provider(client: _FakeClient, **kwargs: Any) -> IntersightProvider:
    """
    A provider wired to a fake client.

    Args:
        client (_FakeClient): The scripted client.
        **kwargs: Constructor overrides.

    Returns:
        IntersightProvider: The provider under test.
    """
    return IntersightProvider(
        manager=Manager(
            id="mgr_intersight",
            name="intersight",
            type=ManagerType.INTERSIGHT,
            endpoint="intersight.com",
            enabled=True,
            audit=AuditFields.new(),
        ),
        endpoint="intersight.com",
        api_key_id="a/b/c",
        api_key_pem="pem",
        client_factory=lambda: client,
        **kwargs,
    )


async def _collect(provider: IntersightProvider) -> list[Any]:
    """
    Drain a provider.

    Args:
        provider (IntersightProvider): The provider.

    Returns:
        list[Any]: Every server it yielded.
    """
    return [server async for server in provider.collect()]


@pytest.mark.asyncio
async def test_every_subresource_is_read_once_for_the_whole_fleet() -> None:
    """The property that makes this collector reach 10,000 servers: cost
    is a function of resource count, not of fleet size. A regression to
    per-server queries would show up here as a repeated resource.
    """
    client = _FakeClient()
    await _collect(_provider(client))

    assert len(client.requested) == len(set(client.requested)), (
        f"a resource was queried more than once: {client.requested}"
    )
    assert "compute/PhysicalSummaries" in client.requested


@pytest.mark.asyncio
async def test_a_two_hop_join_attaches_interfaces_through_their_adapter() -> None:
    """An interface references its adapter unit, and the adapter unit
    references the server — the server is never named on the interface.
    """
    servers = await _collect(_provider(_FakeClient()))
    first = next(s for s in servers if s.serial == "WZP1")

    kinds = sorted(a.interface_kind for a in first.attachments)
    assert kinds == ["PHYSICAL", "VNIC"]
    assert first.nic_macs == ("00:aa:bb:cc:dd:ee",)


@pytest.mark.asyncio
async def test_a_two_hop_join_attaches_disks_through_their_controller() -> None:
    """A `storage.PhysicalDisk` carries no reference to its server."""
    servers = await _collect(_provider(_FakeClient()))
    first = next(s for s in servers if s.serial == "WZP1")

    assert first.storage_drives is not None
    assert [d["model"] for d in first.storage_drives] == ["MZ7LH960"]
    assert first.storage_total_bytes == 960197124096


@pytest.mark.asyncio
async def test_a_controller_that_only_names_its_computeboard_still_joins() -> None:
    """Confirmed live 2026-09-01: 0 of 37 `storage.Controller` rows set
    `ComputeBlade`/`ComputeRackUnit`, only `ComputeBoard`. `graphics.Card`
    and `processor.Unit` share the fallback (ADR-0017); `adapter.Unit` does not.
    """
    tables = dict(_TABLES)
    tables["compute/Boards"] = [{"Moid": "board1", "ComputeRackUnit": _ref("server1")}]
    tables["storage/Controllers"] = [{"Moid": "ctrl1", "ComputeBoard": _ref("board1")}]
    tables["graphics/Cards"] = [
        {"Moid": "gpu1", "Model": "NVIDIA A100", "ComputeBoard": _ref("board1")}
    ]
    tables["processor/Units"] = [
        {"Moid": "cpu1", "Model": "UCS-CPU-I6338", "ComputeBoard": _ref("board1")}
    ]
    servers = await _collect(_provider(_FakeClient(tables)))
    first = next(s for s in servers if s.serial == "WZP1")

    assert first.storage_drives is not None
    assert [d["model"] for d in first.storage_drives] == ["MZ7LH960"]
    assert [g["model"] for g in first.gpus or ()] == ["NVIDIA A100"]
    assert first.cpu_model == "UCS-CPU-I6338"


@pytest.mark.asyncio
async def test_the_profile_supplies_the_name_and_the_template() -> None:
    """The name trap, end to end through the join rather than in
    isolation — this is where a wrong `AssignedServer` key would show.
    """
    servers = await _collect(_provider(_FakeClient()))
    first = next(s for s in servers if s.serial == "WZP1")

    assert first.name == "ocp4-prod-tlv-infra-01"
    assert first.profile_template_name == "ocp-infra-template"


@pytest.mark.asyncio
async def test_a_server_with_no_subresources_gets_empty_not_another_server_s() -> None:
    """The join must not leak one server's hardware onto another — the
    failure mode a naive "first row wins" join produces.
    """
    servers = await _collect(_provider(_FakeClient()))
    second = next(s for s in servers if s.serial == "WZP2")

    assert second.storage_drives == ()
    assert second.attachments == ()
    assert second.gpus == ()
    assert second.psus == ()
    assert second.cpu_model is None
    assert second.name == "UCSC-C220-WZP2"


@pytest.mark.asyncio
async def test_cpu_model_joins_through_the_processor_unit() -> None:
    """`processor.Unit` was added 2026-09-01, reversing ADR-0017's
    original cut — see `docs/adr/0017-intersight-collector.md`, Decision
    5, and `docs/notes/intersight-inventory-model.md`'s follow-up.
    """
    servers = await _collect(_provider(_FakeClient()))
    first = next(s for s in servers if s.serial == "WZP1")

    assert first.cpu_model == "UCS-CPU-I6338"


@pytest.mark.asyncio
async def test_psus_join_through_computerackunit() -> None:
    """`equipment.Psu` carries no `ComputeBoard` relationship at all
    (confirmed against Cisco's generated Go SDK) — a blade's PSUs belong to
    its chassis — so this join is `ComputeRackUnit` only. See ADR-0017.
    """
    servers = await _collect(_provider(_FakeClient()))
    first = next(s for s in servers if s.serial == "WZP1")

    assert first.psus is not None
    assert [p["id"] for p in first.psus] == ["1", "2"]
    assert [p["health"] for p in first.psus] == ["UP", "DOWN"]


@pytest.mark.asyncio
async def test_the_bmc_interface_joins_through_its_controller() -> None:
    """Two hops again, and the MAC is the half the summary cannot give."""
    servers = await _collect(_provider(_FakeClient()))
    first = next(s for s in servers if s.serial == "WZP1")

    assert first.bmc_mac == "00:BB:CC:DD:EE:FF"
    assert first.bmc_address_raw == "ipmi://10.0.0.1:623"


@pytest.mark.asyncio
async def test_ucsm_managed_servers_are_excluded_by_default() -> None:
    """They are exactly the set `..ucs_central` already owns. Collecting
    both makes one document's `source_provider` and fields flip on
    whichever CronJob ran last. ADR-0017, "Decision 3".
    """
    tables = dict(_TABLES)
    tables["compute/PhysicalSummaries"] = [
        {"Moid": "s1", "Name": "imm", "Serial": "A", "ManagementMode": "Intersight"},
        {"Moid": "s2", "Name": "ucsm", "Serial": "B", "ManagementMode": "UCSM"},
    ]
    servers = await _collect(_provider(_FakeClient(tables)))

    assert [s.serial for s in servers] == ["A"]


@pytest.mark.asyncio
async def test_an_operator_can_opt_into_ucsm_mode() -> None:
    """For an estate whose UCS domains are not registered with Central at
    all, where nothing else would ever collect them.
    """
    tables = dict(_TABLES)
    tables["compute/PhysicalSummaries"] = [
        {"Moid": "s2", "Name": "ucsm", "Serial": "B", "ManagementMode": "UCSM"}
    ]
    servers = await _collect(_provider(_FakeClient(tables), management_modes=("UCSM",)))

    assert [s.serial for s in servers] == ["B"]


@pytest.mark.asyncio
async def test_a_failed_subresource_query_reports_none_not_empty() -> None:
    """`IngestService` carries a stored value forward for `None` and
    overwrites for a real value, so `[]` here would clear every server's
    drives and take a CRITICAL server to HEALTHY.
    """
    client = _FakeClient(failing=frozenset({"storage/PhysicalDisks"}))
    provider = _provider(client)
    servers = await _collect(provider)

    first = next(s for s in servers if s.serial == "WZP1")
    assert first.storage_drives is None
    assert first.storage_total_bytes is None
    assert first.attachments != ()


@pytest.mark.asyncio
async def test_a_failed_subresource_is_reported_as_a_partial_run() -> None:
    """`tools.run_collector` turns this into exit 3. A run that could not
    read drives is not a run that found none, and must not exit 0.
    """
    provider = _provider(_FakeClient(failing=frozenset({"graphics/Cards"})))
    await _collect(provider)

    assert any("graphics/Cards" in message for message in provider.collection_errors)


@pytest.mark.asyncio
async def test_a_failed_parent_query_skips_its_children_without_crashing() -> None:
    """No adapter units means no way to attribute any interface, so the
    interfaces are unreadable too rather than mis-attributed.
    """
    provider = _provider(_FakeClient(failing=frozenset({"adapter/Units"})))
    servers = await _collect(provider)

    first = next(s for s in servers if s.serial == "WZP1")
    assert first.attachments == ()
    assert first.nic_macs is None


@pytest.mark.asyncio
async def test_a_failing_server_list_aborts_the_run() -> None:
    """Unlike a sub-resource, the anchor query has no partial answer."""
    provider = _provider(_FakeClient(failing=frozenset({"compute/PhysicalSummaries"})))
    with pytest.raises(IntersightError):
        await _collect(provider)


@pytest.mark.asyncio
async def test_an_exhausted_run_budget_stops_and_says_what_it_missed() -> None:
    """Ending with a summary beats being killed by `activeDeadlineSeconds`
    with nothing reported at all.
    """
    provider = _provider(_FakeClient(), run_budget_seconds=-1.0)
    servers = await _collect(provider)

    assert servers == []
    assert any("run budget" in message for message in provider.collection_errors)


@pytest.mark.asyncio
async def test_the_client_is_released_even_when_the_run_fails() -> None:
    """A CronJob pod that leaks its connection pool on every failure is a
    slow leak nobody attributes to the collector.
    """
    client = _FakeClient(failing=frozenset({"compute/PhysicalSummaries"}))
    with pytest.raises(IntersightError):
        await _collect(_provider(client))

    assert client.closed


@pytest.mark.asyncio
async def test_a_clean_run_reports_no_collection_errors() -> None:
    """The negative case, so the partial-run signal means something."""
    provider = _provider(_FakeClient())
    await _collect(provider)

    assert provider.collection_errors == ()


@pytest.mark.asyncio
async def test_the_provider_type_is_stamped_on_every_server() -> None:
    """`Server.source_provider` is what tells an operator which collector
    found a machine, and is filterable in the API.
    """
    provider = _provider(_FakeClient())
    assert provider.provider_type == ManagerType.INTERSIGHT.value

    servers = await _collect(provider)
    assert all(s.manager_id == "mgr_intersight" for s in servers)


@pytest.mark.asyncio
async def test_a_profile_joins_on_its_association_before_its_assignment() -> None:
    """A profile can be *assigned* to one server and *associated* with
    the one actually running it. The association wins, because that is
    the machine whose inventory this run is naming.
    """
    tables = dict(_TABLES)
    tables["server/Profiles"] = [
        {
            "Moid": "profile1",
            "Name": "ocp4-prod-tlv-infra-01",
            "AssignedServer": _ref("server2"),
            "AssociatedServer": _ref("server1"),
        }
    ]
    servers = await _collect(_provider(_FakeClient(tables)))

    assert next(s for s in servers if s.serial == "WZP1").name == "ocp4-prod-tlv-infra-01"
    assert next(s for s in servers if s.serial == "WZP2").name == "UCSC-C220-WZP2"


@pytest.mark.asyncio
async def test_an_unassociated_profile_still_names_its_assigned_server() -> None:
    """A profile assigned but not yet deployed is the normal state for a
    server being built, and its name is still the right one.
    """
    tables = dict(_TABLES)
    tables["server/Profiles"] = [
        {"Moid": "p", "Name": "ocp4-nyc-worker-01", "AssignedServer": _ref("server2")}
    ]
    servers = await _collect(_provider(_FakeClient(tables)))

    assert next(s for s in servers if s.serial == "WZP2").name == "ocp4-nyc-worker-01"


@pytest.mark.asyncio
async def test_the_run_budget_also_bounds_the_join_phase() -> None:
    """The sub-resource reads are where a throttled tenant actually costs
    its time. A budget that only bounded the streaming phase would let the
    CronJob be killed here with nothing reported at all.
    """
    client = _FakeClient()
    provider = _provider(client, run_budget_seconds=-1.0)
    servers = await _collect(provider)

    assert servers == []
    assert any("run budget" in m for m in provider.collection_errors)
    # Gave up before reading any sub-resource, not after reading the estate.
    assert [r for r in client.requested if r != "compute/PhysicalSummaries"] == []


@pytest.mark.asyncio
async def test_the_budget_message_names_the_phase_it_gave_up_in() -> None:
    """ "Ran out of time" is not actionable; "ran out before reading any
    sub-resource" tells an operator to raise the budget rather than go
    looking for a broken query.
    """
    provider = _provider(_FakeClient(), run_budget_seconds=-1.0)
    await _collect(provider)

    assert any("before reading any sub-resource" in m for m in provider.collection_errors)
