"""`app.infrastructure.providers.intersight.mapping`.

Fixtures here are shaped from the API's published contract (the OpenAPI
schema as rendered by the `intersight` SDK's generated models), not
captured from a live tenant — no live Intersight call has ever been made
against this code. See docs/adr/0017-intersight-collector.md, which is
explicit about what that leaves unproven.

What these tests *can* prove is the part that is ours: that a field the
collector could not read stays `None`, that a physical uplink is not
counted as a vNIC, and that a server is named after its profile rather
than after its chassis slot — the three mistakes that were expensive on
UCS Manager.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.infrastructure.providers.intersight import mapping

pytestmark = pytest.mark.unit

_MIB = 1024 * 1024


def _summary(**overrides: Any) -> dict[str, Any]:
    """
    A `compute.PhysicalSummary` row.

    Args:
        **overrides: Fields to replace.

    Returns:
        dict[str, Any]: The row.
    """
    row = {
        "Moid": "6304b1f16176752d3193d1c1",
        "Dn": "sys/rack-unit-3",
        "Name": "UCSC-C240-M6SX-WZP24",
        "Model": "UCSC-C240-M6SX",
        "Serial": "WZP24140ABC",
        "Uuid": "1b1a1c1d-0000-4000-8000-000000000001",
        "Vendor": "Cisco Systems Inc",
        "TotalMemory": 524288,
        "NumCpus": 2,
        "NumCpuCores": 64,
        "NumThreads": 128,
        "MgmtIpAddress": "10.10.5.31",
        "ManagementMode": "Intersight",
    }
    row.update(overrides)
    return row


def _ref(moid: str) -> dict[str, str]:
    """
    An `mo.MoRef` relationship value.

    Args:
        moid (str): The referenced object.

    Returns:
        dict[str, str]: The reference.
    """
    return {"ClassId": "mo.MoRef", "ObjectType": "compute.RackUnit", "Moid": moid}


def test_a_server_is_named_after_its_service_profile() -> None:
    """`PhysicalSummary.Name` is never an operator hostname: it would name
    every server after its model and slot, which carries no site token and
    matches no `^ocp` pattern. See ADR-0017.
    """
    name = mapping.server_name(_summary(), {"Name": "ocp4-prod-tlv-infra-01"})
    assert name == "ocp4-prod-tlv-infra-01"


def test_a_server_with_no_profile_falls_back_to_its_label_then_its_name() -> None:
    """A standalone-claimed CIMC has no profile at all, so the operator's
    own label is the best name available before the summary's.
    """
    assert mapping.server_name(_summary(UserLabel="ocp4-nyc-worker-07"), None) == (
        "ocp4-nyc-worker-07"
    )
    assert mapping.server_name(_summary(), None) == "UCSC-C240-M6SX-WZP24"


def test_an_empty_profile_name_does_not_shadow_the_fallback() -> None:
    """An assigned but unnamed profile must not blank out the name."""
    assert mapping.server_name(_summary(UserLabel="ocp4-tlv-01"), {"Name": "  "}) == "ocp4-tlv-01"


def test_memory_is_converted_from_the_reported_unit() -> None:
    """`TotalMemory` carries no documented unit anywhere in the contract
    (ADR-0017, UNVERIFIED item 1). It is read as 2**20 bytes, matching
    what `..ucs_manager.mapping` already assumes for the same hardware.
    """
    assert mapping.memory_total_bytes(_summary(TotalMemory=524288)) == 524288 * _MIB


def test_unreported_memory_is_none_rather_than_zero() -> None:
    """Zero memory would evaluate health policies against a lie."""
    assert mapping.memory_total_bytes(_summary(TotalMemory=None)) is None
    summary = _summary()
    del summary["TotalMemory"]
    assert mapping.memory_total_bytes(summary) is None


def test_a_drive_prefers_the_byte_denominated_size() -> None:
    """`NonCoercedSizeBytes` names its own unit; `Size` is a string
    documented as MB. Preferring the former removes an assumption.
    """
    drive = mapping.drive({"DiskId": "1", "NonCoercedSizeBytes": 960197124096, "Size": "915715"})
    assert drive["capacity_bytes"] == 960197124096


def test_a_drive_falls_back_to_the_mb_string() -> None:
    """Cisco reports several sizes as strings rather than numbers."""
    assert mapping.drive({"DiskId": "1", "Size": "915715"})["capacity_bytes"] == 915715 * _MIB


def test_a_drive_with_no_reported_size_is_none() -> None:
    """Not zero: a zero-byte drive is a different claim from an unread one."""
    assert mapping.drive({"DiskId": "1"})["capacity_bytes"] is None


@pytest.mark.parametrize(
    ("disk", "expected"),
    [
        ({"Health": "Good"}, "HEALTHY"),
        ({"Health": "OK"}, "HEALTHY"),
        ({"Health": "Warning"}, "WARNING"),
        ({"Health": "Critical"}, "CRITICAL"),
        ({"DriveState": "Online"}, "HEALTHY"),
        ({"DriveState": "Failed"}, "CRITICAL"),
        ({"FailurePredicted": "true"}, "WARNING"),
        ({}, "UNKNOWN"),
        ({"Health": "something-new"}, "UNKNOWN"),
    ],
)
def test_drive_health_uses_the_platform_vocabulary(disk: dict[str, Any], expected: str) -> None:
    """`CRITICAL`, not `FAILED`: the seeded `storage.failed_drive` policy
    counts drives whose health is CRITICAL, and a vocabulary no collector
    emits makes that policy permanently silent.
    """
    assert mapping.drive(disk)["health"] == expected


@pytest.mark.parametrize(
    ("disk", "expected"),
    [
        ({"Health": "Critical"}, "Critical"),
        ({"DriveState": "Failed"}, "Failed"),
        ({"Health": "OK", "DriveState": "Online"}, "OK"),  # Health wins when both present
        ({}, None),
    ],
)
def test_drive_health_detail_names_whichever_field_decided_the_tier(
    disk: dict[str, Any], expected: str | None
) -> None:
    """The raw state `health` was reduced from — same `Health`-then-
    `DriveState` precedence `_drive_health` itself uses.
    """
    assert mapping.drive(disk)["health_detail"] == expected


def test_drive_health_recognizes_the_live_intersight_ok_spelling() -> None:
    """Confirmed live 2026-09-07: 216 of 226 sampled drives report
    `Health: "OK"`, which no healthy spelling covered, so all read UNKNOWN.
    See ADR-0017, "A second field pass (2026-09-07)".
    """
    unit = mapping.drive({"DiskId": "1", "Health": "OK"})
    assert unit["health"] == "HEALTHY"


def test_a_physical_uplink_and_a_vnic_are_told_apart() -> None:
    """Both report the same fabric, so a server's physical port count is
    not derivable without this — the ADR-0009 defect that made physical
    port counts always zero.
    """
    server = mapping.to_provider_server(
        _summary(),
        provider_type="INTERSIGHT",
        manager_id="mgr_intersight",
        ext_interfaces=[
            {"SwitchId": "A", "ExtEthInterfaceId": "1", "MacAddress": "00:11:22:33:44:00"}
        ],
        host_interfaces=[{"Name": "eth0", "MacAddress": "00:11:22:33:44:01"}],
    )
    kinds = [a.interface_kind for a in server.attachments]
    assert kinds == ["PHYSICAL", "VNIC"]


def test_an_uncabled_uplink_is_skipped() -> None:
    """An `ExtEthInterface` reporting no fabric is not attached to one."""
    server = mapping.to_provider_server(
        _summary(),
        provider_type="INTERSIGHT",
        manager_id="mgr_intersight",
        ext_interfaces=[{"ExtEthInterfaceId": "1"}, {"SwitchId": "B", "ExtEthInterfaceId": "2"}],
        host_interfaces=[],
    )
    assert [a.fabric for a in server.attachments] == ["B"]


def test_attachment_speed_is_none_rather_than_guessed() -> None:
    """Neither interface class carries a numeric speed, and the
    switch-side ports report a free-form string of unverified format.
    """
    server = mapping.to_provider_server(
        _summary(),
        provider_type="INTERSIGHT",
        manager_id="mgr_intersight",
        ext_interfaces=[{"SwitchId": "A", "ExtEthInterfaceId": "1"}],
    )
    assert server.attachments[0].speed_mbps is None


def test_interface_states_use_the_shared_cisco_vocabulary() -> None:
    """The same mapping UCS Manager uses, from `..ucs_common`."""
    server = mapping.to_provider_server(
        _summary(),
        provider_type="INTERSIGHT",
        manager_id="mgr_intersight",
        ext_interfaces=[
            {"SwitchId": "A", "ExtEthInterfaceId": "1", "AdminState": "enabled", "OperState": "up"}
        ],
    )
    assert server.attachments[0].admin_state == "ENABLED"
    assert server.attachments[0].oper_state == "UP"


def test_vnic_macs_are_preferred_over_physical_port_macs() -> None:
    """A vNIC MAC is what an OS on the server actually reports."""
    server = mapping.to_provider_server(
        _summary(),
        provider_type="INTERSIGHT",
        manager_id="mgr_intersight",
        ext_interfaces=[{"SwitchId": "A", "MacAddress": "00:11:22:33:44:00"}],
        host_interfaces=[{"Name": "eth0", "MacAddress": "00:AA:BB:CC:DD:EE"}],
    )
    assert server.nic_macs == ("00:aa:bb:cc:dd:ee",)


def test_physical_macs_stand_in_when_there_are_no_vnics() -> None:
    """A standalone server has no vNICs at all."""
    server = mapping.to_provider_server(
        _summary(),
        provider_type="INTERSIGHT",
        manager_id="mgr_intersight",
        ext_interfaces=[{"SwitchId": "A", "MacAddress": "00:11:22:33:44:00"}],
        host_interfaces=[],
    )
    assert server.nic_macs == ("00:11:22:33:44:00",)


def test_nics_carry_name_mac_and_link_state_matching_nic_macs() -> None:
    """`nics` is the richer view behind `nic_macs` — same vNIC-first
    preference, one entry per counted MAC, named and stated too.
    """
    server = mapping.to_provider_server(
        _summary(),
        provider_type="INTERSIGHT",
        manager_id="mgr_intersight",
        ext_interfaces=[{"SwitchId": "A", "MacAddress": "00:11:22:33:44:00"}],
        host_interfaces=[{"Name": "eth0", "MacAddress": "00:AA:BB:CC:DD:EE", "OperState": "down"}],
    )
    [nic] = server.nics
    assert nic.name == "eth0"
    assert nic.mac == "00:AA:BB:CC:DD:EE"
    assert nic.link_state == "DOWN"
    assert nic.speed_mbps is None


def test_an_unqueried_subresource_is_none_not_empty() -> None:
    """`IngestService` carries a stored value forward for `None` and
    overwrites for a real value. Collapsing the two once wrote zero
    drives over a real inventory and reported a failed disk as recovered.
    """
    server = mapping.to_provider_server(
        _summary(), provider_type="INTERSIGHT", manager_id="mgr_intersight"
    )
    assert server.storage_drives is None
    assert server.gpus is None
    assert server.nic_macs is None
    assert server.storage_total_bytes is None
    assert server.psus is None


def test_a_queried_but_empty_subresource_is_empty_not_none() -> None:
    """ "Read it, there are none" is a real claim and must be recorded."""
    server = mapping.to_provider_server(
        _summary(),
        provider_type="INTERSIGHT",
        manager_id="mgr_intersight",
        disks=[],
        cards=[],
        host_interfaces=[],
        ext_interfaces=[],
        psus=[],
    )
    assert server.storage_drives == ()
    assert server.gpus == ()
    assert server.nic_macs == ()
    assert server.psus == ()


def test_gpu_telemetry_is_none_because_the_api_has_none() -> None:
    """A capability ceiling, not a gap: this API version carries no GPU
    memory, temperature, power or ECC field at all (ADR-0017,
    "Decision 5"). Reporting zeros would look like a healthy idle GPU.
    """
    gpu = mapping.gpu(
        {"Model": "NVIDIA A100", "Vendor": "NVIDIA", "Serial": "GPU-1", "OperState": "operable"}
    )
    assert gpu["model"] == "NVIDIA A100"
    assert gpu["health"] == "UP"
    assert gpu["health_detail"] == "operable"
    for absent in (
        "memory_bytes",
        "memory_type",
        "ecc_mode_enabled",
        "correctable_error_count",
        "uncorrectable_error_count",
        "temperature_celsius",
        "power_watts",
    ):
        assert gpu[absent] is None, absent


def test_cpu_model_is_the_first_socket_with_a_reported_model() -> None:
    """Mirrors `ucs_manager.mapping._cpu_model`'s first-equipped-socket rule
    without filtering on `Presence`, whose equipped-value string is
    unverified (ADR-0017's UNVERIFIED list, item 10).
    """
    server = mapping.to_provider_server(
        _summary(),
        provider_type="INTERSIGHT",
        manager_id="mgr_intersight",
        processors=[{"Model": ""}, {"Model": "UCS-CPU-I6338"}],
    )
    assert server.cpu_model == "UCS-CPU-I6338"


def test_cpu_model_is_none_when_no_socket_reports_one() -> None:
    """An empty socket (or a table with none) contributes nothing."""
    server = mapping.to_provider_server(
        _summary(),
        provider_type="INTERSIGHT",
        manager_id="mgr_intersight",
        processors=[{"Model": None}],
    )
    assert server.cpu_model is None


def test_cpu_model_is_none_when_the_table_was_not_queried() -> None:
    """`cpu_model` is a scalar with no "queried but empty" state distinct from
    "not queried" — either means "could not determine it", and
    `IngestService` carries the stored value forward for both.
    """
    server = mapping.to_provider_server(
        _summary(), provider_type="INTERSIGHT", manager_id="mgr_intersight"
    )
    assert server.cpu_model is None


def test_psu_health_uses_the_oper_state_vocabulary_not_ok_failed() -> None:
    """`Psu.health` is the OperState vocabulary (UP/DOWN/DISABLED/UNKNOWN),
    the one `power.failed_psu_count` counts — never a literal "OK"/"FAILED"
    pair.
    """
    healthy = mapping.psu(
        {"PsuId": "1", "Model": "PSU-750W", "Serial": "PSU-1", "OperState": "operable"}
    )
    failed = mapping.psu({"PsuId": "2", "OperState": "inoperable"})
    assert healthy["health"] == "UP"
    assert healthy["health_detail"] == "operable"
    assert failed["health"] == "DOWN"
    assert failed["health_detail"] == "inoperable"


def test_psu_health_recognizes_the_live_intersight_ok_spelling() -> None:
    """Confirmed live 2026-09-07: 38 PSUs split `"OK"` (36) / `"Operable"`
    (2) for one healthy state, and `_OPER_STATE_MAP` had no `"OK"` entry.
    See ADR-0017, "A second field pass (2026-09-07)".
    """
    unit = mapping.psu({"PsuId": "1", "OperState": "OK"})
    assert unit["health"] == "UP"


def test_psu_capacity_and_id() -> None:
    unit = mapping.psu({"PsuId": "1", "Model": "PSU-750W", "Serial": "PSU-1", "PsuWattage": "750"})
    assert unit["id"] == "1"
    assert unit["model"] == "PSU-750W"
    assert unit["serial"] == "PSU-1"
    assert unit["capacity_watts"] == 750


def test_psu_falls_back_to_moid_when_psuid_is_absent() -> None:
    unit = mapping.psu({"Moid": "psu-moid-1"})
    assert unit["id"] == "psu-moid-1"


def test_external_id_is_the_moid_and_names_its_source() -> None:
    """Unique across the tenant, and unambiguous beside a UCS Central DN."""
    assert mapping.external_id(_summary()) == "intersight/6304b1f16176752d3193d1c1"


def test_the_bmc_address_is_a_uri_the_platform_already_parses() -> None:
    """The same `ipmi://host:623` form the UCS Manager collector emits,
    so `parse_bmc_address` needs no Intersight-specific branch.
    """
    assert mapping.bmc_address(_summary(), None) == "ipmi://10.10.5.31:623"


def test_an_unset_bmc_address_sentinel_is_not_an_address() -> None:
    """Cisco reports an unconfigured interface as 0.0.0.0."""
    unset = "0.0.0.0"  # noqa: S104 - Cisco's unset-IP sentinel, not a bind address
    assert mapping.bmc_address(_summary(MgmtIpAddress=unset), None) is None
    assert mapping.bmc_address(_summary(MgmtIpAddress=""), None) is None


def test_the_management_interface_wins_over_the_summarys_own_field() -> None:
    """`bmc_mac` comes from the interface, so the address must too, or the
    pair describes no single interface. The summary is the fallback when no
    interface was read.
    """
    assert mapping.bmc_address(_summary(), {"IpAddress": "10.10.5.99"}) == "ipmi://10.10.5.99:623"
    assert mapping.bmc_address(_summary(), None) == "ipmi://10.10.5.31:623"
    assert mapping.bmc_address(_summary(), {"MacAddress": "aa:bb"}) == "ipmi://10.10.5.31:623"


def test_a_ucsm_managed_server_keeps_its_service_profile_dn() -> None:
    """`ServiceProfile` is only populated in UCSM mode, and is the site
    fallback `parse_site_code` reads when a name carries no site token.
    """
    server = mapping.to_provider_server(
        _summary(ManagementMode="UCSM", ServiceProfile="org-root/org_tlv/ls-worker-01"),
        provider_type="INTERSIGHT",
        manager_id="mgr_intersight",
    )
    assert server.profile_dn == "org-root/org_tlv/ls-worker-01"


def test_the_profile_template_is_carried_through() -> None:
    """Both halves, so the UI can name the template and link to it."""
    server = mapping.to_provider_server(
        _summary(),
        provider_type="INTERSIGHT",
        manager_id="mgr_intersight",
        profile={"Name": "ocp4-tlv-01", "Dn": "profile/1"},
        template={"Name": "ocp-worker-template", "Moid": "abc123"},
    )
    assert server.profile_template_name == "ocp-worker-template"
    assert server.profile_template_external_id == "abc123"


def test_the_vendor_is_always_cisco() -> None:
    """Correlation is on `(vendor, serial_normalized)`, so a server that
    moved between Cisco collectors must stay one document.
    """
    server = mapping.to_provider_server(
        _summary(), provider_type="INTERSIGHT", manager_id="mgr_intersight"
    )
    assert server.vendor == "cisco"


def test_storage_total_sums_only_the_drives_that_reported_a_size() -> None:
    """A drive with no size contributes nothing rather than discarding
    the total for the drives that did report.
    """
    server = mapping.to_provider_server(
        _summary(),
        provider_type="INTERSIGHT",
        manager_id="mgr_intersight",
        disks=[
            {"DiskId": "1", "NonCoercedSizeBytes": 1000},
            {"DiskId": "2"},
            {"DiskId": "3", "NonCoercedSizeBytes": 2000},
        ],
    )
    assert server.storage_total_bytes == 3000


def test_a_moref_resolves_and_an_unset_one_does_not() -> None:
    """Every fleet-wide join in the provider turns on this one function."""
    assert mapping.moref(_ref("abc")) == "abc"
    assert mapping.moref(None) is None
    assert mapping.moref({"ClassId": "mo.MoRef"}) is None


def test_a_ucsm_server_is_named_from_its_service_profile_dn() -> None:
    """A UCSM-managed server has no `server.Profile`, only the summary's
    `ServiceProfile` DN; falling back to `PhysicalSummary.Name` (a chassis
    slot) would make a `UCSM` management-mode override silently collect nothing.
    """
    summary = _summary(
        ManagementMode="UCSM",
        ServiceProfile="org-root/org_tlv/ls-ocp4-prod-tlv-infra-01",
        Name="FI-cluster-A/chassis-3/blade-7",
    )
    assert mapping.server_name(summary, None) == "ocp4-prod-tlv-infra-01"


@pytest.mark.parametrize(
    ("dn", "expected"),
    [
        ("org-root/org_tlv/ls-worker-01", "worker-01"),
        ("org-root/ls-a", "a"),
        ("org-root/org_tlv", None),
        ("", None),
        (None, None),
        ("org-root/ls-", None),
    ],
)
def test_a_profile_name_is_read_off_the_dns_last_component(
    dn: str | None, expected: str | None
) -> None:
    """Same `ls-<name>` shape `ucs_manager.mapping` already reads."""
    assert mapping.profile_name_from_dn(dn) == expected


def test_a_real_profile_still_beats_the_dn() -> None:
    """An IMM server has both in principle; the object is authoritative."""
    summary = _summary(ServiceProfile="org-root/ls-from-dn")
    assert mapping.server_name(summary, {"Name": "from-profile"}) == "from-profile"


def test_macs_stay_unread_when_one_nic_table_failed() -> None:
    """`adapter/ExtEthInterfaces` failing while `HostEthInterfaces` succeeds
    on a server with no vNICs must not report `()` — `IngestService` would
    overwrite the stored MACs with nothing (ADR-0016).
    """
    server = mapping.to_provider_server(
        _summary(),
        provider_type="INTERSIGHT",
        manager_id="mgr_intersight",
        host_interfaces=[],
        ext_interfaces=None,
    )
    assert server.nic_macs is None


def test_macs_are_empty_only_when_both_tables_were_read() -> None:
    """ "Read both, found none" is a real claim and must be recorded."""
    server = mapping.to_provider_server(
        _summary(),
        provider_type="INTERSIGHT",
        manager_id="mgr_intersight",
        host_interfaces=[],
        ext_interfaces=[],
    )
    assert server.nic_macs == ()


def test_macs_from_the_surviving_table_are_still_reported() -> None:
    """A failed sibling must not discard MACs that were actually read."""
    server = mapping.to_provider_server(
        _summary(),
        provider_type="INTERSIGHT",
        manager_id="mgr_intersight",
        host_interfaces=None,
        ext_interfaces=[{"SwitchId": "A", "MacAddress": "00:11:22:33:44:00"}],
    )
    assert server.nic_macs == ("00:11:22:33:44:00",)
