"""Pure-logic tests for the deterministic fake data generator — no I/O,
no database, safe for `tests/unit`.
"""

from __future__ import annotations

import re
from collections import Counter

from tools.run_collector import PROVIDER_FACTORIES, manager_for

from app.domain.enums import HealthSeverity, ManagerType, Vendor
from app.domain.models.hardware import Gpu
from app.domain.ports.credentials import ManagerConnection
from app.domain.value_objects.gpu_catalog import gpu_catalog
from app.domain.value_objects.site import parse_site_code, site_catalog
from app.infrastructure.mongodb.classification_rule_repository import default_system_rules
from app.infrastructure.providers.fake.generator import (
    COLLECTOR_TYPES,
    collector_for,
    generate_servers,
    list_managers,
    list_sites,
    manager_id_for,
    nic_slot_for,
    provider_type_for,
)

# Pinned to the shipped default so a reconfigured deployment cannot change
# what these tests assert.
SITES = site_catalog("")

_RULES = [
    (rule.installation_type.value, re.compile(rule.pattern)) for rule in default_system_rules(SITES)
]


def _installation_type(name: str) -> str:
    """
    The installation type the seeded rules give a hostname.

    Args:
        name (str): A generated hostname.

    Returns:
        str: An `InstallationType` value.
    """
    for installation_type, pattern in _RULES:
        if pattern.search(name):
            return installation_type
    return "UNCLASSIFIED"


# Every implemented collector is seeded — derived from `PROVIDER_FACTORIES`
# so a sixth collector fails here instead of quietly having no seed data
# (how OPENMANAGE went unseeded). A collector that cannot be shaped goes here.
_UNSEEDED_COLLECTORS: frozenset[ManagerType] = frozenset()


def test_the_seeder_shapes_every_implemented_collector() -> None:
    """A collector with no seeded shape cannot be looked at in the UI
    without live hardware, which is most of what the seeder is for.
    """
    expected = frozenset(PROVIDER_FACTORIES) - _UNSEEDED_COLLECTORS
    missing = expected - frozenset(COLLECTOR_TYPES)

    assert not missing, (
        f"COLLECTOR_TYPES is missing {sorted(t.value for t in missing)}. Give the "
        "collector a shape in the generator, or add it to _UNSEEDED_COLLECTORS "
        "with the reason."
    )


def test_same_seed_produces_byte_identical_output() -> None:
    first = list(generate_servers(seed=42, count=200))
    second = list(generate_servers(seed=42, count=200))
    assert first == second


def test_different_seed_produces_different_output() -> None:
    first = list(generate_servers(seed=1, count=50))
    second = list(generate_servers(seed=2, count=50))
    assert first != second


def test_count_is_respected() -> None:
    servers = list(generate_servers(seed=42, count=37))
    assert len(servers) == 37


def test_external_ids_are_unique() -> None:
    servers = list(generate_servers(seed=42, count=500))
    external_ids = [s.external_id for s in servers]
    assert len(external_ids) == len(set(external_ids))


def test_serials_are_unique_per_vendor() -> None:
    servers = list(generate_servers(seed=42, count=500))
    seen: set[tuple[str, str | None]] = set()
    for s in servers:
        key = (s.vendor, s.serial)
        assert key not in seen
        seen.add(key)


def test_system_uuids_are_unique() -> None:
    """`None` (an unreachable server never had its BMC's UUID read) is
    excluded — it means "not reported", not a duplicate empty value.
    """
    servers = list(generate_servers(seed=42, count=500))
    uuids = [s.system_uuid for s in servers if s.system_uuid is not None]
    assert len(uuids) == len(set(uuids))


def test_vendors_are_from_the_known_set() -> None:
    servers = list(generate_servers(seed=42, count=200))
    vendors = {s.vendor for s in servers}
    assert vendors <= {v.value for v in Vendor}
    assert "standalone" in vendors


def test_bmc_address_forms_match_the_collector_that_reported_them() -> None:
    """The address shape is the collector's, not the vendor's: UCS reports
    `ipmi://host:623`, Redfish composes a `redfish://` system URL.
    """
    servers = list(generate_servers(seed=42, count=300))
    for s in servers:
        assert s.bmc_address_raw is not None
        if s.vendor == "cisco":
            assert s.bmc_address_raw.startswith("ipmi://")
            assert s.bmc_address_raw.endswith(":623")
        else:
            assert s.bmc_address_raw.startswith("redfish://")
            assert s.bmc_address_raw.endswith("/redfish/v1/Systems/1")


def test_external_ids_match_the_collector_that_reported_them() -> None:
    """Each collector stamps its own identity: a Central-rooted DN, an
    `intersight/<moid>`, or a Redfish system URL — the same strings the
    real collectors correlate on.
    """
    for s in generate_servers(seed=42, count=300):
        collector = provider_type_for(s)
        if collector == ManagerType.UCS_CENTRAL.value:
            assert s.external_id.startswith("compute/sys-")
            assert "/blade-" in s.external_id or "/rack-unit-" in s.external_id
        elif collector == ManagerType.INTERSIGHT.value:
            assert s.external_id.startswith("intersight/")
            # A Moid is 24 hex characters.
            assert len(s.external_id[len("intersight/") :]) == 24
        elif collector == ManagerType.ONEVIEW.value:
            assert s.external_id.startswith("/rest/server-hardware/")
        else:
            assert s.external_id.startswith("redfish://")


def test_only_ucs_central_servers_carry_a_service_profile_dn() -> None:
    """`profile_dn` is UCS Manager's alone: a BMC has no service profiles and
    an Intersight `server.Profile` has no `Dn`, so only UCS Central's servers
    carry the org path a siteless name falls back to.
    """
    for s in generate_servers(seed=42, count=300):
        if provider_type_for(s) != ManagerType.UCS_CENTRAL.value:
            assert s.profile_dn is None
            continue
        assert s.profile_dn is not None
        assert s.profile_dn.startswith("org-root/org-")
        assert parse_site_code(s.profile_dn, SITES) is not None


def test_a_siteless_ucs_central_name_still_resolves_through_its_org_dn() -> None:
    """UCS Central is the one collector with an org path to fall back to; a
    siteless Intersight server really does end up unsited, a gap this
    fixture shows rather than papers over.
    """
    siteless = [
        s
        for s in generate_servers(seed=42, count=300)
        if provider_type_for(s) == ManagerType.UCS_CENTRAL.value
        and parse_site_code(s.name, SITES) is None
    ]
    assert siteless, "the siteless name family should reach UCS Central servers too"
    for s in siteless:
        assert parse_site_code(s.profile_dn, SITES) is not None


def test_each_collector_reports_gpus_to_its_own_ceiling() -> None:
    """UCS Central reads a GPU's identity, PCI address and temperature only;
    Intersight reads identity and has no telemetry field at all; Redfish and
    OneView read both. See docs/cisco-collectors.md.
    """
    servers = list(generate_servers(seed=42, count=300))
    by_collector: dict[str, list] = {}
    for s in servers:
        by_collector.setdefault(provider_type_for(s), []).append(s)

    with_gpus = [s for s in servers if s.gpus]
    assert with_gpus
    for s in with_gpus:
        assert s.gpus is not None
        for gpu in s.gpus:
            assert set(gpu) == set(Gpu.model_fields)
            Gpu.model_validate(gpu)

    intersight_gpus = [
        gpu for s in by_collector[ManagerType.INTERSIGHT.value] for gpu in (s.gpus or ())
    ]
    assert intersight_gpus, "some Intersight servers should carry GPUs"
    for gpu in intersight_gpus:
        # Telemetry is None, not zero — the API has no such field (ADR-0017).
        assert gpu["model"]
        assert gpu["temperature_celsius"] is None
        assert gpu["ecc_mode_enabled"] is None
        assert gpu["correctable_error_count"] is None

    central_gpus = [
        gpu for s in by_collector[ManagerType.UCS_CENTRAL.value] for gpu in (s.gpus or ())
    ]
    assert central_gpus, "some UCS Central servers should carry GPUs"
    for gpu in central_gpus:
        assert gpu["temperature_celsius"] is not None
        assert gpu["pci_address"] is not None
        assert gpu["ecc_mode_enabled"] is None
        assert gpu["power_watts"] is None


def test_no_collector_reports_a_gpus_memory_size() -> None:
    """VRAM is `GpuCatalog`'s to fill in at ingest (docs/adr/0021); a
    generator that pre-filled it would make a local run prove nothing about
    the catalog.
    """
    gpus = [gpu for s in generate_servers(seed=42, count=300) for gpu in (s.gpus or ())]
    assert gpus
    assert all(gpu["memory_bytes"] is None for gpu in gpus)


def test_gpu_identifiers_are_the_ones_their_management_plane_reports() -> None:
    """Cisco answers with a PID, a BMC with the vendor's model string, and
    `GpuCatalog` matches both. A fleet carrying only one kind would leave
    half the catalog unexercised locally.
    """
    catalog = gpu_catalog("")
    matched: dict[str, set[bool]] = {}
    # 500, not 300: UCS Central's slice is small enough that 300 can miss
    # its uncataloged GPUs entirely.
    for s in generate_servers(seed=42, count=500):
        collector = provider_type_for(s)
        for gpu in s.gpus or ():
            model = gpu["model"]
            assert isinstance(model, str)
            if collector in (ManagerType.UCS_CENTRAL.value, ManagerType.INTERSIGHT.value):
                assert model.startswith(("UCSC-", "UCSX-")), model
            else:
                assert not model.startswith(("UCSC-", "UCSX-")), model
            enriched = catalog.enrich(gpu)
            matched.setdefault(collector, set()).add(enriched["memory_bytes"] is not None)

    # Each collector must reach both a cataloged and an uncataloged card.
    for collector, outcomes in matched.items():
        assert outcomes == {True, False}, collector


def test_drive_health_uses_the_health_severity_vocabulary() -> None:
    """Providers normalize drive health onto `HealthSeverity` and
    `storage.failed_drive_count` counts CRITICAL; any other vocabulary here
    would make the seeded storage policy fire in dev and never in production.
    """
    allowed = {s.value for s in HealthSeverity}
    healths = {
        drive["health"]
        for s in generate_servers(seed=42, count=300)
        for drive in s.storage_drives or ()
    }
    assert healths <= allowed
    assert HealthSeverity.CRITICAL.value in healths


def test_only_cisco_servers_have_attachments() -> None:
    servers = list(generate_servers(seed=42, count=300))
    non_cisco_with_attachments = [s for s in servers if s.vendor != "cisco" and s.attachments]
    assert non_cisco_with_attachments == []
    physical_counts = {
        sum(1 for a in s.attachments if a.interface_kind == "PHYSICAL")
        for s in servers
        if s.vendor == "cisco"
    }
    assert physical_counts & {0, 1, 2, 4}


def test_cisco_attachments_report_both_physical_ports_and_vnics() -> None:
    """The UCS collector reports `adaptorExtEthIf` and `adaptorHostEthIf`
    together, told apart only by `interface_kind`. A fixture with just one
    kind would never catch code that conflates them.
    """
    attached = [s for s in generate_servers(seed=42, count=300) if s.attachments]
    assert attached
    for s in attached:
        kinds = {a.interface_kind for a in s.attachments}
        assert kinds == {"PHYSICAL", "VNIC"}
        assert all(a.provider == ManagerType.UCS_CENTRAL.value for a in s.attachments)


def test_fabric_names_follow_fi_a_b_convention() -> None:
    servers = list(generate_servers(seed=42, count=300))
    for s in servers:
        for attachment in s.attachments:
            assert attachment.fabric in ("A", "B")
            assert attachment.fabric_name is not None
            assert attachment.fabric_name.startswith(f"FI-{attachment.fabric}-")


def test_names_follow_expected_patterns() -> None:
    """Names must keep the shapes the real estate uses — `parse_site_code`
    and the seeded classification rules read them, so drift here would make
    every dev/CI fixture classify differently from production.
    """
    servers = list(generate_servers(seed=42, count=300))
    for s in servers:
        assert (
            s.name.startswith("ocp4-hypershift-")
            or s.name.startswith("ocp-")
            or s.name.startswith("ocp4-")
            or s.name.startswith("random-server-")
        ), s.name


def test_every_generated_name_resolves_to_a_site_or_is_deliberately_siteless() -> None:
    servers = list(generate_servers(seed=42, count=300))
    sited = 0
    for s in servers:
        site = parse_site_code(s.name, SITES)
        if site is None:
            assert s.name.startswith("random-server-"), s.name
        else:
            assert site in SITES
            sited += 1
    assert sited > len(servers) * 0.6


def test_generated_names_cover_every_site() -> None:
    names = [s.name for s in generate_servers(seed=42, count=300)]
    seen = {parse_site_code(n, SITES) for n in names} - {None}
    assert seen == set(SITES.codes)


def test_list_sites_and_managers_have_unique_names_and_ids() -> None:
    sites = list_sites()
    site_names = [s.name for s in sites]
    site_ids = [s.id for s in sites]
    assert len(site_names) == len(set(site_names))
    assert len(site_ids) == len(set(site_ids))

    managers = list_managers()
    manager_names = [m.name for m in managers]
    manager_ids = [m.id for m in managers]
    assert len(manager_names) == len(set(manager_names))
    assert len(manager_ids) == len(set(manager_ids))


def test_managers_are_exactly_the_implemented_collectors() -> None:
    """Seeding a manager type with no collector would invent a data path that
    cannot exist. Derived from `PROVIDER_FACTORIES`, never restated — a
    hand-written copy is the drift that left OPENMANAGE unseeded.
    """
    assert {m.type for m in list_managers()} == frozenset(PROVIDER_FACTORIES)


def test_manager_ids_match_what_a_real_collector_writes() -> None:
    """`tools.run_collector.manager_for` builds the same id, so a seeded
    fleet and a collected one share one manager document rather than two.
    """
    assert {m.id for m in list_managers()} == {
        manager_for(t, ManagerConnection(endpoint="e", username="u", password="p")).id
        for t in PROVIDER_FACTORIES
    }


def test_every_server_is_owned_by_the_collector_that_could_find_it() -> None:
    """And by exactly one: the two Cisco collectors partition the Cisco
    fleet rather than both claiming it, mirroring the `ManagementMode`
    split the real Intersight collector enforces.
    """
    seen: set[str] = set()
    for s in generate_servers(seed=42, count=300):
        expected = collector_for(s.vendor, s.model or "")
        assert provider_type_for(s) == expected.value
        assert s.manager_id == manager_id_for(expected)
        if s.vendor == "cisco":
            seen.add(expected.value)

    assert seen == {ManagerType.UCS_CENTRAL.value, ManagerType.INTERSIGHT.value}, (
        "Cisco servers should be split across both Cisco collectors"
    )


def test_servers_reference_known_managers() -> None:
    """`ProviderServer` no longer carries a site at all — it is derived
    from the name at ingest — so only the manager reference is checked
    here. Site coverage is asserted by the name tests above.
    """
    manager_ids = {m.id for m in list_managers()}
    servers = list(generate_servers(seed=42, count=300))
    for s in servers:
        assert s.manager_id in manager_ids


def test_site_documents_are_ided_by_their_bare_site_code() -> None:
    """A `Site` document's id must be exactly what `parse_site_code`
    yields, or a server's `site_id` would never join to its site.
    """
    assert {s.id for s in list_sites()} == set(SITES.codes)


def test_some_servers_have_a_profile_template_and_some_do_not() -> None:
    servers = list(generate_servers(seed=42, count=300))
    with_template = [s for s in servers if s.profile_template_name is not None]
    without_template = [s for s in servers if s.profile_template_name is None]
    assert with_template
    assert without_template


def test_profile_template_name_and_external_id_are_both_set_or_both_none() -> None:
    servers = list(generate_servers(seed=42, count=300))
    for s in servers:
        assert (s.profile_template_name is None) == (s.profile_template_external_id is None)


def test_every_collector_with_a_template_concept_can_report_one() -> None:
    """Every collector whose mapping populates `profile_template_name` must
    be seeded with one, or the UI's template field stays blank for that
    vendor (CLAUDE.md convention 10). A bare BMC has no template concept.
    """
    with_templates = {
        ManagerType.UCS_CENTRAL.value,
        ManagerType.INTERSIGHT.value,
        ManagerType.ONEVIEW.value,
        ManagerType.OPENMANAGE.value,
    }
    seen_with_template: set[str] = set()
    for s in generate_servers(seed=42, count=300):
        collector = provider_type_for(s)
        if collector == ManagerType.REDFISH_STANDALONE.value:
            assert s.profile_template_name is None
            continue
        if s.profile_template_name is not None:
            seen_with_template.add(collector)
            # The fake provider references a template by name, unlike production.
            assert s.profile_template_external_id == s.profile_template_name

    assert seen_with_template == with_templates


def test_hpe_servers_come_from_oneview_and_span_ilo_generations() -> None:
    """OneView owns the ProLiant fleet, and it spans the iLO-5/6
    generations it can inventory fully and the iLO-4 generation it
    cannot.
    """
    hp = [s for s in generate_servers(seed=42, count=300) if s.vendor == "hp"]
    assert hp
    assert all(provider_type_for(s) == ManagerType.ONEVIEW.value for s in hp)
    assert all((s.model or "").startswith("ProLiant DL") for s in hp)
    generations = {(s.model or "").split()[-1] for s in hp}
    assert {"Gen9", "Gen11"} <= generations


def test_an_ilo4_server_reports_identity_with_unread_hardware() -> None:
    """Every OneView subresource call against an iLO 4 fails, so a Gen9 is an
    identity-only record: `None`, never `()` or `0`, which would read as
    "no drives installed". See docs/hpe-collectors.md.
    """
    servers = list(generate_servers(seed=42, count=300))
    gen9 = [s for s in servers if (s.model or "").endswith("Gen9")]
    assert gen9
    for s in gen9:
        assert s.serial and s.model and s.system_uuid
        assert s.storage_drives is None
        assert s.storage_total_bytes is None
        assert s.gpus is None
        assert s.nic_macs is None
        assert s.psus is None

    # A Gen11 is fully inventoried, so the ceiling is the iLO's, not OneView's.
    gen11 = [
        s
        for s in servers
        if provider_type_for(s) == ManagerType.ONEVIEW.value and (s.model or "").endswith("Gen11")
    ]
    assert gen11
    assert any(s.storage_drives for s in gen11)


def test_some_dell_servers_are_seeded_unreachable() -> None:
    """`Server.reachable=False` (2026-09-10) only shows up for real on an
    `OPENMANAGE` run, so seeded data needs its own example — see
    `OpenManageProvider._unreachable_server`.
    """
    servers = list(generate_servers(seed=42, count=1000))
    unreachable = [s for s in servers if not s.reachable]
    assert unreachable
    assert all(provider_type_for(s) == ManagerType.OPENMANAGE.value for s in unreachable)
    for s in unreachable:
        assert s.name and s.serial
        assert s.cpu_sockets is None
        assert s.storage_drives is None
        assert s.psus is None

    # Most of the fleet stays reachable, or every other Dell assertion here breaks.
    reachable_dell = [
        s for s in servers if provider_type_for(s) == ManagerType.OPENMANAGE.value and s.reachable
    ]
    assert reachable_dell


def test_installation_types_are_a_visible_three_way_split() -> None:
    """Two landing on the same count reads as a bug, not a fleet property.
    Three, not four — UPI is unconditional since 2026-09-10.
    """
    counts = Counter(_installation_type(s.name) for s in generate_servers(seed=42, count=1000))
    assert set(counts) == {"HOSTED_CLUSTER", "MCE", "UPI"}
    assert all(count > 50 for count in counts.values())
    assert len(set(counts.values())) == 3


def test_the_unclassified_shaped_family_is_upi_now() -> None:
    """The siteless `random-server-*` minority still exercises "Unassigned
    site" — it just classifies UPI now, not UNCLASSIFIED.
    """
    unclassified_shaped = [
        s for s in generate_servers(seed=42, count=1000) if s.name.startswith("random-server-")
    ]
    assert unclassified_shaped
    assert all(_installation_type(s.name) == "UPI" for s in unclassified_shaped)
    assert all(parse_site_code(s.name, SITES) is None for s in unclassified_shaped)


def test_every_readable_server_reports_power_supplies() -> None:
    """Every collector populates `psus`; a fake fleet reporting none would
    flag `hardware.power.psus` unread on every seeded server. Health is the
    UP/DOWN vocabulary `power.failed_psu_count` counts, never `HealthSeverity`.
    """
    servers = list(generate_servers(seed=42, count=300))
    readable = [s for s in servers if s.psus is not None]
    assert readable

    for s in readable:
        assert s.psus
        for psu in s.psus:
            assert psu["health"] in {"UP", "DOWN", "DISABLED", "UNKNOWN"}
            assert isinstance(psu["capacity_watts"], int)

    assert any(psu["health"] == "DOWN" for s in readable for psu in (s.psus or ()))


def test_every_collector_with_macs_reports_per_interface_nics() -> None:
    """Every collector populates `nics` now (2026-09-10) — UCS Central and
    Intersight report a vNIC's own name, Redfish an FQDD.
    """
    by_collector: dict[str, list[bool]] = {}
    for server in generate_servers(seed=42, count=400, sites=SITES):
        # Unread MACs (an iLO 4) say nothing about the collector's shape.
        if not server.nic_macs:
            continue
        by_collector.setdefault(provider_type_for(server), []).append(bool(server.nics))

    assert by_collector, "the generator produced nothing to check"
    for collector, has_nics in by_collector.items():
        assert all(has_nics), f"{collector} should report per-interface nics"


def test_ucs_and_intersight_nics_are_eno_named_vnics() -> None:
    """The one shape unique to Cisco: no FQDD, no location — just the
    vNIC name `cisco_eno_names` keys its derived `enoN` label off.
    """
    fabric = {ManagerType.UCS_CENTRAL.value, ManagerType.INTERSIGHT.value}
    for server in generate_servers(seed=42, count=400, sites=SITES):
        if provider_type_for(server) not in fabric or not server.nics:
            continue
        assert [n.name for n in server.nics] == [f"eth{i}" for i in range(len(server.nics))]
        assert all(n.location is None for n in server.nics)
        assert all(n.speed_mbps is None for n in server.nics)


def test_a_nics_mac_is_one_the_server_reports_for_identity() -> None:
    """`nic_macs` is what identity correlation keys on and `nics` is the
    richer view of the same interfaces — a NIC carrying a MAC absent from
    `nic_macs` would describe a different server.
    """
    for server in generate_servers(seed=42, count=200, sites=SITES):
        for nic in server.nics:
            assert nic.mac in (server.nic_macs or ()), f"{server.name}: {nic.mac} is not its own"


def test_redfish_nic_names_carry_the_fqdd_kind_an_os_name_derives_from() -> None:
    """`NIC.Integrated.1` becomes an `eno...` name on a booted host and
    `NIC.Slot.8` becomes `ens8...`, so a seeded fleet that only ever
    produced one kind could not exercise that mapping at all.
    """
    kinds = {
        nic.name.split("-")[0]
        for server in generate_servers(seed=42, count=400, sites=SITES)
        for nic in server.nics
        if nic.name.startswith("NIC.")
    }

    assert any(k.startswith("NIC.Integrated") for k in kinds), kinds
    assert any(k.startswith("NIC.Slot") for k in kinds), kinds


def test_a_servers_nic_slot_matches_the_profile_its_name_declares() -> None:
    """The hostname token is the only thing that says which PCIe slot the
    add-in NIC is in, and the OS interface name derives from that slot;
    names and interfaces disagreeing would make that join untestable.
    """
    checked = 0
    for server in generate_servers(seed=42, count=1000, sites=SITES):
        slot = nic_slot_for(server.name)
        if slot is None or not server.nics:
            continue
        checked += 1
        assert [n.name for n in server.nics if n.name.startswith(f"NIC.Slot.{slot}-")], (
            f"{server.name} declares slot {slot} but its NICs are {[n.name for n in server.nics]}"
        )

    assert checked, "no seeded server carried a hardware profile token"


def test_the_add_in_cards_ports_are_the_third_and_fourth_macs() -> None:
    """Dell's two-port onboard LOM enumerates first, so an add-in card's ports
    are MACs three and four — the positional rule existing tooling selects
    on (`fake/generator.py`'s `_ONBOARD_PORTS`). Pins slot and position agree.
    """
    checked = 0
    for server in generate_servers(seed=42, count=1000, sites=SITES):
        if nic_slot_for(server.name) is None or len(server.nics) != 4:
            continue
        checked += 1
        onboard, card = server.nics[:2], server.nics[2:]
        assert all(n.name.startswith("NIC.Integrated.1-") for n in onboard), server.name
        assert all(n.name.startswith("NIC.Slot.") for n in card), server.name
        assert [n.mac for n in card] == list(server.nic_macs or ())[2:4], server.name

    assert checked, "no seeded server had an add-in card"


def test_a_server_with_no_profile_token_has_onboard_nics_only() -> None:
    """These boot as `eno...` rather than `ens...`, which is the whole
    reason the two FQDD kinds have to be distinguishable.
    """
    for server in generate_servers(seed=42, count=600, sites=SITES):
        if nic_slot_for(server.name) is not None or not server.nics:
            continue
        if not server.nics[0].name.startswith("NIC."):
            continue  # a OneView server, named by adapter and port
        assert all(n.name.startswith("NIC.Integrated.1-") for n in server.nics), (
            f"{server.name} has no profile token but reports {[n.name for n in server.nics]}"
        )
