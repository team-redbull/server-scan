"""Deterministic fake inventory data.

Produces `ProviderServer` DTOs in the shapes the four collectors that
actually exist emit, so dev and CI fixtures exercise the same field
shapes, vocabularies and absences production does:

* `UCS_CENTRAL` — Cisco blades, which live in a UCS domain registered
  with Central.
* `INTERSIGHT` — Cisco rack units, the ones claimed directly into
  Intersight rather than into a UCS domain. This mirrors the real
  partition: the Intersight collector excludes `ManagementMode == UCSM`
  precisely because UCS Central already owns those servers
  (docs/adr/0017-intersight-collector.md, "Decision 3").
* `ONEVIEW` — HPE ProLiant servers an HPE OneView appliance owns.
* `REDFISH_STANDALONE` — everything else, one BMC at a time.

A field a collector cannot read is `None` here too, and each collector's
absences differ, which is the point: UCS Central reports a GPU's identity
and its temperature but no memory, Intersight reports the identity with
no telemetry at all, only Redfish reports both — and OneView reports
nothing but identity for an iLO-4 server. A fixture richer than the real
thing hides exactly the gaps worth seeing.

No collector reports a GPU's VRAM (see docs/adr/0021), so no fake server
does either: every GPU here carries `memory_bytes=None` and the number
the UI shows comes from `GpuCatalog` at ingest, exactly as in production.

Determinism: every random choice goes through a single `random.Random(seed)`
instance threaded through in a fixed order, so `generate_servers(seed=42,
count=1000)` produces byte-identical `ProviderServer` output on every run,
on every machine — this is what makes `tools/seed_inventory.py --seed 42`
reproducible and is asserted directly in
`tests/unit/infrastructure/providers/test_generator.py`.
"""

from __future__ import annotations

import random
import uuid
import zlib
from collections.abc import Iterator
from dataclasses import replace

from app.domain.enums import ManagerType
from app.domain.models.common import AuditFields
from app.domain.models.manager import Manager
from app.domain.models.site import Site
from app.domain.ports.provider import ProviderAttachment, ProviderNic, ProviderServer
from app.domain.value_objects.site import SiteCatalog, site_catalog

# --- Fixed reference universe -----------------------------------------
#
# Managers are a small, fixed set independent of `seed`/`count` —
# re-seeding with a different seed or count must not create duplicate
# manager documents (unique index on `name`). Only the *servers*
# generated vary with `seed`/`count`.
#
# Sites are not fixed here at all: they come from the configured
# `SiteCatalog`, so a deployment that reconfigures INVENTORY_SITES gets
# fake data whose hostnames carry ITS site tokens. Seeding names built
# from some other estate's sites would produce a fleet that is entirely
# "Unassigned" — which is exactly the bug this generator exists to catch
# rather than create.
_DEFAULT_SITES = site_catalog("")

# The manager types that have a collector. `OPENMANAGE` has a
# configuration slot and no implementation, so seeding servers behind it
# would invent a data path that cannot exist.
COLLECTOR_TYPES: tuple[ManagerType, ...] = (
    ManagerType.UCS_CENTRAL,
    ManagerType.INTERSIGHT,
    ManagerType.OPENMANAGE,
    ManagerType.ONEVIEW,
    ManagerType.REDFISH_STANDALONE,
)


def _site_id(code: str) -> str:
    """A site's id *is* its code.

    No `site_fake_<code>` prefix: a server's site is derived from its name
    (`app.domain.value_objects.site.parse_site_code`), which can only ever
    yield a bare `SiteCode`. Prefixing the `Site` document's id would mean
    the id a server carries and the id a site document has are different
    strings — which is exactly the mismatch that made filtering by site
    silently return nothing.

    Args:
        code (str): A `SiteCode` value.

    Returns:
        str: The `Site` document id.
    """
    return code


def manager_id_for(manager_type: ManagerType) -> str:
    """
    The `Manager` document id a collector of this type writes.

    Deliberately identical to `tools.run_collector.manager_for`'s id, so a
    seeded fleet and a really-collected one resolve `Server.manager_id`
    through the same document instead of two near-duplicates.

    Args:
        manager_type (ManagerType): The collector's manager type.

    Returns:
        str: The deterministic manager id.
    """
    return f"mgr_{manager_type.value.lower()}"


def _is_blade(model: str) -> bool:
    """
    Whether a Cisco model is a B-series blade rather than a rack unit.

    Args:
        model (str): The model string, e.g. `"UCS B200 M6"`.

    Returns:
        bool: True for a blade.
    """
    parts = model.split()
    return len(parts) > 1 and parts[1].startswith("B")


def _is_ilo4(model: str) -> bool:
    """
    Whether an HPE model carries an iLO 4 rather than an iLO 5/6.

    Gen9 is the iLO-4 generation, and OneView's own subresource calls are
    documented to fail against one (docs/notes/oneview-api.md), so this
    decides which HPE servers come back as identity-only records.

    Args:
        model (str): The model string, e.g. `"ProLiant DL380 Gen9"`.

    Returns:
        bool: True for a Gen9.
    """
    return model.endswith("Gen9")


def collector_for(vendor: str, model: str) -> ManagerType:
    """
    Which collector would really have found this server.

    Cisco is split the way the two Cisco collectors actually split it:
    a blade sits in a chassis inside a UCS domain, which is what UCS
    Central registers and collects; a rack unit is the shape claimed
    directly into Intersight. Nothing is collected by both, mirroring the
    `ManagementMode` partition the Intersight collector enforces.

    HPE goes to OneView and Dell to OpenManage, the aggregators that
    actually own that hardware in this estate. Only `standalone` — a
    manufacturer this platform does not model — is left on its own BMC.

    Args:
        vendor (str): A `Vendor` value.
        model (str): The server's model.

    Returns:
        ManagerType: The owning collector.
    """
    if vendor == "hp":
        return ManagerType.ONEVIEW
    if vendor == "dell":
        return ManagerType.OPENMANAGE
    if vendor != "cisco":
        return ManagerType.REDFISH_STANDALONE
    if _is_blade(model):
        return ManagerType.UCS_CENTRAL
    return ManagerType.INTERSIGHT


def provider_type_for(server: ProviderServer) -> str:
    """
    Which collector owns an already-generated server.

    Read back off `external_id` rather than recomputed from the server's
    fields, because `external_id` is the one thing a real collector
    stamps with its own identity — so this cannot drift from what
    `collector_for` decided when the server was built.

    Dell is the one case `external_id` alone cannot settle, and it is not
    a shortcut: OpenManage reads a Dell's hardware from its own iDRAC over
    Redfish (ADR-0020), so an OME-collected Dell carries the *same*
    `redfish://` identifier a standalone BMC would. The vendor is what
    separates them, exactly as it does in `collector_for`.

    Args:
        server (ProviderServer): A generated server.

    Returns:
        str: The owning collector's `ManagerType` value.
    """
    if server.external_id.startswith("intersight/"):
        return ManagerType.INTERSIGHT.value
    if server.external_id.startswith("compute/sys-"):
        return ManagerType.UCS_CENTRAL.value
    if server.external_id.startswith("/rest/server-hardware/"):
        return ManagerType.ONEVIEW.value
    if server.vendor == "dell":
        return ManagerType.OPENMANAGE.value
    return ManagerType.REDFISH_STANDALONE.value


def list_sites(sites: SiteCatalog | None = None) -> list[Site]:
    """
    The sites servers are distributed across.

    Args:
        sites (SiteCatalog | None): The configured sites, or None for the
            shipped default.

    Returns:
        list[Site]: One `Site` per configured site, named as the API
            names it.
    """
    catalog = sites if sites is not None else _DEFAULT_SITES
    return [
        Site(
            id=_site_id(definition.code),
            name=definition.name,
            code=definition.code.upper(),
            audit=AuditFields.new(),
        )
        for definition in catalog.definitions
    ]


def list_managers() -> list[Manager]:
    """
    The managers the seeded fleet is collected through.

    One document per implemented collector, mirroring the projection
    `tools.run_collector.manager_for` writes on every real run. There is
    no UCS Central -> UCS Manager pair here any more: `--manager-type
    UCS_MANAGER` was removed, and Central is the single Cisco entry point.

    Returns:
        list[Manager]: One `Manager` per entry in `COLLECTOR_TYPES`.
    """
    return [
        Manager(
            id=manager_id_for(manager_type),
            name=manager_type.value.lower().replace("_", "-"),
            type=manager_type,
            endpoint=None,
            enabled=True,
            audit=AuditFields.new(),
        )
        for manager_type in COLLECTOR_TYPES
    ]


# --- Vendor-specific catalogs -------------------------------------------

# `standalone` is a real vendor, not a fallback: a Lenovo/Supermicro/
# whitebox machine reached at its own BMC, which is what the Redfish
# collector maps any manufacturer this platform does not model onto.
_VENDORS = ("dell", "cisco", "hp", "standalone")

_MODELS: dict[str, tuple[str, ...]] = {
    "dell": ("PowerEdge R650", "PowerEdge R750", "PowerEdge R6515"),
    "cisco": ("UCS C220 M6", "UCS C240 M6", "UCS B200 M6"),
    # Three ProLiant generations on purpose: Gen11/Gen10 carry an iLO 5
    # or 6 and inventory fully, Gen9 carries an iLO 4 and comes back as
    # an identity-only record — see `_is_ilo4`.
    "hp": (
        "ProLiant DL380 Gen11",
        "ProLiant DL360 Gen11",
        "ProLiant DL325 Gen11",
        "ProLiant DL380 Gen10",
        "ProLiant DL360 Gen10",
        "ProLiant DL380 Gen9",
        "ProLiant DL360 Gen9",
    ),
    "standalone": ("ThinkSystem SR650 V3", "SYS-221H-TNR", "AS-2125HS-TNR"),
}

_CPU_MODELS: dict[str, tuple[str, ...]] = {
    "dell": ("Intel Xeon Gold 6338", "Intel Xeon Platinum 8358"),
    "cisco": ("Intel Xeon Gold 6348", "Intel Xeon Silver 4314"),
    "hp": ("AMD EPYC 7513", "AMD EPYC 9354"),
    "standalone": ("Intel Xeon Gold 6438Y+", "AMD EPYC 9454"),
}

# UPI node roles, in the real cluster vocabulary.
_NAME_ROLES = ("compute", "control-plane", "infra")

# A Dell hostname's hardware token and the PCIe slot its add-in NIC sits
# in. The token is what an operator reads ("this is a GPU node"); the slot
# is what decides the interface's OS-level name once the host boots, since
# systemd derives `ens<slot>f<function>np<port>` from PCI topology. Both
# halves are here so a seeded fleet exercises the join between them.
#
# `""` is the default profile: onboard NICs only, `NIC.Integrated.1-*`,
# which boots as `eno...` rather than `ens...`.
_NIC_PROFILE_SLOTS: tuple[tuple[str, int], ...] = (
    ("h100", 8),
    ("h200", 33),
    ("10tb-", 2),
    ("5tb-", 2),
)
_NAME_HARDWARE_PROFILES = ("", "", "h100", "h200", "10tb", "5tb")

# Dell's PowerEdge onboard NIC is a two-port LOM, so a server with no
# add-in card has exactly two interfaces and one with a card has four —
# which is what makes "the third and fourth MACs" name the card's ports.
_ONBOARD_PORTS = 2
_SLOT_PORTS = 2

# The collectors that read a BMC over Redfish and so report FQDD-shaped
# interface names. Dell is in both senses one of them: OpenManage takes a
# Dell's identity from the appliance but its hardware from the server's
# own iDRAC (ADR-0020), reusing the Redfish mapping unchanged.
_FQDD_COLLECTORS = (ManagerType.OPENMANAGE, ManagerType.REDFISH_STANDALONE)
_FABRIC_COLLECTORS = (ManagerType.UCS_CENTRAL, ManagerType.INTERSIGHT)

# Environment segment some UPI hostnames carry (`ocp4-prod-tlv-infra-01`),
# and some don't (`ocp4-nyc-control-plane-02`). Both real shapes.
_NAME_ENVIRONMENTS = ("prod", "prep", None)

# (pattern-family, weight) — weighted so "unclassified-shaped" names stay
# a small minority, matching a realistic mixed estate. This family
# deliberately carries no site token, exercising the UI's "Unassigned
# site" state — it does NOT exercise "Unclassified" any more, since UPI's
# system-default pattern became unconditional 2026-09-10; every one of
# these names classifies UPI too, same as everything else that isn't
# hosted-cluster or MCE. Kept under its original name rather than
# renamed, since the shape (siteless, non-`ocp`-prefixed) is still what
# it is generating — see `docs/architecture.md`'s classification section.
#
# UPI outweighs hosted-cluster rather than tying it, so the sites
# overview's fleet cards read as different numbers rather than several
# landing on the same count, which looks like a bug in the page rather
# than a property of the fleet. MCE stays a single-weight minority for
# the same reason unclassified is — it is a real but small slice of a
# UPI-installed estate, not a mainstream shape.
_NAME_FAMILIES = (
    "hosted_cluster",
    "hosted_cluster",
    "hosted_cluster_hw",
    "mce",
    "upi",
    "upi",
    "upi",
    "upi",
    "unclassified",
)

_DRIVE_MEDIA = ("NVME", "SSD", "SSD", "HDD")

# Both collectors normalize component health onto `HealthSeverity` at the
# provider boundary (`redfish.mapping.health_of`, `ucs_manager.mapping.
# _disk_health`), so fixtures must speak that vocabulary and not an
# invented OK/DEGRADED/FAILED one — the seeded storage policy counts
# CRITICAL drives.
_COMPONENT_HEALTHS = ("HEALTHY", "HEALTHY", "HEALTHY", "HEALTHY", "WARNING", "CRITICAL")

# `health_detail`'s stand-in per reduced tier — Redfish's own real
# `Status.Health` strings, cosmetic only, never read by anything.
_HEALTH_DETAIL_SAMPLES = {"HEALTHY": "OK", "WARNING": "Warning", "CRITICAL": "Critical"}

# Same idea for a PSU's separate UP/DOWN/DISABLED/UNKNOWN vocabulary.
_PSU_HEALTH_DETAIL_SAMPLES = {
    "UP": "operable",
    "DOWN": "inoperable",
    "DISABLED": "admin-down",
    "UNKNOWN": "unknown",
}

_DRIVE_CAPACITIES_BYTES: dict[str, tuple[int, ...]] = {
    "NVME": (960_000_000_000, 1_920_000_000_000, 3_840_000_000_000),
    "SSD": (480_000_000_000, 960_000_000_000, 1_920_000_000_000),
    "HDD": (2_000_000_000_000, 4_000_000_000_000, 8_000_000_000_000),
}

# The GPU identifiers each management plane really answers with, in its
# own spelling. Cisco reports its part number; a BMC reports the vendor's
# model string and no PID at all. `GpuCatalog` matches both, normalized —
# so the fleet has to carry both or the local view only ever proves one
# half of the catalog works.
#
# (vendor, Cisco PID)
_CISCO_GPU_PIDS: tuple[tuple[str, str], ...] = (
    ("NVIDIA", "UCSC-GPU-L40S"),
    ("NVIDIA", "UCSC-GPU-A100-80"),
    ("NVIDIA", "UCSC-GPU-H100-80"),
    ("NVIDIA", "UCSC-GPU-T4-16"),
    ("NVIDIA", "UCSX-GPU-A16"),
)

# (vendor, model string, memory type) — what a Redfish `Processor` with
# `ProcessorType == "GPU"` reports on an iDRAC or an iLO.
_BMC_GPU_MODELS: tuple[tuple[str, str, str], ...] = (
    ("NVIDIA", "NVIDIA A100-PCIE-40GB", "HBM2e"),
    ("NVIDIA", "NVIDIA H100 80GB HBM3", "HBM3"),
    ("NVIDIA", "NVIDIA L40S", "GDDR6"),
    ("NVIDIA", "Tesla T4", "GDDR6"),
    ("AMD", "AMD Instinct MI300X", "HBM3"),
)

# Cards the built-in catalog deliberately does not answer for, so the
# "VRAM unknown" path is visible in dev instead of being discovered in
# production. `UCSC-GPU-A100-40` is a PID the table has never been
# taught; a bare `NVIDIA A100` is genuinely ambiguous (the A100 shipped
# in 40GB and 80GB) and the catalog refuses it rather than guessing; the
# RTX A6000 is simply absent. An operator closes all three with
# `INVENTORY_GPU_MODELS`.
_UNCATALOGED_CISCO_GPU_PIDS: tuple[tuple[str, str], ...] = (("NVIDIA", "UCSC-GPU-A100-40"),)
_UNCATALOGED_BMC_GPU_MODELS: tuple[tuple[str, str, str], ...] = (
    ("NVIDIA", "NVIDIA A100", "HBM2e"),
    ("NVIDIA", "NVIDIA RTX A6000", "GDDR6"),
)
_UNCATALOGED_GPU_SHARE = 0.2

_GPU_COUNTS = (0, 0, 0, 0, 0, 0, 2, 4, 8)  # most servers have none

_ATTACHMENT_COUNTS = (0, 1, 2, 2, 2, 4)  # weighted toward the common 2-up case
_OPER_STATE_PATTERNS: dict[int, tuple[tuple[str, ...], ...]] = {
    1: (("UP",), ("DOWN",)),
    2: (("UP", "UP"), ("UP", "DOWN"), ("DOWN", "DOWN")),
    4: (("UP", "UP", "UP", "UP"), ("UP", "UP", "UP", "DOWN"), ("UP", "DOWN", "UP", "DOWN")),
}

# The seeded minority whose links really are down, so the two link
# policies have something to fire on now that a Cisco server's UNKNOWN
# vNICs no longer trip them by accident (ADR-0027's "Seeded data").
_ALL_LINKS_DOWN_PER_MILLE = 30
_SINGLE_LINK_UP_PER_MILLE = 40

# vNICs UCS carves out of each physical port (adaptorHostEthIf per
# adaptorExtEthIf). Two is the common OCP bond.
_VNICS_PER_PORT = 2


def _random_mac(rng: random.Random) -> str:
    """
    A syntactically valid random MAC.

    Args:
        rng (random.Random): The seeded generator.

    Returns:
        str: A colon-separated MAC.
    """
    value = rng.getrandbits(48)
    if value in (0, (1 << 48) - 1):  # avoid the reserved all-zero/broadcast forms
        value ^= 1
    hexed = f"{value:012x}"
    return ":".join(hexed[i : i + 2] for i in range(0, 12, 2))


def _random_uuid(rng: random.Random) -> str:
    """
    A random system UUID.

    Args:
        rng (random.Random): The seeded generator.

    Returns:
        str: The UUID in canonical string form.
    """
    return str(uuid.UUID(int=rng.getrandbits(128), version=4))


def _bmc_address(collector: ManagerType, ip: str) -> str:
    """
    The BMC address the owning collector would report.

    Args:
        collector (ManagerType): The collector that owns the server.
        ip (str): Its BMC address.

    Returns:
        str: `ipmi://<ip>:623` for either Cisco collector — they read the
            same CIMC, and `intersight.mapping.bmc_address` deliberately
            emits the same form `ucs_manager.mapping` does — and the
            `redfish://` form otherwise. OneView shares that form: what
            it reports is the iLO's own address
            (`mpHostInfo.mpIpAddresses[]`), which is a Redfish endpoint.
    """
    if collector in (ManagerType.UCS_CENTRAL, ManagerType.INTERSIGHT):
        return f"ipmi://{ip}:623"
    return f"redfish://{ip}/redfish/v1/Systems/1"


def _external_id(collector: ManagerType, *, index: int, site_index: int, bmc_ip: str) -> str:
    """
    The collector-native identifier for this server.

    Args:
        collector (ManagerType): The collector that owns it.
        index (int): Its index in the generated fleet.
        site_index (int): Its site's position in `_SITE_CODES`, standing in
            for a registered UCS domain.
        bmc_ip (str): Its BMC address.

    Returns:
        str: A domain-qualified UCS DN (`ucs_central.provider.
            central_external_id`'s shape), an `intersight/<moid>` id
            (`intersight.mapping.external_id`'s shape, whose Moid is 24
            hex characters), a OneView `server-hardware` URI (the
            canonical `uri` field, docs/notes/oneview-api.md §3), or the
            `redfish://` system URL.
    """
    if collector is ManagerType.UCS_CENTRAL:
        domain_id = str(1000 + site_index)
        return f"compute/sys-{domain_id}/chassis-{index // 8 + 1}/blade-{index % 8 + 1}"
    if collector is ManagerType.INTERSIGHT:
        return f"intersight/{index:024x}"
    if collector is ManagerType.ONEVIEW:
        return f"/rest/server-hardware/{index:08x}-0000-4000-8000-{index:012x}"
    return f"redfish://{bmc_ip}/redfish/v1/Systems/1"


def _fake_ip(site_index: int, host_index: int) -> str:
    """
    A stable BMC IP for one server.

    Args:
        site_index (int): Its site's position in `_SITE_CODES`.
        host_index (int): Its index in the generated fleet.

    Returns:
        str: A dotted-quad address in a per-site /16.
    """
    return f"10.{site_index + 10}.{host_index // 256}.{host_index % 256}"


# UCS Manager Service Profile Templates. Only Cisco gets one: the Redfish
# collector reads a BMC, which knows nothing about profiles, and the OME/
# OneView collectors that would report their own templates do not exist.
# Every collector whose real mapping populates `profile_template_name`/
# `_external_id` (see docs/architecture.md's "Server.profile_template"
# entry) gets a plausible set here. A template's name is an operator's
# own choice, not a vendor-fixed vocabulary, so these are illustrative,
# not researched facts the way a vendor enum value would have to be.
# REDFISH_STANDALONE is deliberately absent: a bare BMC has no template
# concept, and `OverviewTab`'s `PROFILE_TEMPLATE_LABELS` omits its row
# entirely for exactly that reason — this fixture must not paper over it.
_TEMPLATE_NAMES: dict[ManagerType, tuple[str, ...]] = {
    ManagerType.UCS_CENTRAL: ("SPT-OCP-Worker-B200", "SPT-OCP-Master-C240", "SPT-UPI-Generic"),
    ManagerType.INTERSIGHT: ("IMM-Worker-Template", "IMM-Master-Template", "UCS-X-Standard"),
    ManagerType.ONEVIEW: (
        "OneView-Gen10-Worker",
        "OneView-Gen11-Master",
        "OneView-Standard-Compute",
    ),
    ManagerType.OPENMANAGE: ("OME-Deploy-Worker", "OME-Deploy-Master", "OME-Standard-Template"),
}


def _profile_template(rng: random.Random, collector: ManagerType) -> tuple[str | None, str | None]:
    """
    The deployment/profile template this server was provisioned from.

    ~10% of servers from a collector that has this concept get none — a
    profile applied ad hoc rather than from a template is a real, common
    state, same as in production.

    Args:
        rng (random.Random): The seeded generator.
        collector (ManagerType): The collector this server came from.

    Returns:
        tuple[str | None, str | None]: `(name, external_id)` — every
            supported collector references its template by name here, so
            both are the same string; `None, None` for a collector with
            no template concept at all (`REDFISH_STANDALONE`).
    """
    names = _TEMPLATE_NAMES.get(collector)
    if names is None or rng.random() < 0.1:
        return None, None
    name = rng.choice(names)
    return name, name


def _profile_dn(collector: ManagerType, *, name: str, site_code: str) -> str | None:
    """
    The service profile's own DN, which doubles as its org path.

    The org segment is what `app.domain.value_objects.site.parse_site_code`
    falls back to when a server's *name* carries no site token, so the
    siteless name family still resolves to a site on UCS — exactly as it
    does in production.

    Only `UCS_CENTRAL` reports one. An Intersight `server.Profile` has no
    `Dn` field at all, so its servers get `None` and a siteless name
    there really does resolve to no site — a real gap this fixture is
    meant to show rather than paper over.

    Args:
        collector (ManagerType): The collector that owns the server.
        name (str): The server's name, which is its profile's name.
        site_code (str): The site whose org holds the profile.

    Returns:
        str | None: The DN, or `None` for a collector that reports none.
    """
    if collector is not ManagerType.UCS_CENTRAL:
        return None
    return f"org-root/org-{site_code}/ls-{name}"


def _build_name(
    rng: random.Random,
    vendor: str,
    index: int,
    site_code: str,
    model: str,
    memory_gib: int,
    collector: ManagerType,
) -> str:
    """
    A hostname in the shapes this estate actually uses.

    Every shape but the deliberate `unclassified` minority embeds
    `site_code` as a whole `-`-delimited token, because the name is what
    `app.domain.value_objects.site.parse_site_code` reads the site back
    out of, and what the seeded classification rules key on to decide
    HOSTED_CLUSTER vs MCE vs UPI.

    Args:
        rng (random.Random): The seeded generator.
        vendor (str): The server's vendor.
        index (int): Its index in the generated fleet.
        site_code (str): The site token to embed.
        model (str): Its model, one hostname shape embeds a short form.
        memory_gib (int): Its memory, likewise embedded in that shape.
        collector (ManagerType): The collector that owns it. Only a
            Redfish-sourced server takes a hardware-profile token, since
            that token names a PCIe slot and only an FQDD-shaped interface
            can agree with it.

    Returns:
        str: The hostname.
    """
    family = rng.choice(_NAME_FAMILIES)

    if family == "hosted_cluster":
        # ocp4-hypershift-tlv-01 / ocp4-hypershift-data-tlv-02
        segment = "hypershift-data" if rng.random() < 0.35 else "hypershift"
        return f"ocp4-{segment}-{site_code}-{index % 100:02d}"

    if family == "hosted_cluster_hw":
        # ocp-dell-r650-tlv-128c-1024gb-<serial>
        short_model = model.split()[-1].lower()
        cores = rng.choice((64, 128, 192))
        profile = rng.choice(_NAME_HARDWARE_PROFILES) if collector in _FQDD_COLLECTORS else ""
        hardware = f"{profile}-" if profile else ""
        return (
            f"ocp-{vendor}-{short_model}-{site_code}-{cores}c-"
            f"{memory_gib}gb-{hardware}{vendor[:3].upper()}{index:07d}"
        )

    if family == "mce":
        # ocp4-mce-tlv-01 — an MCE hub's own nodes. Matched by name alone
        # (the "mce" substring), same as every other family here, so it
        # deliberately also matches the UPI catch-all below; the
        # classification rules' own ordering is what keeps it out of UPI.
        return f"ocp4-mce-{site_code}-{index % 100:02d}"

    if family == "upi":
        # ocp4-five-compute-01 / ocp4-nyc-control-plane-02 /
        # ocp4-prod-tlv-infra-01
        role = rng.choice(_NAME_ROLES)
        environment = rng.choice(_NAME_ENVIRONMENTS)
        prefix = f"ocp4-{environment}" if environment else "ocp4"
        return f"{prefix}-{site_code}-{role}-{index % 100:02d}"

    # No site token and no classifiable shape, on purpose.
    return f"random-server-{index:04d}"


def _build_storage_drives(
    rng: random.Random, count: int
) -> tuple[tuple[dict[str, object], ...], int]:
    """
    A server's drives, keyed exactly as both collectors emit them.

    Args:
        rng (random.Random): The seeded generator.
        count (int): How many drives to build.

    Returns:
        tuple[tuple[dict[str, object], ...], int]: The drives and their
            total capacity in bytes.
    """
    drives: list[dict[str, object]] = []
    total = 0
    for i in range(count):
        media = rng.choice(_DRIVE_MEDIA)
        capacity = rng.choice(_DRIVE_CAPACITIES_BYTES[media])
        health = rng.choice(_COMPONENT_HEALTHS)
        total += capacity
        drives.append(
            {
                "id": f"disk{i}",
                "model": f"{media}-{capacity // 1_000_000_000}G",
                "serial": f"drv{rng.getrandbits(32):08x}",
                "media_type": media,
                "capacity_bytes": capacity,
                "health": health,
                "health_detail": _HEALTH_DETAIL_SAMPLES[health],
            }
        )
    return tuple(drives), total


def _gpu_identity(rng: random.Random, collector: ManagerType) -> tuple[str, str, str | None]:
    """
    The vendor, identifier and memory type one server's GPUs report.

    Which *kind* of identifier depends on the management plane, not the
    card: Cisco answers with its own PID, a BMC with the vendor's model
    string. A fifth of servers draw a card the built-in catalog cannot
    answer for, so "VRAM unknown" is reachable locally.

    Args:
        rng (random.Random): The seeded generator.
        collector (ManagerType): The collector that owns the server.

    Returns:
        tuple[str, str, str | None]: `(vendor, model, memory_type)`;
            `memory_type` is `None` for Cisco, which reports no such
            field.
    """
    uncataloged = rng.random() < _UNCATALOGED_GPU_SHARE
    if collector in (ManagerType.UCS_CENTRAL, ManagerType.INTERSIGHT):
        gpu_vendor, model = rng.choice(
            _UNCATALOGED_CISCO_GPU_PIDS if uncataloged else _CISCO_GPU_PIDS
        )
        return gpu_vendor, model, None
    return rng.choice(_UNCATALOGED_BMC_GPU_MODELS if uncataloged else _BMC_GPU_MODELS)


def _build_psus(rng: random.Random) -> tuple[dict[str, object], ...]:
    """
    A server's fitted power supplies, keyed as every real collector emits them.

    Every collector reports PSUs now, so this is never `()`; health is
    UP/DOWN/DISABLED/UNKNOWN, the vocabulary `power.failed_psu_count` reads.

    Args:
        rng (random.Random): The seeded generator.

    Returns:
        tuple[dict[str, object], ...]: Two supplies, occasionally one of
            them DOWN so `power.failed_psu_count` has something to read
            locally.
    """
    capacity = rng.choice((800, 1200, 1600, 2400))
    failed_bay = rng.choice((1, 2)) if rng.random() < 0.06 else None
    psus: list[dict[str, object]] = []
    for bay in (1, 2):
        health = "DOWN" if bay == failed_bay else "UP"
        psus.append(
            {
                "id": f"PSU{bay}",
                "model": f"{capacity}W Platinum",
                "serial": f"PSU{rng.randint(1_000_000, 9_999_999)}",
                "health": health,
                "health_detail": _PSU_HEALTH_DETAIL_SAMPLES[health],
                "capacity_watts": capacity,
            }
        )
    return tuple(psus)


def _build_gpus(rng: random.Random, collector: ManagerType) -> tuple[dict[str, object], ...] | None:
    """
    A server's GPUs, keyed as `redfish.mapping.gpus_from_processors` emits them.

    `memory_bytes` is always `None`, matching every real collector — the
    UI's VRAM figure comes from `GpuCatalog` at ingest, not from here.

    Args:
        rng (random.Random): The seeded generator.
        collector (ManagerType): The collector that owns the server.

    Returns:
        tuple[dict[str, object], ...] | None: The GPUs — empty for most
            servers, which is "none discoverable" rather than "none
            installed".
    """
    count = rng.choice(_GPU_COUNTS)
    gpu_vendor, model, memory_type = _gpu_identity(rng, collector)
    gpus: list[dict[str, object]]
    if collector is ManagerType.UCS_CENTRAL:
        gpus = []
        for _ in range(count):
            health = rng.choice(_COMPONENT_HEALTHS)
            gpus.append(
                {
                    "vendor": gpu_vendor,
                    "model": model,
                    "serial": f"GPU{rng.getrandbits(32):08x}",
                    "memory_bytes": None,
                    "health": health,
                    "health_detail": _HEALTH_DETAIL_SAMPLES[health],
                    "pci_address": f"0000:{rng.randint(0x10, 0xBF):02x}:00.0",
                    "firmware_version": f"{rng.randint(535, 560)}.{rng.randint(0, 99):02d}.01",
                    "memory_type": None,
                    "ecc_mode_enabled": None,
                    "correctable_error_count": None,
                    "uncorrectable_error_count": None,
                    # The one real telemetry field UCS has, read off the
                    # card's own temperature stats MO.
                    "temperature_celsius": float(rng.randint(38, 82)),
                    "power_watts": None,
                }
            )
        return tuple(gpus)
    if collector is ManagerType.INTERSIGHT:
        gpus = []
        for _ in range(count):
            health = rng.choice(_COMPONENT_HEALTHS)
            gpus.append(
                {
                    "vendor": gpu_vendor,
                    "model": model,
                    "serial": f"GPU{rng.getrandbits(32):08x}",
                    "memory_bytes": None,
                    "health": health,
                    "health_detail": _HEALTH_DETAIL_SAMPLES[health],
                    "pci_address": None,
                    "firmware_version": None,
                    "memory_type": None,
                    "ecc_mode_enabled": None,
                    "correctable_error_count": None,
                    "uncorrectable_error_count": None,
                    "temperature_celsius": None,
                    "power_watts": None,
                }
            )
        return tuple(gpus)
    gpus = []
    for _ in range(count):
        health = rng.choice(_COMPONENT_HEALTHS)
        gpus.append(
            {
                "vendor": gpu_vendor,
                "model": model,
                "serial": f"GPU{rng.getrandbits(32):08x}",
                "memory_bytes": None,
                "health": health,
                "health_detail": _HEALTH_DETAIL_SAMPLES[health],
                # Redfish has no PCI address on a `Processor`; the
                # collector reports None rather than inventing one.
                "pci_address": None,
                "firmware_version": f"{rng.randint(535, 560)}.{rng.randint(0, 99):02d}.01",
                "memory_type": memory_type,
                "ecc_mode_enabled": True,
                "correctable_error_count": rng.choice((0, 0, 0, 1, 17)),
                "uncorrectable_error_count": rng.choice((0, 0, 0, 0, 1)),
                "temperature_celsius": float(rng.randint(38, 82)),
                "power_watts": float(rng.randint(90, 700)),
            }
        )
    return tuple(gpus)


def _build_attachments(rng: random.Random, *, site_code: str) -> tuple[ProviderAttachment, ...]:
    """
    A Cisco server's fabric attachments.

    The UCS collector reports both the physical uplinks (`adaptorExtEthIf`)
    and the vNICs carved out of them (`adaptorHostEthIf`), telling them
    apart with `interface_kind` — so the fixture does too, or nothing in
    dev ever sees the two kinds together.

    Args:
        rng (random.Random): The seeded generator.
        site_code (str): The site, which names the fabric interconnects.

    Returns:
        tuple[ProviderAttachment, ...]: The physical ports followed by
            their vNICs.
    """
    count = rng.choice(_ATTACHMENT_COUNTS)
    if count == 0:
        return ()

    oper_states = rng.choice(_OPER_STATE_PATTERNS[count])

    physical: list[ProviderAttachment] = []
    for i, oper_state in enumerate(oper_states):
        fabric = "A" if i % 2 == 0 else "B"
        fabric_name = f"FI-{fabric}-{site_code}-01"
        physical.append(
            ProviderAttachment(
                type="FABRIC_INTERCONNECT",
                provider=ManagerType.UCS_CENTRAL.value,
                fabric=fabric,
                fabric_name=fabric_name,
                fabric_id=fabric_name,
                fabric_model="UCS-FI-6454",
                fabric_serial=f"FI{rng.getrandbits(24):06x}",
                server_interface=f"eth{i}",
                server_port=f"1/{i + 1}",
                fabric_port=f"1/{i + 1}",
                admin_state="ENABLED",
                oper_state=oper_state,
                speed_mbps=rng.choice((10_000, 25_000, 40_000)),
                interface_kind="PHYSICAL",
            )
        )

    vnics = [
        replace(
            port,
            server_interface=f"vnic{i}-{n}",
            interface_kind="VNIC",
        )
        for i, port in enumerate(physical)
        for n in range(_VNICS_PER_PORT)
    ]
    return tuple(physical) + tuple(vnics)


def nic_slot_for(name: str) -> int | None:
    """
    Which PCIe slot a server's add-in NIC sits in, read from its name.

    The hostname token is the only thing that says so — no management API
    reports an OS-level interface name, and the slot is what one is
    derived from. Public because it is the same lookup anything generating
    per-server network configuration needs.

    Args:
        name (str): The server's hostname.

    Returns:
        int | None: The slot number, or None for a server with onboard
            NICs only.
    """
    lowered = name.lower()
    for token, slot in _NIC_PROFILE_SLOTS:
        if token in lowered:
            return slot
    return None


def _nic_port_count(rng: random.Random, collector: ManagerType, *, slot: int | None) -> int:
    """
    How many interfaces one server has.

    Args:
        rng (random.Random): The seeded generator.
        collector (ManagerType): The collector that owns this server.
        slot (int | None): Its add-in NIC's slot, or None for onboard only.

    Returns:
        int: The port count.
    """
    if collector in _FQDD_COLLECTORS:
        return _ONBOARD_PORTS + (_SLOT_PORTS if slot is not None else 0)
    return rng.randint(2, 4)


def _link_states(macs: tuple[str, ...]) -> tuple[str, ...]:
    """
    Real UP/DOWN readings for one server's ports, faults included.

    Keyed off the MAC, not `rng`, and never UNKNOWN — ADR-0027's "Seeded
    data" says why both.

    Args:
        macs (tuple[str, ...]): The server's MACs, one per port.

    Returns:
        tuple[str, ...]: One link state per port, in order.
    """
    ports = len(macs)
    draw = zlib.crc32(macs[0].encode()) % 1000 if macs else 999
    if draw < _ALL_LINKS_DOWN_PER_MILLE:
        return ("DOWN",) * ports
    if draw < _ALL_LINKS_DOWN_PER_MILLE + _SINGLE_LINK_UP_PER_MILLE and ports >= 2:
        return ("UP",) + ("DOWN",) * (ports - 1)
    return ("UP",) * ports


def _nics_for(
    rng: random.Random, collector: ManagerType, macs: tuple[str, ...], *, slot: int | None
) -> tuple[ProviderNic, ...]:
    """
    Build the per-interface view for one server, in its collector's shape.

    Every collector populates this: UCS Central/Intersight report a
    vNIC's own name (`eth0`), Redfish an FQDD — see
    `..ucs_manager.mapping._nics`/`..intersight.mapping._nics`.

    Args:
        rng (random.Random): The seeded generator.
        collector (ManagerType): The collector that owns this server.
        macs (tuple[str, ...]): The server's MACs, one per interface.
        slot (int | None): Its add-in NIC's slot, or None for onboard only.

    Returns:
        tuple[ProviderNic, ...]: One entry per physical port.
    """
    if collector in (ManagerType.UCS_CENTRAL, ManagerType.INTERSIGHT):
        # No FQDD, no location, no speed — matching real UCS/Intersight
        # mapping. UNKNOWN link_state matches ADR-0009's 99.75% figure.
        return tuple(
            ProviderNic(name=f"eth{i}", mac=mac, speed_mbps=None, link_state="UNKNOWN")
            for i, mac in enumerate(macs)
        )
    states = _link_states(macs)
    if collector is ManagerType.ONEVIEW:
        # HPE names a port by its adapter and port number, not an FQDD.
        return tuple(
            ProviderNic(
                name=f"Physical Port {port}",
                mac=mac,
                speed_mbps=rng.choice((10_000, 25_000)),
                link_state=state,
                location=f"1:{port}",
            )
            for port, (mac, state) in enumerate(zip(macs, states, strict=True), start=1)
        )
    # Onboard first, then the add-in card, because that is the order a
    # BMC enumerates them and therefore the order the MACs come back in.
    # It is what makes "the third and fourth MACs" name the card's ports
    # on a four-interface server: onboard contributes the first two.
    nics: list[ProviderNic] = []
    for port, mac in enumerate(macs[:_ONBOARD_PORTS], start=1):
        nics.append(
            ProviderNic(
                name=f"NIC.Integrated.1-{port}-1",
                mac=mac,
                speed_mbps=rng.choice((10_000, 25_000)),
                link_state=states[port - 1],
                location=f"1/{port}/1",
            )
        )
    if slot is not None:
        for port, mac in enumerate(macs[_ONBOARD_PORTS:], start=1):
            nics.append(
                ProviderNic(
                    name=f"NIC.Slot.{slot}-{port}-1",
                    mac=mac,
                    speed_mbps=rng.choice((25_000, 100_000)),
                    link_state=states[_ONBOARD_PORTS + port - 1],
                    location=f"{slot}/{port}/1",
                )
            )
    return tuple(nics)


def generate_servers(
    *, seed: int, count: int, sites: SiteCatalog | None = None
) -> Iterator[ProviderServer]:
    """
    Yield `count` deterministic `ProviderServer` DTOs for the given `seed`.

    Every random decision is drawn from one `random.Random(seed)` in a
    fixed order, so this is fully reproducible: same `(seed, count)` ->
    field-for-field identical output, every run.

    Args:
        seed (int): The RNG seed.
        count (int): How many servers to generate.
        sites (SiteCatalog | None): The sites whose codes appear in the
            generated hostnames, or None for the shipped default.

    Yields:
        ProviderServer: One fake server.
    """
    rng = random.Random(seed)  # noqa: S311 - deterministic fake data, never security-sensitive
    site_codes = (sites if sites is not None else _DEFAULT_SITES).codes

    for index in range(count):
        site_index = index % len(site_codes)
        site_code = site_codes[site_index]
        # Stepped by site cycle, not by `index`: there are as many vendors
        # as sites, so a plain `index % len(_VENDORS)` locks each site to
        # exactly one vendor and leaves every per-site vendor breakdown a
        # single bar.
        vendor = _VENDORS[(index // len(site_codes)) % len(_VENDORS)]
        # Shuffle vendor pick slightly so it isn't perfectly periodic —
        # still fully deterministic (drawn from `rng`), just less uniform.
        if rng.random() < 0.15:
            vendor = rng.choice(_VENDORS)

        # `model` and `memory_gib` are drawn before `name` because one of
        # the real hostname shapes embeds both
        # (`ocp-dell-r650-tlv-128c-1024gb-<serial>`).
        model = rng.choice(_MODELS[vendor])
        collector = collector_for(vendor, model)
        memory_gib = rng.choice((128, 256, 512, 1024))
        memory_total_bytes = memory_gib * 1024**3

        name = _build_name(rng, vendor, index, site_code, model, memory_gib, collector)
        serial = f"{vendor[:3].upper()}{index:07d}"
        system_uuid = _random_uuid(rng)

        nic_slot = nic_slot_for(name)
        nic_count = _nic_port_count(rng, collector, slot=nic_slot)
        nic_macs = tuple(_random_mac(rng) for _ in range(nic_count))
        nics = _nics_for(rng, collector, nic_macs, slot=nic_slot)
        bmc_mac = _random_mac(rng)
        bmc_ip = _fake_ip(site_index, index)

        cpu_sockets = rng.choice((1, 2))
        cpu_cores = rng.choice((16, 24, 32, 48, 64))
        cpu_threads = cpu_cores * 2
        cpu_model = rng.choice(_CPU_MODELS[vendor])

        drive_count = rng.randint(2, 8)
        storage_drives, storage_total_bytes = _build_storage_drives(rng, drive_count)
        gpus = _build_gpus(rng, collector)
        psus = _build_psus(rng)

        # A small fraction of Dell servers exercise `reachable=False`, the
        # one shape `OpenManageProvider._unreachable_server` can produce.
        reachable = not (collector is ManagerType.OPENMANAGE and rng.random() < 0.03)
        if not reachable:
            system_uuid = None
            nic_macs = None
            nics = ()
            bmc_mac = None
            cpu_sockets = cpu_cores = cpu_threads = cpu_model = None
            memory_total_bytes = None
            storage_drives = storage_total_bytes = None
            gpus = psus = None

        # A Gen9's iLO 4 is the one partial record in this fleet: every
        # OneView subresource call against one fails, so its detailed
        # hardware is `None` — unread — while its identity is intact.
        # Never `()` or `0`: an empty drive list reads as "no drives
        # installed" and takes a healthy server to CRITICAL, which is the
        # exact bug the `None`-means-unread contract exists to prevent.
        if collector is ManagerType.ONEVIEW and _is_ilo4(model):
            nic_macs = None
            # `()` rather than `None`: `nics` has no unread state — it is
            # the richer view of the interfaces `nic_macs` lists, so a
            # server whose MACs went unread cannot coherently report
            # per-port detail for them.
            nics = ()
            storage_drives = None
            storage_total_bytes = None
            gpus = None
            psus = None

        # Only the two fabric-interconnect collectors report attachments —
        # the same set the seeded `connectivity.fabric_paths_down`
        # policies are scoped to (docs/adr/0030).
        attachments = (
            _build_attachments(rng, site_code=site_code) if collector in _FABRIC_COLLECTORS else ()
        )
        template_name, template_external_id = _profile_template(rng, collector)

        yield ProviderServer(
            external_id=_external_id(collector, index=index, site_index=site_index, bmc_ip=bmc_ip),
            vendor=vendor,
            name=name,
            model=model,
            serial=serial,
            system_uuid=system_uuid,
            reachable=reachable,
            nic_macs=nic_macs,
            nics=nics,
            bmc_address_raw=_bmc_address(collector, bmc_ip),
            bmc_mac=bmc_mac,
            manager_id=manager_id_for(collector),
            profile_dn=_profile_dn(collector, name=name, site_code=site_code),
            profile_template_name=template_name,
            profile_template_external_id=template_external_id,
            cpu_sockets=cpu_sockets,
            cpu_cores=cpu_cores,
            cpu_threads=cpu_threads,
            cpu_model=cpu_model,
            memory_total_bytes=memory_total_bytes,
            storage_total_bytes=storage_total_bytes,
            storage_drives=storage_drives,
            gpus=gpus,
            psus=psus,
            attachments=attachments,
            # No collector reports tags: UCS's are per-org labels the
            # provider does not read, Intersight's are not mapped, and a
            # BMC has none at all.
            tags=(),
        )
