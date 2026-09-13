"""`app.infrastructure.providers.ucs_manager.provider`.

`UcsManagerProvider.list_servers` is exercised here against a fake client
rather than being left to a live domain: its orchestration (which classes
get queried, how service profiles are told apart from templates, how
descendant MOs are joined back onto their owning server, and whether the
session is closed on every path) is exactly where the bugs were, and none
of it needs a real UCS Manager to pin down.

The fake client's *shape* is what protects the real SDK contract — it
returns MOs whose DNs nest the way `ucsmo.py` really builds them
(`parent_dn + "/" + rn`), and the DN depths used here are the real ones
confirmed from `ucsmsdk==0.9.27` MO metadata:
`sys/chassis-1/blade-1/mgmt/if-1` and
`sys/chassis-1/blade-1/adaptor-1/host-eth-1`. A regression back to a
per-server `configResolveChildren` scoped to a blade DN would find
nothing at those depths and fail these tests.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from app.domain.enums import ManagerType
from app.domain.models.common import AuditFields
from app.domain.models.manager import Manager
from app.domain.ports.credentials import ManagerConnection
from app.infrastructure.providers.ucs_manager.provider import (
    UcsManagerProvider,
    _group_by_owning_server_dn,
    _is_equipped,
)

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("presence", "expected"),
    [
        ("equipped", True),
        ("equipped-deprecated", True),
        ("equipped-identity-unestablishable", True),
        ("equipped-with-malformed-fru", True),
        ("equipped-unsupported", True),
        # The secondary half of a multi-node server; ingesting it would
        # double-count one machine.
        ("equipped-slave", False),
        ("equipped-not-primary", False),
        ("empty", False),
        ("missing", False),
        ("missing-slave", False),
        ("mismatch", False),
        ("mismatch-slave", False),
        ("unauthorized", False),
        ("unknown", False),
        ("inaccessible", False),
        (None, False),
        ("", False),
    ],
)
def test_is_equipped(presence: str | None, expected: bool) -> None:
    mo = SimpleNamespace(presence=presence)
    assert _is_equipped(mo) is expected


class TestGroupByOwningServerDn:
    def test_groups_grandchildren_by_dn_prefix(self) -> None:
        mgmt_if = SimpleNamespace(dn="sys/chassis-1/blade-1/mgmt/if-1")
        host_eth = SimpleNamespace(dn="sys/chassis-1/blade-1/adaptor-1/host-eth-1")
        grouped = _group_by_owning_server_dn(
            [mgmt_if, host_eth], server_dns=["sys/chassis-1/blade-1"]
        )
        assert grouped["sys/chassis-1/blade-1"] == [mgmt_if, host_eth]

    def test_drops_mos_owned_by_non_servers(self) -> None:
        """A domain-wide `query_classid("mgmtIf")` also returns chassis, FI
        and IOM interfaces — `mgmtIf` hangs off a dozen parent classes.
        Those must be dropped, not mis-attributed to a server.
        """
        grouped = _group_by_owning_server_dn(
            [
                SimpleNamespace(dn="sys/chassis-1/mgmt/if-1"),
                SimpleNamespace(dn="sys/switch-A/mgmt/if-1"),
            ],
            server_dns=["sys/chassis-1/blade-1"],
        )
        assert grouped["sys/chassis-1/blade-1"] == []

    def test_dn_prefix_is_separator_anchored(self) -> None:
        """`sys/rack-unit-1` must not swallow `sys/rack-unit-10`'s
        descendants — a bare `startswith` without the separator would.
        """
        unit_10_if = SimpleNamespace(dn="sys/rack-unit-10/mgmt/if-1")
        grouped = _group_by_owning_server_dn(
            [unit_10_if], server_dns=["sys/rack-unit-1", "sys/rack-unit-10"]
        )
        assert grouped["sys/rack-unit-1"] == []
        assert grouped["sys/rack-unit-10"] == [unit_10_if]

    def test_longest_prefix_wins(self) -> None:
        """A nested compute unit claims its own descendants rather than
        donating them to the server enclosing it.
        """
        inner_if = SimpleNamespace(dn="sys/rack-unit-1/server-1/mgmt/if-1")
        grouped = _group_by_owning_server_dn(
            [inner_if], server_dns=["sys/rack-unit-1", "sys/rack-unit-1/server-1"]
        )
        assert grouped["sys/rack-unit-1"] == []
        assert grouped["sys/rack-unit-1/server-1"] == [inner_if]

    def test_skips_mos_without_a_dn(self) -> None:
        grouped = _group_by_owning_server_dn(
            [SimpleNamespace(dn=None), SimpleNamespace()], server_dns=["sys/chassis-1/blade-1"]
        )
        assert grouped["sys/chassis-1/blade-1"] == []


class FakeUcsClient:
    """Stands in for `UcsManagerClient`, recording the call sequence so
    tests can assert on session lifecycle as well as returned data.
    """

    def __init__(
        self,
        *,
        responses: dict[str, list[Any]] | None = None,
        login_error: Exception | None = None,
    ) -> None:
        self._responses = responses or {}
        self._login_error = login_error
        self.calls: list[str] = []

    async def login(self) -> None:
        self.calls.append("login")
        if self._login_error is not None:
            raise self._login_error

    async def logout(self) -> None:
        self.calls.append("logout")

    async def query_classid(self, class_id: str) -> list[Any]:
        self.calls.append(f"query_classid:{class_id}")
        return list(self._responses.get(class_id, []))


def _manager() -> Manager:
    return Manager(
        _id="mgr-1",
        name="ucsm-lab",
        type=ManagerType.UCS_MANAGER,
        endpoint="ucsm.lab.example.com",
        audit=AuditFields.new(),
    )


def _provider(client: FakeUcsClient) -> UcsManagerProvider:
    provider = UcsManagerProvider(
        manager=_manager(),
        credentials=ManagerConnection(
            endpoint="ucsm.lab.example.com", username="admin", password="secret"
        ),
        timeout_seconds=5.0,
    )
    provider._new_client = lambda: client  # ty: ignore[invalid-assignment]
    return provider


def _domain(**overrides: list[Any]) -> dict[str, list[Any]]:
    """One equipped blade with a service profile derived from a template,
    an out-of-band management interface, and one fabric-A vNIC.
    """
    domain: dict[str, list[Any]] = {
        "computeBlade": [
            SimpleNamespace(
                dn="sys/chassis-1/blade-1",
                name="blade-1",
                model="UCSB-B200-M6",
                serial="FCH12345678",
                uuid="11111111-2222-3333-4444-555555555555",
                presence="equipped",
                oper_state="ok",
                assigned_to_dn="org-root/ls-worker-01",
                num_of_cpus="2",
                num_of_cores="32",
                num_of_threads="64",
                total_memory="524288",
            )
        ],
        "computeRackUnit": [],
        "lsServer": [
            SimpleNamespace(
                dn="org-root/ls-worker-01",
                name="worker-01",
                type="instance",
                src_templ_name="worker-template",
                oper_src_templ_name="org-root/ls-worker-template",
            ),
            SimpleNamespace(
                dn="org-root/ls-worker-template",
                name="worker-template",
                type="updating-template",
                src_templ_name="",
                oper_src_templ_name="",
            ),
        ],
        "mgmtIf": [
            SimpleNamespace(
                dn="sys/chassis-1/blade-1/mgmt/if-1",
                access="out-of-band",
                ext_ip="10.1.2.3",
                mac="00:11:22:33:44:55",
            )
        ],
        "adaptorHostEthIf": [
            SimpleNamespace(
                dn="sys/chassis-1/blade-1/adaptor-1/host-eth-1",
                switch_id="A",
                mac="00:aa:bb:cc:dd:ee",
                admin_state="enabled",
                oper_state="up",
                id="1",
                name="eth0",
            )
        ],
        "processorUnit": [
            SimpleNamespace(
                dn="sys/chassis-1/blade-1/board/cpu-1",
                presence="equipped",
                model="Intel(R) Xeon(R) Gold 6338",
            )
        ],
        "storageLocalDisk": [
            SimpleNamespace(
                dn="sys/chassis-1/blade-1/board/storage-SAS-1/disk-1",
                presence="equipped",
                model="UCS-HD12TB10K12G",
                serial="S3X0ABCD",
                device_type="HDD",
                disk_state="online",
                size="1144641",
            )
        ],
        "topSystem": [SimpleNamespace(name="myc-03")],
    }
    domain.update(overrides)
    return domain


async def _collect(provider: UcsManagerProvider) -> list[Any]:
    return [server async for server in provider.collect()]


class TestListServers:
    async def test_joins_grandchild_mos_onto_their_server(self) -> None:
        """`mgmtIf` and `adaptorHostEthIf` live two levels below a compute
        unit, so they are fetched domain-wide and joined by DN prefix;
        unjoined, BMC address, MACs and attachments all come back empty (ADR-0009).
        """
        client = FakeUcsClient(responses=_domain())
        [server] = await _collect(_provider(client))

        assert server.bmc_address_raw == "ipmi://10.1.2.3:623"
        assert server.bmc_mac == "00:11:22:33:44:55"
        assert server.nic_macs == ("00:aa:bb:cc:dd:ee",)
        assert [a.fabric for a in server.attachments] == ["A"]
        # `fabric_name` is `topSystem.name`, confirmed live 2026-09-07
        # (ADR-0009's "Update (2026-09-07)").
        assert [a.fabric_name for a in server.attachments] == ["myc-03"]

    async def test_a_rack_units_own_psu_joins_directly(self) -> None:
        """`equipmentPsu` is a direct child of `computeRackUnit`'s own DN
        (`sys/rack-unit-3/psu-1`), so the ancestor-walk join resolves it
        the same way it does every other grandchild MO.
        """
        domain = _domain(computeBlade=[])
        domain["computeRackUnit"] = [
            SimpleNamespace(
                dn="sys/rack-unit-3",
                name="rack-3",
                presence="equipped",
                assigned_to_dn="",
                total_memory="524288",
            )
        ]
        domain["equipmentPsu"] = [
            SimpleNamespace(
                dn="sys/rack-unit-3/psu-1",
                id="1",
                presence="equipped",
                model="UCSC-PSU1-1050W",
                serial="LIT12345678",
                oper_state="operable",
                power="ok",
                psu_wattage="1050",
            )
        ]
        [server] = await _collect(_provider(FakeUcsClient(responses=domain)))

        assert len(server.psus) == 1
        assert server.psus[0]["health"] == "UP"
        assert server.psus[0]["capacity_watts"] == 1050

    async def test_a_blade_chassis_psu_is_not_attributed_to_the_blade(self) -> None:
        """A blade's PSUs (`sys/chassis-1/psu-1`) are siblings of the blade,
        not ancestors, so the ancestor-walk join drops them — the documented
        capability gap (docs/cisco-collectors.md, "Power supplies (PSUs)").
        """
        domain = _domain()
        domain["equipmentPsu"] = [
            SimpleNamespace(
                dn="sys/chassis-1/psu-1",
                id="1",
                presence="equipped",
                model="UCSB-PSU-2500ACPL",
                serial="DCA12345678",
                oper_state="operable",
                power="ok",
                psu_wattage="2500",
            )
        ]
        [server] = await _collect(_provider(FakeUcsClient(responses=domain)))

        assert server.psus == ()

    async def test_a_blades_own_gpu_joins_directly_unlike_psus(self) -> None:
        """`graphicsCard`'s parent (`computeBoard`) sits directly under the
        server (`.../blade-1/board/graphics-card-1`) for blades and rack
        units alike, per `ComputeBoard`'s `mo_meta`, so the ancestor walk finds it.
        """
        domain = _domain()
        domain["graphicsCard"] = [
            SimpleNamespace(
                dn="sys/chassis-1/blade-1/board/graphics-card-1",
                id="1",
                presence="equipped",
                model="UCSC-GPU-A100",
                vendor="NVIDIA",
                serial="GPU12345678",
                oper_state="operable",
                power="ok",
                pci_addr="0000:af:00.0",
                firmware_version="96.00.5E.00.02",
                temperature="42.5",
            )
        ]
        [server] = await _collect(_provider(FakeUcsClient(responses=domain)))

        assert len(server.gpus) == 1
        assert server.gpus[0]["vendor"] == "NVIDIA"
        assert server.gpus[0]["temperature_celsius"] == 42.5

    async def test_management_ip_pool_address_under_the_profile_dn_is_preferred(self) -> None:
        """Real hardware, unlike UCSPE, can have `mgmtIf.ext_ip` unset while a
        pooled address sits under the profile's own DN
        (`org-root/ls-worker-01/ipv4-pooled-addr`). See docs/cisco-collectors.md.
        """
        domain = _domain()
        domain["mgmtIf"] = [
            SimpleNamespace(
                dn="sys/chassis-1/blade-1/mgmt/if-1",
                access="out-of-band",
                ext_ip="0.0.0.0",  # noqa: S104 - unset-IP sentinel, not a bind
                mac="00:11:22:33:44:55",
            )
        ]
        domain["vnicIpV4PooledAddr"] = [
            SimpleNamespace(dn="org-root/ls-worker-01/ipv4-pooled-addr", addr="10.9.8.7")
        ]
        client = FakeUcsClient(responses=domain)
        [server] = await _collect(_provider(client))

        assert server.bmc_address_raw == "ipmi://10.9.8.7:623"
        # The MAC still comes off the physical mgmtIf.
        assert server.bmc_mac == "00:11:22:33:44:55"

    async def test_management_ip_pool_address_falls_back_to_the_mgmt_controller_dn(self) -> None:
        """Schema-valid per `ucsmsdk`'s `mo_meta.parents`, but not
        confirmed populated on real hardware — kept as a fallback, tried
        only once the profile-scoped DN misses.
        """
        domain = _domain()
        domain["mgmtIf"] = [
            SimpleNamespace(
                dn="sys/chassis-1/blade-1/mgmt/if-1",
                access="out-of-band",
                ext_ip="0.0.0.0",  # noqa: S104 - unset-IP sentinel, not a bind
                mac="00:11:22:33:44:55",
            )
        ]
        domain["vnicIpV4PooledAddr"] = [
            SimpleNamespace(dn="sys/chassis-1/blade-1/mgmt/ipv4-pooled-addr", addr="10.5.5.5")
        ]
        client = FakeUcsClient(responses=domain)
        [server] = await _collect(_provider(client))

        assert server.bmc_address_raw == "ipmi://10.5.5.5:623"

    async def test_profile_dn_is_reported_on_the_provider_server(self) -> None:
        client = FakeUcsClient(responses=_domain())
        [server] = await _collect(_provider(client))

        assert server.profile_dn == "org-root/ls-worker-01"

    async def test_fabric_interconnect_identity_is_joined_by_switch_id(self) -> None:
        """`networkElement` is queried domain-wide (the redundant FI pair) and
        joined onto every attachment by its bare `switch_id`, the same "A"/"B"
        the adaptor interfaces report.
        """
        domain = _domain()
        domain["networkElement"] = [
            SimpleNamespace(id="A", model="UCS-FI-6454", serial="FCH2222A"),
            SimpleNamespace(id="B", model="UCS-FI-6454", serial="FCH2222B"),
        ]
        client = FakeUcsClient(responses=domain)
        [server] = await _collect(_provider(client))

        [attachment] = server.attachments
        assert attachment.fabric == "A"
        assert attachment.fabric_model == "UCS-FI-6454"
        assert attachment.fabric_serial == "FCH2222A"

    async def test_physical_and_vnic_attachments_are_labeled(self) -> None:
        domain = _domain()
        domain["adaptorExtEthIf"] = [
            SimpleNamespace(
                dn="sys/chassis-1/blade-1/adaptor-1/ext-eth-1",
                switch_id="A",
                mac="00:aa:bb:cc:dd:00",
                admin_state="enabled",
                oper_state="up",
                id="1",
                name="ext-eth-1",
            )
        ]
        client = FakeUcsClient(responses=domain)
        [server] = await _collect(_provider(client))

        kinds = sorted(a.interface_kind for a in server.attachments)
        assert kinds == ["PHYSICAL", "VNIC"]

    async def test_never_queries_a_nonexistent_template_class(self) -> None:
        """There is no `lsServiceProfileTemplate` class in UCS Manager's
        model — querying it makes the whole run fail. Templates come from
        `lsServer` partitioned by `type`.
        """
        client = FakeUcsClient(responses=_domain())
        await _collect(_provider(client))

        queried = [c.split(":", 1)[1] for c in client.calls if c.startswith("query_classid:")]
        assert "lsServiceProfileTemplate" not in queried
        assert queried.count("lsServer") == 1

    async def test_templates_are_not_treated_as_service_profiles(self) -> None:
        """A template shares the `lsServer` class with real profiles. If
        it landed in `profile_by_dn`, a server assigned to a template DN
        would resolve nonsense.
        """
        client = FakeUcsClient(responses=_domain())
        [server] = await _collect(_provider(client))

        assert server.profile_template_name == "worker-template"
        assert server.profile_template_external_id == "org-root/ls-worker-template"

    async def test_template_external_id_survives_a_cross_org_name_collision(self) -> None:
        """Two orgs can each own a "worker-template". The resolved
        `oper_src_templ_name` DN disambiguates where the bare name can't.
        """
        domain = _domain()
        domain["lsServer"].append(
            SimpleNamespace(
                dn="org-root/org-other/ls-worker-template",
                name="worker-template",
                type="updating-template",
                src_templ_name="",
                oper_src_templ_name="",
            )
        )
        client = FakeUcsClient(responses=domain)
        [server] = await _collect(_provider(client))

        assert server.profile_template_external_id == "org-root/ls-worker-template"

    async def test_scales_query_count_independently_of_fleet_size(self) -> None:
        """Fixed number of round trips regardless of how many servers the
        domain has — the guard against reintroducing a per-server query.
        """
        domain = _domain()
        domain["computeBlade"] = [
            SimpleNamespace(
                dn=f"sys/chassis-1/blade-{i}",
                name=f"blade-{i}",
                presence="equipped",
                assigned_to_dn="",
                total_memory="1024",
            )
            for i in range(1, 51)
        ]
        client = FakeUcsClient(responses=domain)
        servers = await _collect(_provider(client))

        assert len(servers) == 50
        # 14 since 2026-09-07's `topSystem` query — still O(1) per domain,
        # which is the property guarded.
        assert len([c for c in client.calls if c.startswith("query_classid:")]) == 14

    async def test_no_topsystem_returned_leaves_fabric_name_none(self) -> None:
        """A domain that answers `topSystem` with nothing — rather than a
        real row with no `name` — must not crash or invent a name.
        """
        domain = _domain(topSystem=[])
        client = FakeUcsClient(responses=domain)
        [server] = await _collect(_provider(client))

        assert [a.fabric_name for a in server.attachments] == [None]

    async def test_skips_non_equipped_servers(self) -> None:
        domain = _domain()
        domain["computeBlade"].append(
            SimpleNamespace(dn="sys/chassis-1/blade-2", name="blade-2", presence="empty")
        )
        client = FakeUcsClient(responses=domain)
        assert len(await _collect(_provider(client))) == 1

    async def test_ignores_in_band_management_interfaces(self) -> None:
        domain = _domain()
        domain["mgmtIf"] = [
            SimpleNamespace(
                dn="sys/chassis-1/blade-1/mgmt/if-2",
                access="in-band",
                ext_ip="10.9.9.9",
                mac="00:00:00:00:00:01",
            )
        ]
        client = FakeUcsClient(responses=domain)
        [server] = await _collect(_provider(client))

        assert server.bmc_address_raw is None
        assert server.bmc_mac is None

    async def test_server_with_no_descendants_still_yields(self) -> None:
        domain = _domain(mgmtIf=[], adaptorHostEthIf=[], processorUnit=[], storageLocalDisk=[])
        client = FakeUcsClient(responses=domain)
        [server] = await _collect(_provider(client))

        assert server.external_id == "sys/chassis-1/blade-1"
        assert server.bmc_address_raw is None
        assert server.nic_macs == ()
        assert server.attachments == ()
        assert server.cpu_model is None
        assert server.storage_drives == ()

    async def test_joins_cpu_and_storage_grandchildren_onto_their_server(self) -> None:
        """The grandchildren-join defect class ADR-0009 found for `mgmtIf`
        applies here too: `processorUnit` and `storageLocalDisk` are two or
        more levels below a compute unit, not children of it.
        """
        client = FakeUcsClient(responses=_domain())
        [server] = await _collect(_provider(client))

        assert server.cpu_model == "Intel(R) Xeon(R) Gold 6338"
        assert len(server.storage_drives) == 1
        assert server.storage_drives[0]["model"] == "UCS-HD12TB10K12G"
        assert server.storage_total_bytes == 1144641 * 1024 * 1024

    async def test_logs_out_after_a_full_drain(self) -> None:
        client = FakeUcsClient(responses=_domain())
        await _collect(_provider(client))
        assert client.calls[0] == "login"
        assert client.calls[-1] == "logout"

    async def test_logs_out_when_login_itself_fails(self) -> None:
        """`login()` has to sit *inside* the try: `ucssession._login` sets
        the session cookie before its version/domain probes, so a failure
        there leaves a live session that only `logout()` reclaims.
        """
        client = FakeUcsClient(login_error=RuntimeError("version probe failed"))
        with pytest.raises(RuntimeError):
            await _collect(_provider(client))
        assert client.calls == ["login", "logout"]

    async def test_logs_out_when_a_query_fails(self) -> None:
        class ExplodingClient(FakeUcsClient):
            async def query_classid(self, class_id: str) -> list[Any]:
                self.calls.append(f"query_classid:{class_id}")
                raise RuntimeError("connection reset")

        client = ExplodingClient()
        with pytest.raises(RuntimeError):
            await _collect(_provider(client))
        assert client.calls[-1] == "logout"

    async def test_logs_out_when_the_consumer_abandons_the_generator(self) -> None:
        """Nothing in the platform breaks out of this loop today, but the
        session cap makes a leak here expensive — `aclose()` must run the
        `finally`.
        """
        client = FakeUcsClient(responses=_domain())
        generator = _provider(client).collect()
        await generator.__anext__()
        await generator.aclose()
        assert client.calls[-1] == "logout"
