"""`app.infrastructure.providers.ucs_manager.mapping` — pure MO ->
`ProviderServer` conversion, tested with plain `SimpleNamespace` stand-ins
for `ucsmsdk` managed objects (attribute names match the real MO classes'
`prop_meta`, confirmed against the installed `ucsmsdk==0.9.27` package —
see `mapping.py`'s module docstring) rather than constructing real
`ucsmsdk` MO instances, which need a live/mocked XML response to build.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.infrastructure.providers.ucs_manager.mapping import compute_unit_to_provider_server

pytestmark = pytest.mark.unit


def _blade(**overrides: object) -> SimpleNamespace:
    defaults = {
        "dn": "sys/chassis-1/blade-3",
        "name": "blade-3",
        "model": "UCSB-B200-M6",
        "vendor": "Cisco Systems Inc",
        "serial": "FCH12345678",
        "uuid": "11111111-2222-3333-4444-555555555555",
        "num_of_cpus": "2",
        "num_of_cores": "32",
        "num_of_threads": "64",
        "total_memory": "524288",  # MB
        "available_memory": "524288",
        "presence": "equipped",
        "oper_state": "ok",
        "assigned_to_dn": "org-root/ls-worker-01",
        "server_id": "1/3",
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _profile(**overrides: object) -> SimpleNamespace:
    defaults = {"dn": "org-root/ls-worker-01", "src_templ_name": "worker-template"}
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _template(**overrides: object) -> SimpleNamespace:
    defaults = {"name": "worker-template", "dn": "org-root/ls-template-worker-template"}
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _mgmt_if(**overrides: object) -> SimpleNamespace:
    defaults = {"access": "out-of-band", "ext_ip": "10.1.2.3", "mac": "00:11:22:33:44:55"}
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _mgmt_ip_addr(**overrides: object) -> SimpleNamespace:
    defaults: dict[str, object] = {"addr": "10.9.8.7"}
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _adapter_if(**overrides: object) -> SimpleNamespace:
    defaults = {
        "name": "eth0",
        "id": "1",
        "mac": "AA:BB:CC:DD:EE:01",
        "switch_id": "A",
        "admin_state": "enabled",
        "oper_state": "up",
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


class TestComputeUnitToProviderServer:
    def test_full_mapping(self) -> None:
        blade = _blade()
        profile = _profile()
        template = _template()
        mgmt_if = _mgmt_if()
        adapter_if = _adapter_if()

        result = compute_unit_to_provider_server(
            blade,
            manager_id="mgr_ucsm_dc1",
            profile_by_dn={profile.dn: profile},
            template_dn_by_name={template.name: template.dn},
            mgmt_if=mgmt_if,
            mgmt_ip_by_parent_dn={},
            switches_by_id={},
            ext_eth_ifs=[adapter_if],
            host_eth_ifs=[],
            cpu_units=[],
            disk_units=[],
        )

        assert result.external_id == "sys/chassis-1/blade-3"
        assert result.vendor == "cisco"
        assert result.name == "blade-3"
        assert result.model == "UCSB-B200-M6"
        assert result.serial == "FCH12345678"
        assert result.system_uuid == "11111111-2222-3333-4444-555555555555"
        assert result.manager_id == "mgr_ucsm_dc1"
        assert result.profile_template_name == "worker-template"
        assert result.profile_template_external_id == "org-root/ls-template-worker-template"
        assert result.cpu_sockets == 2
        assert result.cpu_cores == 32
        assert result.cpu_threads == 64
        assert result.memory_total_bytes == 524288 * 1024 * 1024
        assert result.bmc_address_raw == "ipmi://10.1.2.3:623"
        assert result.bmc_mac == "00:11:22:33:44:55"
        assert result.nic_macs == ("AA:BB:CC:DD:EE:01",)
        assert len(result.attachments) == 1
        assert result.attachments[0].fabric == "A"
        assert result.attachments[0].server_interface == "eth0"
        assert result.attachments[0].interface_kind == "PHYSICAL"

    def test_no_assigned_profile_leaves_template_fields_none(self) -> None:
        blade = _blade(assigned_to_dn="")
        result = compute_unit_to_provider_server(
            blade,
            manager_id="mgr_1",
            profile_by_dn={},
            template_dn_by_name={},
            mgmt_if=None,
            mgmt_ip_by_parent_dn={},
            switches_by_id={},
            ext_eth_ifs=[],
            host_eth_ifs=[],
            cpu_units=[],
            disk_units=[],
        )
        assert result.profile_template_name is None
        assert result.profile_template_external_id is None

    def test_profile_without_a_template_leaves_template_fields_none(self) -> None:
        blade = _blade()
        profile = _profile(src_templ_name="")
        result = compute_unit_to_provider_server(
            blade,
            manager_id="mgr_1",
            profile_by_dn={profile.dn: profile},
            template_dn_by_name={},
            mgmt_if=None,
            mgmt_ip_by_parent_dn={},
            switches_by_id={},
            ext_eth_ifs=[],
            host_eth_ifs=[],
            cpu_units=[],
            disk_units=[],
        )
        assert result.profile_template_name is None
        assert result.profile_template_external_id is None

    def test_template_name_with_no_matching_template_falls_back_to_name(self) -> None:
        blade = _blade()
        profile = _profile()
        result = compute_unit_to_provider_server(
            blade,
            manager_id="mgr_1",
            profile_by_dn={profile.dn: profile},
            template_dn_by_name={},  # no lsServiceProfileTemplate matched
            mgmt_if=None,
            mgmt_ip_by_parent_dn={},
            switches_by_id={},
            ext_eth_ifs=[],
            host_eth_ifs=[],
            cpu_units=[],
            disk_units=[],
        )
        assert result.profile_template_name == "worker-template"
        assert result.profile_template_external_id == "worker-template"

    def test_no_mgmt_if_means_no_bmc_address(self) -> None:
        result = compute_unit_to_provider_server(
            _blade(),
            manager_id="mgr_1",
            profile_by_dn={},
            template_dn_by_name={},
            mgmt_if=None,
            mgmt_ip_by_parent_dn={},
            switches_by_id={},
            ext_eth_ifs=[],
            host_eth_ifs=[],
            cpu_units=[],
            disk_units=[],
        )
        assert result.bmc_address_raw is None
        assert result.bmc_mac is None

    @pytest.mark.parametrize("unset_ip", ["0.0.0.0", "none", "", None])  # noqa: S104 - sentinel value, not a bind
    def test_unset_cimc_ip_means_no_bmc_address(self, unset_ip: str | None) -> None:
        result = compute_unit_to_provider_server(
            _blade(),
            manager_id="mgr_1",
            profile_by_dn={},
            template_dn_by_name={},
            mgmt_if=_mgmt_if(ext_ip=unset_ip),
            mgmt_ip_by_parent_dn={},
            switches_by_id={},
            ext_eth_ifs=[],
            host_eth_ifs=[],
            cpu_units=[],
            disk_units=[],
        )
        assert result.bmc_address_raw is None

    def test_management_ip_pool_address_is_preferred_over_mgmt_if_ext_ip(self) -> None:
        """On real hardware `mgmtIf.ext_ip` was unset while the profile's
        management IP policy had assigned an address under the profile's
        own DN (docs/cisco-collectors.md; `ucs_common.management_ip_by_parent_dn`).
        """
        profile = _profile()
        result = compute_unit_to_provider_server(
            _blade(),
            manager_id="mgr_1",
            profile_by_dn={profile.dn: profile},
            template_dn_by_name={},
            mgmt_if=_mgmt_if(ext_ip="0.0.0.0"),  # noqa: S104 - sentinel, not a bind
            mgmt_ip_by_parent_dn={profile.dn: _mgmt_ip_addr(addr="10.9.8.7")},
            switches_by_id={},
            ext_eth_ifs=[],
            host_eth_ifs=[],
            cpu_units=[],
            disk_units=[],
        )
        assert result.bmc_address_raw == "ipmi://10.9.8.7:623"

    def test_management_ip_falls_back_to_the_physical_mgmt_controller_dn(self) -> None:
        """Schema-valid per `ucsmsdk`'s `mo_meta.parents`, but not
        confirmed populated on real hardware — kept as a fallback for a
        deployment that does use it.
        """
        result = compute_unit_to_provider_server(
            _blade(assigned_to_dn=""),
            manager_id="mgr_1",
            profile_by_dn={},
            template_dn_by_name={},
            mgmt_if=None,
            mgmt_ip_by_parent_dn={"sys/chassis-1/blade-3/mgmt": _mgmt_ip_addr(addr="10.5.5.5")},
            switches_by_id={},
            ext_eth_ifs=[],
            host_eth_ifs=[],
            cpu_units=[],
            disk_units=[],
        )
        assert result.bmc_address_raw == "ipmi://10.5.5.5:623"

    def test_mgmt_if_ext_ip_is_the_fallback_with_no_pooled_address(self) -> None:
        result = compute_unit_to_provider_server(
            _blade(),
            manager_id="mgr_1",
            profile_by_dn={},
            template_dn_by_name={},
            mgmt_if=_mgmt_if(ext_ip="10.1.2.3"),
            mgmt_ip_by_parent_dn={},
            switches_by_id={},
            ext_eth_ifs=[],
            host_eth_ifs=[],
            cpu_units=[],
            disk_units=[],
        )
        assert result.bmc_address_raw == "ipmi://10.1.2.3:623"

    def test_profile_dn_is_populated_from_the_assigned_service_profile(self) -> None:
        profile = _profile()
        result = compute_unit_to_provider_server(
            _blade(),
            manager_id="mgr_1",
            profile_by_dn={profile.dn: profile},
            template_dn_by_name={},
            mgmt_if=None,
            mgmt_ip_by_parent_dn={},
            switches_by_id={},
            ext_eth_ifs=[],
            host_eth_ifs=[],
            cpu_units=[],
            disk_units=[],
        )
        assert result.profile_dn == "org-root/ls-worker-01"

    def test_profile_dn_is_none_with_no_assigned_profile(self) -> None:
        result = compute_unit_to_provider_server(
            _blade(assigned_to_dn=""),
            manager_id="mgr_1",
            profile_by_dn={},
            template_dn_by_name={},
            mgmt_if=None,
            mgmt_ip_by_parent_dn={},
            switches_by_id={},
            ext_eth_ifs=[],
            host_eth_ifs=[],
            cpu_units=[],
            disk_units=[],
        )
        assert result.profile_dn is None

    def test_adapter_with_no_switch_id_is_not_an_attachment(self) -> None:
        result = compute_unit_to_provider_server(
            _blade(),
            manager_id="mgr_1",
            profile_by_dn={},
            template_dn_by_name={},
            mgmt_if=None,
            mgmt_ip_by_parent_dn={},
            switches_by_id={},
            ext_eth_ifs=[_adapter_if(switch_id="NONE")],
            host_eth_ifs=[],
            cpu_units=[],
            disk_units=[],
        )
        assert result.attachments == ()
        # The MAC is reported even with no fabric attachment.
        assert result.nic_macs == ("AA:BB:CC:DD:EE:01",)

    def test_multiple_adapters_preserve_order(self) -> None:
        result = compute_unit_to_provider_server(
            _blade(),
            manager_id="mgr_1",
            profile_by_dn={},
            template_dn_by_name={},
            mgmt_if=None,
            mgmt_ip_by_parent_dn={},
            switches_by_id={},
            ext_eth_ifs=[
                _adapter_if(name="eth0", mac="AA:00:00:00:00:00", switch_id="A"),
                _adapter_if(name="eth1", mac="AA:00:00:00:00:01", switch_id="B"),
            ],
            host_eth_ifs=[],
            cpu_units=[],
            disk_units=[],
        )
        assert result.nic_macs == ("AA:00:00:00:00:00", "AA:00:00:00:00:01")
        assert [a.fabric for a in result.attachments] == ["A", "B"]

    def test_physical_and_vnic_attachments_are_both_present_and_labeled(self) -> None:
        """A physical port and the vNIC carved from it can report the same
        fabric — `interface_kind` is what tells them apart, since
        `len(attachments)` alone would overcount physical uplinks.
        """
        result = compute_unit_to_provider_server(
            _blade(),
            manager_id="mgr_1",
            profile_by_dn={},
            template_dn_by_name={},
            mgmt_if=None,
            mgmt_ip_by_parent_dn={},
            switches_by_id={},
            ext_eth_ifs=[_adapter_if(name="ext-eth-1", switch_id="A")],
            host_eth_ifs=[_adapter_if(name="eth0", switch_id="A")],
            cpu_units=[],
            disk_units=[],
        )
        assert [(a.interface_kind, a.server_interface) for a in result.attachments] == [
            ("PHYSICAL", "ext-eth-1"),
            ("VNIC", "eth0"),
        ]

    @pytest.mark.parametrize(
        ("oper_state", "expected"),
        [
            ("error-disabled", "DOWN"),
            ("hardware-failure", "DOWN"),
            ("no-license", "DISABLED"),
            ("software-failure", "DOWN"),
            ("udld-aggr-down", "DOWN"),
        ],
    )
    def test_physical_oper_state_recognizes_the_full_ucsmsdk_enum(
        self, oper_state: str, expected: str
    ) -> None:
        """`AdaptorExtEthIf.OPER_STATE_*` has 13 values; only 8 were mapped
        before the 2026-09-07 live dry run. See ADR-0009's "Update
        (2026-09-07)".
        """
        result = compute_unit_to_provider_server(
            _blade(),
            manager_id="mgr_1",
            profile_by_dn={},
            template_dn_by_name={},
            mgmt_if=None,
            mgmt_ip_by_parent_dn={},
            switches_by_id={},
            ext_eth_ifs=[_adapter_if(oper_state=oper_state)],
            host_eth_ifs=[],
            cpu_units=[],
            disk_units=[],
        )
        [attachment] = result.attachments
        assert attachment.oper_state == expected

    def test_indeterminate_oper_state_stays_unknown_on_purpose(self) -> None:
        """Confirmed live 2026-09-07: `"indeterminate"` was 24% of one fleet's
        physical interfaces and is Cisco's own "cannot be determined" —
        deliberately unmapped (ADR-0009's "Update (2026-09-07)").
        """
        result = compute_unit_to_provider_server(
            _blade(),
            manager_id="mgr_1",
            profile_by_dn={},
            template_dn_by_name={},
            mgmt_if=None,
            mgmt_ip_by_parent_dn={},
            switches_by_id={},
            ext_eth_ifs=[_adapter_if(oper_state="indeterminate")],
            host_eth_ifs=[],
            cpu_units=[],
            disk_units=[],
        )
        [attachment] = result.attachments
        assert attachment.oper_state == "UNKNOWN"

    def test_fabric_model_and_serial_come_from_the_matching_network_element(self) -> None:
        switch_a = SimpleNamespace(id="A", model="UCS-FI-6454", serial="FCH2222A")
        result = compute_unit_to_provider_server(
            _blade(),
            manager_id="mgr_1",
            profile_by_dn={},
            template_dn_by_name={},
            mgmt_if=None,
            mgmt_ip_by_parent_dn={},
            switches_by_id={"A": switch_a},
            ext_eth_ifs=[_adapter_if(switch_id="A")],
            host_eth_ifs=[],
            cpu_units=[],
            disk_units=[],
        )
        [attachment] = result.attachments
        assert attachment.fabric_model == "UCS-FI-6454"
        assert attachment.fabric_serial == "FCH2222A"

    def test_no_matching_network_element_leaves_fabric_identity_none(self) -> None:
        result = compute_unit_to_provider_server(
            _blade(),
            manager_id="mgr_1",
            profile_by_dn={},
            template_dn_by_name={},
            mgmt_if=None,
            mgmt_ip_by_parent_dn={},
            switches_by_id={},
            ext_eth_ifs=[_adapter_if(switch_id="A")],
            host_eth_ifs=[],
            cpu_units=[],
            disk_units=[],
        )
        [attachment] = result.attachments
        assert attachment.fabric_model is None
        assert attachment.fabric_serial is None

    def test_nic_macs_prefer_the_logical_vnic_over_the_physical_port(self) -> None:
        """The OS binds to the vNIC's MAC (`eno1`/`eno2`), not the
        physical adapter port's burned-in one — see `_nic_macs`.
        """
        result = compute_unit_to_provider_server(
            _blade(),
            manager_id="mgr_1",
            profile_by_dn={},
            template_dn_by_name={},
            mgmt_if=None,
            mgmt_ip_by_parent_dn={},
            switches_by_id={},
            ext_eth_ifs=[_adapter_if(mac="6C:B2:AE:00:00:01", switch_id="A")],
            host_eth_ifs=[_adapter_if(mac="00:25:B5:00:00:01", switch_id="A")],
            cpu_units=[],
            disk_units=[],
        )
        assert result.nic_macs == ("00:25:B5:00:00:01",)

    def test_nic_macs_fall_back_to_the_physical_port_with_no_vnic(self) -> None:
        """A server with no service profile associated yet has no vNIC at
        all, but is still physically cabled — it should not report zero
        NICs just because it isn't associated.
        """
        result = compute_unit_to_provider_server(
            _blade(assigned_to_dn=""),
            manager_id="mgr_1",
            profile_by_dn={},
            template_dn_by_name={},
            mgmt_if=None,
            mgmt_ip_by_parent_dn={},
            switches_by_id={},
            ext_eth_ifs=[_adapter_if(mac="6C:B2:AE:00:00:01", switch_id="A")],
            host_eth_ifs=[],
            cpu_units=[],
            disk_units=[],
        )
        assert result.nic_macs == ("6C:B2:AE:00:00:01",)

    def test_nics_carry_name_mac_and_link_state_matching_nic_macs(self) -> None:
        """`nics` is the richer view behind `nic_macs` — same vNIC-first
        preference, one entry per counted MAC, named and stated too.
        """
        result = compute_unit_to_provider_server(
            _blade(),
            manager_id="mgr_1",
            profile_by_dn={},
            template_dn_by_name={},
            mgmt_if=None,
            mgmt_ip_by_parent_dn={},
            switches_by_id={},
            ext_eth_ifs=[_adapter_if(mac="6C:B2:AE:00:00:01", switch_id="A")],
            host_eth_ifs=[
                _adapter_if(
                    name="eth0", mac="00:25:B5:00:00:01", switch_id="A", oper_state="link-down"
                )
            ],
            cpu_units=[],
            disk_units=[],
        )
        [nic] = result.nics
        assert nic.name == "eth0"
        assert nic.mac == "00:25:B5:00:00:01"
        assert nic.link_state == "DOWN"
        assert nic.speed_mbps is None

    def test_attachments_include_both_physical_and_logical_interfaces(self) -> None:
        """Fabric-attachment coverage is unaffected by the `nic_macs`
        preference — UCSPE 4.2 showed most servers have only one of the
        two classes populated, so both still count toward connectivity.
        """
        result = compute_unit_to_provider_server(
            _blade(),
            manager_id="mgr_1",
            profile_by_dn={},
            template_dn_by_name={},
            mgmt_if=None,
            mgmt_ip_by_parent_dn={},
            switches_by_id={},
            ext_eth_ifs=[_adapter_if(name="eth0", switch_id="A")],
            host_eth_ifs=[_adapter_if(name="vnic0", switch_id="B")],
            cpu_units=[],
            disk_units=[],
        )
        assert [a.fabric for a in result.attachments] == ["A", "B"]

    @pytest.mark.parametrize(
        ("raw", "expected"), [("32", 32), (None, 0), ("not-a-number", 0), ("", 0)]
    )
    def test_numeric_fields_parse_defensively(self, raw: str | None, expected: int) -> None:
        result = compute_unit_to_provider_server(
            _blade(num_of_cpus=raw),
            manager_id="mgr_1",
            profile_by_dn={},
            template_dn_by_name={},
            mgmt_if=None,
            mgmt_ip_by_parent_dn={},
            switches_by_id={},
            ext_eth_ifs=[],
            host_eth_ifs=[],
            cpu_units=[],
            disk_units=[],
        )
        assert result.cpu_sockets == expected

    def test_rack_unit_shape_maps_the_same_way(self) -> None:
        # Same attribute set as computeBlade; `dn` alone identifies it.
        rack_unit = SimpleNamespace(
            dn="sys/rack-unit-1",
            name="rack-unit-1",
            model="UCSC-C220-M6",
            vendor="Cisco Systems Inc",
            serial="WZP99999999",
            uuid="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
            num_of_cpus="1",
            num_of_cores="16",
            num_of_threads="32",
            total_memory="131072",
            presence="equipped",
            oper_state="ok",
            assigned_to_dn="",
        )
        result = compute_unit_to_provider_server(
            rack_unit,
            manager_id="mgr_1",
            profile_by_dn={},
            template_dn_by_name={},
            mgmt_if=None,
            mgmt_ip_by_parent_dn={},
            switches_by_id={},
            ext_eth_ifs=[],
            host_eth_ifs=[],
            cpu_units=[],
            disk_units=[],
        )
        assert result.external_id == "sys/rack-unit-1"
        assert result.serial == "WZP99999999"
        assert result.cpu_sockets == 1


def _processor_unit(**overrides: object) -> SimpleNamespace:
    defaults = {
        "dn": "sys/chassis-1/blade-3/board/cpu-1",
        "presence": "equipped",
        "model": "Intel(R) Xeon(R) Gold 6338",
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _storage_disk(**overrides: object) -> SimpleNamespace:
    defaults = {
        "dn": "sys/chassis-1/blade-3/board/storage-SAS-1/disk-1",
        "presence": "equipped",
        "model": "UCS-HD12TB10K12G",
        "serial": "S3X0ABCD",
        "device_type": "HDD",
        "disk_state": "online",
        "size": "1144641",  # MB
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _psu_mo(**overrides: object) -> SimpleNamespace:
    defaults = {
        "dn": "sys/rack-unit-3/psu-1",
        "id": "1",
        "presence": "equipped",
        "model": "UCSC-PSU1-1050W",
        "serial": "LIT12345678",
        "oper_state": "operable",
        "power": "ok",
        "psu_wattage": "1050",
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _card_mo(**overrides: object) -> SimpleNamespace:
    defaults = {
        "dn": "sys/chassis-1/blade-3/board/graphics-card-1",
        "id": "1",
        "presence": "equipped",
        "model": "UCSC-GPU-A100",
        "vendor": "NVIDIA",
        "serial": "GPU12345678",
        "oper_state": "operable",
        "power": "ok",
        "pci_addr": "0000:af:00.0",
        "firmware_version": "96.00.5E.00.02",
        "temperature": "42.5",
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


class TestCpuAndStorage:
    def test_cpu_model_from_first_equipped_processor(self) -> None:
        result = compute_unit_to_provider_server(
            _blade(),
            manager_id="mgr_1",
            profile_by_dn={},
            template_dn_by_name={},
            mgmt_if=None,
            mgmt_ip_by_parent_dn={},
            switches_by_id={},
            ext_eth_ifs=[],
            host_eth_ifs=[],
            cpu_units=[
                _processor_unit(dn=".../cpu-2", presence="empty", model=""),
                _processor_unit(),
            ],
            disk_units=[],
        )
        assert result.cpu_model == "Intel(R) Xeon(R) Gold 6338"

    def test_no_equipped_processor_leaves_cpu_model_none(self) -> None:
        result = compute_unit_to_provider_server(
            _blade(),
            manager_id="mgr_1",
            profile_by_dn={},
            template_dn_by_name={},
            mgmt_if=None,
            mgmt_ip_by_parent_dn={},
            switches_by_id={},
            ext_eth_ifs=[],
            host_eth_ifs=[],
            cpu_units=[_processor_unit(presence="empty")],
            disk_units=[],
        )
        assert result.cpu_model is None

    def test_storage_drives_mapped_and_summed(self) -> None:
        result = compute_unit_to_provider_server(
            _blade(),
            manager_id="mgr_1",
            profile_by_dn={},
            template_dn_by_name={},
            mgmt_if=None,
            mgmt_ip_by_parent_dn={},
            switches_by_id={},
            ext_eth_ifs=[],
            host_eth_ifs=[],
            cpu_units=[],
            disk_units=[
                _storage_disk(dn="disk-1", size="1144641"),
                _storage_disk(dn="disk-2", size="1144641", device_type="SSD", disk_state="good"),
            ],
        )
        assert result.storage_drives is not None
        assert len(result.storage_drives) == 2
        first = result.storage_drives[0]
        assert first["id"] == "disk-1"
        assert first["model"] == "UCS-HD12TB10K12G"
        assert first["media_type"] == "HDD"
        assert first["health"] == "HEALTHY"
        assert first["capacity_bytes"] == 1144641 * 1024 * 1024
        assert result.storage_total_bytes == 2 * 1144641 * 1024 * 1024

    def test_non_equipped_disk_is_dropped(self) -> None:
        result = compute_unit_to_provider_server(
            _blade(),
            manager_id="mgr_1",
            profile_by_dn={},
            template_dn_by_name={},
            mgmt_if=None,
            mgmt_ip_by_parent_dn={},
            switches_by_id={},
            ext_eth_ifs=[],
            host_eth_ifs=[],
            cpu_units=[],
            disk_units=[_storage_disk(presence="empty")],
        )
        assert result.storage_drives == ()
        assert result.storage_total_bytes == 0

    def test_not_applicable_size_is_unknown_capacity_not_zero(self) -> None:
        result = compute_unit_to_provider_server(
            _blade(),
            manager_id="mgr_1",
            profile_by_dn={},
            template_dn_by_name={},
            mgmt_if=None,
            mgmt_ip_by_parent_dn={},
            switches_by_id={},
            ext_eth_ifs=[],
            host_eth_ifs=[],
            cpu_units=[],
            disk_units=[_storage_disk(size="not-applicable")],
        )
        assert result.storage_drives is not None
        assert result.storage_drives[0]["capacity_bytes"] is None
        assert result.storage_total_bytes == 0

    @pytest.mark.parametrize(
        ("disk_state", "expected"),
        [
            ("good", "HEALTHY"),
            ("predictive-failure", "WARNING"),
            ("rebuilding", "WARNING"),
            ("zeroing", "WARNING"),
            ("failed", "CRITICAL"),
            ("bad", "CRITICAL"),
            ("offline", "CRITICAL"),
            ("self-test-failed", "CRITICAL"),
            ("something-unmapped", "UNKNOWN"),
            ("", "UNKNOWN"),
            # Confirmed live 2026-09-07 (18117 disks): both are real
            # DISK_STATE_* values deliberately left unmapped — "unknown" is
            # no verdict, "NA" is not applicable. See ADR-0009's "Update (2026-09-07)".
            ("unknown", "UNKNOWN"),
            ("na", "UNKNOWN"),
        ],
    )
    def test_disk_health_mapping(self, disk_state: str, expected: str) -> None:
        result = compute_unit_to_provider_server(
            _blade(),
            manager_id="mgr_1",
            profile_by_dn={},
            template_dn_by_name={},
            mgmt_if=None,
            mgmt_ip_by_parent_dn={},
            switches_by_id={},
            ext_eth_ifs=[],
            host_eth_ifs=[],
            cpu_units=[],
            disk_units=[_storage_disk(disk_state=disk_state)],
        )
        assert result.storage_drives is not None
        assert result.storage_drives[0]["health"] == expected
        # An empty disk_state is None, per `health_detail`'s contract.
        assert result.storage_drives[0]["health_detail"] == (disk_state or None)

    def test_unmapped_device_type_is_unknown_media(self) -> None:
        result = compute_unit_to_provider_server(
            _blade(),
            manager_id="mgr_1",
            profile_by_dn={},
            template_dn_by_name={},
            mgmt_if=None,
            mgmt_ip_by_parent_dn={},
            switches_by_id={},
            ext_eth_ifs=[],
            host_eth_ifs=[],
            cpu_units=[],
            disk_units=[_storage_disk(device_type="unspecified")],
        )
        assert result.storage_drives is not None
        assert result.storage_drives[0]["media_type"] == "UNKNOWN"


class TestPsus:
    """`health` (from `oper_state`) and `oper_power` (from `power`) are both
    kept so a live run can show which tracks a real failure; only `health`
    reaches the `Psu` model, `oper_power` is dry-run-only (`mapping._psu`).
    """

    def test_an_equipped_psu_is_reported(self) -> None:
        result = compute_unit_to_provider_server(
            _blade(),
            manager_id="mgr_1",
            profile_by_dn={},
            template_dn_by_name={},
            mgmt_if=None,
            mgmt_ip_by_parent_dn={},
            switches_by_id={},
            ext_eth_ifs=[],
            host_eth_ifs=[],
            cpu_units=[],
            disk_units=[],
            psu_units=[_psu_mo()],
        )
        assert result.psus is not None
        assert len(result.psus) == 1
        psu = result.psus[0]
        assert psu["id"] == "1"
        assert psu["model"] == "UCSC-PSU1-1050W"
        assert psu["serial"] == "LIT12345678"
        assert psu["capacity_watts"] == 1050
        assert psu["health"] == "UP"
        assert psu["health_detail"] == "operable"
        assert psu["oper_power"] == "ok"

    def test_a_failed_psu_reports_down(self) -> None:
        result = compute_unit_to_provider_server(
            _blade(),
            manager_id="mgr_1",
            profile_by_dn={},
            template_dn_by_name={},
            mgmt_if=None,
            mgmt_ip_by_parent_dn={},
            switches_by_id={},
            ext_eth_ifs=[],
            host_eth_ifs=[],
            cpu_units=[],
            disk_units=[],
            psu_units=[_psu_mo(oper_state="inoperable", power="failed")],
        )
        assert result.psus is not None
        psu = result.psus[0]
        assert psu["health"] == "DOWN"
        assert psu["health_detail"] == "inoperable"
        assert psu["oper_power"] == "failed"

    def test_an_empty_psu_bay_is_not_reported_as_a_failed_psu(self) -> None:
        """A slot with no PSU installed is not a failed PSU — reporting it
        would permanently misreport `power.failed_psu_count` for any server
        with fewer PSUs than bays.
        """
        result = compute_unit_to_provider_server(
            _blade(),
            manager_id="mgr_1",
            profile_by_dn={},
            template_dn_by_name={},
            mgmt_if=None,
            mgmt_ip_by_parent_dn={},
            switches_by_id={},
            ext_eth_ifs=[],
            host_eth_ifs=[],
            cpu_units=[],
            disk_units=[],
            psu_units=[_psu_mo(presence="empty")],
        )
        assert result.psus == ()

    def test_no_psu_units_defaults_to_empty(self) -> None:
        """`psu_units` defaults to `()` so the many call sites that
        predate this field need no change.
        """
        result = compute_unit_to_provider_server(
            _blade(),
            manager_id="mgr_1",
            profile_by_dn={},
            template_dn_by_name={},
            mgmt_if=None,
            mgmt_ip_by_parent_dn={},
            switches_by_id={},
            ext_eth_ifs=[],
            host_eth_ifs=[],
            cpu_units=[],
            disk_units=[],
        )
        assert result.psus == ()

    def test_wattage_is_none_not_zero_when_unparseable(self) -> None:
        result = compute_unit_to_provider_server(
            _blade(),
            manager_id="mgr_1",
            profile_by_dn={},
            template_dn_by_name={},
            mgmt_if=None,
            mgmt_ip_by_parent_dn={},
            switches_by_id={},
            ext_eth_ifs=[],
            host_eth_ifs=[],
            cpu_units=[],
            disk_units=[],
            psu_units=[_psu_mo(psu_wattage=None)],
        )
        assert result.psus is not None
        assert result.psus[0]["capacity_watts"] is None


class TestGpus:
    """`graphicsCard`, not `coprocessorCard` — see `mapping._gpu` and
    docs/cisco-collectors.md, "GPUs (coprocessor cards vs. graphics cards)".
    """

    def test_an_equipped_gpu_is_reported(self) -> None:
        result = compute_unit_to_provider_server(
            _blade(),
            manager_id="mgr_1",
            profile_by_dn={},
            template_dn_by_name={},
            mgmt_if=None,
            mgmt_ip_by_parent_dn={},
            switches_by_id={},
            ext_eth_ifs=[],
            host_eth_ifs=[],
            cpu_units=[],
            disk_units=[],
            card_units=[_card_mo()],
        )
        assert result.gpus is not None
        assert len(result.gpus) == 1
        gpu = result.gpus[0]
        assert gpu["vendor"] == "NVIDIA"
        assert gpu["model"] == "UCSC-GPU-A100"
        assert gpu["serial"] == "GPU12345678"
        assert gpu["pci_address"] == "0000:af:00.0"
        assert gpu["firmware_version"] == "96.00.5E.00.02"
        assert gpu["health"] == "UP"
        assert gpu["health_detail"] == "operable"
        assert gpu["temperature_celsius"] == 42.5

    def test_a_failed_gpu_reports_down(self) -> None:
        result = compute_unit_to_provider_server(
            _blade(),
            manager_id="mgr_1",
            profile_by_dn={},
            template_dn_by_name={},
            mgmt_if=None,
            mgmt_ip_by_parent_dn={},
            switches_by_id={},
            ext_eth_ifs=[],
            host_eth_ifs=[],
            cpu_units=[],
            disk_units=[],
            card_units=[_card_mo(oper_state="inoperable")],
        )
        assert result.gpus is not None
        assert result.gpus[0]["health"] == "DOWN"
        assert result.gpus[0]["health_detail"] == "inoperable"

    def test_an_empty_gpu_slot_is_not_reported(self) -> None:
        result = compute_unit_to_provider_server(
            _blade(),
            manager_id="mgr_1",
            profile_by_dn={},
            template_dn_by_name={},
            mgmt_if=None,
            mgmt_ip_by_parent_dn={},
            switches_by_id={},
            ext_eth_ifs=[],
            host_eth_ifs=[],
            cpu_units=[],
            disk_units=[],
            card_units=[_card_mo(presence="empty")],
        )
        assert result.gpus == ()

    def test_no_card_units_defaults_to_empty(self) -> None:
        """`card_units` defaults to `()` so the many call sites that
        predate this field need no change.
        """
        result = compute_unit_to_provider_server(
            _blade(),
            manager_id="mgr_1",
            profile_by_dn={},
            template_dn_by_name={},
            mgmt_if=None,
            mgmt_ip_by_parent_dn={},
            switches_by_id={},
            ext_eth_ifs=[],
            host_eth_ifs=[],
            cpu_units=[],
            disk_units=[],
        )
        assert result.gpus == ()

    def test_not_applicable_temperature_is_none_not_a_parse_error(self) -> None:
        """The same sentinel `storageLocalDisk.size` uses for "no reading
        available" — not zero degrees, and not a crash.
        """
        result = compute_unit_to_provider_server(
            _blade(),
            manager_id="mgr_1",
            profile_by_dn={},
            template_dn_by_name={},
            mgmt_if=None,
            mgmt_ip_by_parent_dn={},
            switches_by_id={},
            ext_eth_ifs=[],
            host_eth_ifs=[],
            cpu_units=[],
            disk_units=[],
            card_units=[_card_mo(temperature="not-applicable")],
        )
        assert result.gpus is not None
        assert result.gpus[0]["temperature_celsius"] is None

    def test_missing_temperature_is_none(self) -> None:
        result = compute_unit_to_provider_server(
            _blade(),
            manager_id="mgr_1",
            profile_by_dn={},
            template_dn_by_name={},
            mgmt_if=None,
            mgmt_ip_by_parent_dn={},
            switches_by_id={},
            ext_eth_ifs=[],
            host_eth_ifs=[],
            cpu_units=[],
            disk_units=[],
            card_units=[_card_mo(temperature=None)],
        )
        assert result.gpus is not None
        assert result.gpus[0]["temperature_celsius"] is None

    def test_memory_and_power_telemetry_are_none_not_zero(self) -> None:
        """No field for any of these exists on `graphicsCard` or
        `graphicsController` — a capability ceiling, not a failure.
        """
        result = compute_unit_to_provider_server(
            _blade(),
            manager_id="mgr_1",
            profile_by_dn={},
            template_dn_by_name={},
            mgmt_if=None,
            mgmt_ip_by_parent_dn={},
            switches_by_id={},
            ext_eth_ifs=[],
            host_eth_ifs=[],
            cpu_units=[],
            disk_units=[],
            card_units=[_card_mo()],
        )
        assert result.gpus is not None
        gpu = result.gpus[0]
        for field in (
            "memory_bytes",
            "memory_type",
            "ecc_mode_enabled",
            "correctable_error_count",
            "uncorrectable_error_count",
            "power_watts",
        ):
            assert gpu[field] is None, field
