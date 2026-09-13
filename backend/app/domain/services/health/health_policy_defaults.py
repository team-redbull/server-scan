"""Seeded system-default health policies.

`default_system_policies()` returns the platform spec's Cisco UCS fabric
acceptance scenario (§71) plus one generic storage default, ready to be
upserted by whatever integration step owns app-startup seeding (not this
module's job — see the module-level callers' docstrings for why this file
only *builds* the policies rather than persisting them).

The two fabric policies use DIFFERENT `policy_key`s
(`connectivity.fabric_paths_down_warning` /
`connectivity.fabric_paths_down_critical`) with mutually exclusive
conditions (`EQ 1` and `GTE 2`), matching the already-tested pattern in
`tests/unit/domain/services/test_health_evaluate.py`'s `FABRIC_POLICIES`
fixture: same `policy_key` means "these compete for one winner", and the
two severities here are meant to coexist as independent, simultaneously
registerable candidates, never to shadow each other.
"""

from __future__ import annotations

from app.domain.enums import HealthSeverity, ManagerType, Vendor
from app.domain.models.health_policy import EvidenceField, HealthPolicy, PolicyScope
from app.domain.services.health.conditions import Condition
from app.utils.ids import new_id
from app.utils.timeutil import utcnow


def default_system_policies() -> list[HealthPolicy]:
    """
    Build the seeded system-default health policies, unsaved.

    Returns:
        list[HealthPolicy]: The two UCS fabric-path policies and the
            generic failed-drive policy, each a fresh `HealthPolicy` with
            a new id — building, not persisting, is this function's job.
    """
    now = utcnow()
    # Only a fabric interconnect has fabric paths — see docs/adr/0030.
    fabric_scope = PolicyScope(
        vendor=Vendor.CISCO.value,
        manager_types=[ManagerType.UCS_CENTRAL.value, ManagerType.INTERSIGHT.value],
    )

    # `id=` (the field name), not `_id=` (its Mongo alias): both are valid
    # at runtime under `HealthPolicy`'s `populate_by_name=True`, but
    # mypy's pydantic plugin only recognizes the field name as satisfying
    # the synthesized `__init__`'s required-argument check for this
    # model — passing the alias directly here (as `app.infrastructure.
    # mongodb`'s document-shaped code does when round-tripping `by_alias`
    # dicts) makes it report a spurious "Missing named argument 'id'".
    fabric_warning = HealthPolicy(
        id=new_id("health_policy"),
        name="UCS fabric path down (warning)",
        description="Fires when exactly one UCS fabric path is reported down.",
        policy_key="connectivity.fabric_paths_down_warning",
        category="connectivity",
        severity=HealthSeverity.WARNING,
        condition=Condition(metric="connectivity.fabric_paths_down", operator="EQ", value=1),
        evidence=[EvidenceField(key="down", metric="connectivity.fabric_paths_down")],
        message_template="{down} UCS fabric path is down",
        scope=fabric_scope,
        source="SYSTEM_DEFAULT",
        priority=100,
        system=True,
        created_at=now,
        updated_at=now,
    )

    fabric_critical = HealthPolicy(
        id=new_id("health_policy"),
        name="UCS fabric paths down (critical)",
        description="Fires when two or more UCS fabric paths are reported down.",
        policy_key="connectivity.fabric_paths_down_critical",
        category="connectivity",
        severity=HealthSeverity.CRITICAL,
        condition=Condition(metric="connectivity.fabric_paths_down", operator="GTE", value=2),
        evidence=[EvidenceField(key="down", metric="connectivity.fabric_paths_down")],
        message_template="{down} UCS fabric paths are down",
        scope=fabric_scope,
        source="SYSTEM_DEFAULT",
        priority=100,
        system=True,
        created_at=now,
        updated_at=now,
    )

    # There is deliberately no longer a blanket "any failed drive is
    # CRITICAL" default. It was replaced by the OS/data split below, and
    # keeping both would make a failed OS disk fire MAJOR *and* CRITICAL at
    # once — the worst-of rollup would take CRITICAL and the MAJOR tier
    # would never be reachable for the case it was added for.
    #
    # `storage.failed_drive_count` and `storage.warning_drive_count` are
    # still registered metrics: an operator who wants the old blanket rule
    # back can build it in the admin UI. Note that seeding never deletes,
    # so a deployment deployed before this change keeps its existing
    # "Failed drive present" policy until someone disables it.

    # Everything below is vendor-neutral by construction: each condition
    # reads a fact derived from the normalized `Server` document, never a
    # vendor payload, so one policy covers Dell, Cisco, DGX and standalone
    # alike. The two fabric policies above are the exception and stay one —
    # `connectivity.fabric_paths_*` is zero for anything without a fabric
    # interconnect, so they simply never fire there.
    failed_psu = HealthPolicy(
        id=new_id("health_policy"),
        name="Power supply failed",
        description=(
            "Fires when one or more power supplies report DOWN. Covers every "
            "server kind: UCS and Intersight report PSUs from OperState, and "
            "Redfish from the chassis power subsystem."
        ),
        policy_key="power.failed_psu",
        category="power",
        severity=HealthSeverity.CRITICAL,
        condition=Condition(metric="power.failed_psu_count", operator="GTE", value=1),
        evidence=[
            EvidenceField(key="failed", metric="power.failed_psu_count"),
            EvidenceField(key="total", metric="power.psu_count"),
        ],
        message_template="{failed} of {total} power supplies failed",
        scope=PolicyScope(),
        source="SYSTEM_DEFAULT",
        priority=100,
        system=True,
        created_at=now,
        updated_at=now,
    )

    # --- OS disks -------------------------------------------------------
    #
    # The OS disks are the smallest drives in the machine (see
    # `facts._os_disk_capacities`). One bad disk in a boot mirror means the
    # mirror is running unprotected — the server is still up, and the next
    # failure takes it down, which is exactly what MAJOR is for. Two bad is
    # CRITICAL: on the usual two-disk mirror there is nothing left.
    #
    # "Bad" is degraded OR failed, counted together: on a boot mirror the
    # distinction does not change what an operator does about it.
    os_disk_major = HealthPolicy(
        id=new_id("health_policy"),
        name="OS disk degraded or failed",
        description=(
            "Fires when exactly one OS disk (the smallest capacity present) "
            "reports WARNING or CRITICAL. The boot mirror is unprotected."
        ),
        policy_key="storage.os_disk_bad_major",
        category="storage",
        severity=HealthSeverity.MAJOR,
        condition=Condition(metric="storage.os_bad_disk_count", operator="EQ", value=1),
        evidence=[
            EvidenceField(key="bad", metric="storage.os_bad_disk_count"),
            EvidenceField(key="total", metric="storage.os_disk_count"),
        ],
        message_template="{bad} of {total} OS disks degraded or failed",
        scope=PolicyScope(),
        source="SYSTEM_DEFAULT",
        priority=100,
        system=True,
        created_at=now,
        updated_at=now,
    )

    os_disk_critical = HealthPolicy(
        id=new_id("health_policy"),
        name="Multiple OS disks degraded or failed",
        description=(
            "Fires when two or more OS disks report WARNING or CRITICAL. On "
            "the usual two-disk boot mirror, nothing healthy is left."
        ),
        policy_key="storage.os_disk_bad_critical",
        category="storage",
        severity=HealthSeverity.CRITICAL,
        condition=Condition(metric="storage.os_bad_disk_count", operator="GTE", value=2),
        evidence=[
            EvidenceField(key="bad", metric="storage.os_bad_disk_count"),
            EvidenceField(key="total", metric="storage.os_disk_count"),
        ],
        message_template="{bad} of {total} OS disks degraded or failed",
        scope=PolicyScope(),
        source="SYSTEM_DEFAULT",
        priority=100,
        system=True,
        created_at=now,
        updated_at=now,
    )

    # --- Data disks -----------------------------------------------------
    #
    # Split by what the server is *for*, which only its name records. A
    # large-storage node exists to hold data, so a bad data disk there
    # escalates at two; on every other server the local disks are
    # incidental and one bad disk is a warning.
    large_storage_data_warning = HealthPolicy(
        id=new_id("health_policy"),
        name="Data disk degraded (large-storage server)",
        description=(
            "Fires when exactly one non-OS disk is degraded or failed on a "
            "server whose name carries the 10TB token."
        ),
        policy_key="storage.data_disk_bad_large_warning",
        category="storage",
        severity=HealthSeverity.WARNING,
        condition=Condition(
            all_of=[
                Condition(metric="server.name_has_10tb", operator="EQ", value=True),
                Condition(metric="storage.data_bad_disk_count", operator="EQ", value=1),
            ]
        ),
        evidence=[EvidenceField(key="bad", metric="storage.data_bad_disk_count")],
        message_template="{bad} data disk degraded or failed",
        scope=PolicyScope(),
        source="SYSTEM_DEFAULT",
        priority=100,
        system=True,
        created_at=now,
        updated_at=now,
    )

    large_storage_data_critical = HealthPolicy(
        id=new_id("health_policy"),
        name="Multiple data disks degraded (large-storage server)",
        description=(
            "Fires when two or more non-OS disks are degraded or failed on a "
            "server whose name carries the 10TB token."
        ),
        policy_key="storage.data_disk_bad_large_critical",
        category="storage",
        severity=HealthSeverity.CRITICAL,
        condition=Condition(
            all_of=[
                Condition(metric="server.name_has_10tb", operator="EQ", value=True),
                Condition(metric="storage.data_bad_disk_count", operator="GTE", value=2),
            ]
        ),
        evidence=[EvidenceField(key="bad", metric="storage.data_bad_disk_count")],
        message_template="{bad} data disks degraded or failed",
        scope=PolicyScope(),
        source="SYSTEM_DEFAULT",
        priority=100,
        system=True,
        created_at=now,
        updated_at=now,
    )

    data_disk_warning = HealthPolicy(
        id=new_id("health_policy"),
        name="Data disk degraded",
        description=(
            "Fires when any non-OS disk is degraded or failed on a server "
            "that is not a large-storage node."
        ),
        policy_key="storage.data_disk_bad_warning",
        category="storage",
        severity=HealthSeverity.WARNING,
        condition=Condition(
            all_of=[
                Condition(metric="server.name_has_10tb", operator="EQ", value=False),
                Condition(metric="storage.data_bad_disk_count", operator="GTE", value=1),
            ]
        ),
        evidence=[EvidenceField(key="bad", metric="storage.data_bad_disk_count")],
        message_template="{bad} data disk(s) degraded or failed",
        scope=PolicyScope(),
        source="SYSTEM_DEFAULT",
        priority=100,
        system=True,
        created_at=now,
        updated_at=now,
    )

    # 8 TB decimal, matching how the collectors measure and the dry run
    # renders capacity. A 10TB-named node reporting less than this has lost
    # drives or was built wrong — either way it is not the machine its name
    # promises, and a workload placed by name will not fit.
    large_storage_undersized = HealthPolicy(
        id=new_id("health_policy"),
        name="Large-storage server below expected capacity",
        description=(
            "Fires when a server whose name carries the 10TB token reports "
            "less than 8 TB of total storage."
        ),
        policy_key="storage.large_storage_undersized",
        category="storage",
        severity=HealthSeverity.CRITICAL,
        condition=Condition(
            all_of=[
                Condition(metric="server.name_has_10tb", operator="EQ", value=True),
                Condition(metric="storage.total_bytes", operator="LT", value=8_000_000_000_000),
            ]
        ),
        evidence=[EvidenceField(key="total", metric="storage.total_bytes")],
        message_template="only {total} bytes of storage on a 10TB server",
        scope=PolicyScope(),
        source="SYSTEM_DEFAULT",
        priority=100,
        system=True,
        created_at=now,
        updated_at=now,
    )

    # Deliberately NOT "any link is down": a server with unused NICs has
    # down links and is perfectly healthy, so that would fire on most of
    # the fleet forever. `links_known_count GTE 1` — not `interface_count`
    # — is what separates "every link is down" from "no link state could be
    # read at all"; see ADR-0027.
    all_links_down = HealthPolicy(
        id=new_id("health_policy"),
        name="No network link up",
        description=(
            "Fires when a server reports readable link states and none of them "
            "is up. Says nothing about a server whose link states were never read."
        ),
        policy_key="network.all_links_down",
        category="network",
        severity=HealthSeverity.CRITICAL,
        condition=Condition(
            all_of=[
                Condition(metric="network.links_known_count", operator="GTE", value=1),
                Condition(metric="network.links_up_count", operator="EQ", value=0),
            ]
        ),
        evidence=[
            EvidenceField(key="interfaces", metric="network.links_known_count"),
            EvidenceField(key="reported", metric="network.interface_count"),
        ],
        message_template=(
            "no network link is up across {interfaces} of {reported} interface(s) "
            "with a readable link state"
        ),
        scope=PolicyScope(),
        source="SYSTEM_DEFAULT",
        priority=100,
        system=True,
        created_at=now,
        updated_at=now,
    )

    # The mirror of the 10TB rule, in the other direction: a 5TB-named node
    # carrying more than 6 TB is not the machine its name promises either.
    # Both are the same failure — capacity that does not match the name a
    # workload is placed by — and both are CRITICAL for that reason.
    #
    # 6 TB (not 5) leaves headroom for how a "5TB" build is actually
    # assembled and measured, the same way the 10TB rule allows down to
    # 8 TB. Both bounds are decimal.
    name_5tb_oversized = HealthPolicy(
        id=new_id("health_policy"),
        name="5TB server above expected capacity",
        description=(
            "Fires when a server whose name carries the 5TB token reports "
            "more than 6 TB of total storage."
        ),
        policy_key="storage.name_5tb_oversized",
        category="storage",
        severity=HealthSeverity.CRITICAL,
        condition=Condition(
            all_of=[
                Condition(metric="server.name_has_5tb", operator="EQ", value=True),
                Condition(metric="storage.total_bytes", operator="GT", value=6_000_000_000_000),
            ]
        ),
        evidence=[EvidenceField(key="total", metric="storage.total_bytes")],
        message_template="{total} bytes of storage on a 5TB server",
        scope=PolicyScope(),
        source="SYSTEM_DEFAULT",
        priority=100,
        system=True,
        created_at=now,
        updated_at=now,
    )

    # WARNING, not MAJOR: a degraded DIMM is a scheduled swap, not lost
    # redundancy — the server keeps running on the memory it has, and ECC
    # is doing its job until it cannot.
    degraded_dimm = HealthPolicy(
        id=new_id("health_policy"),
        name="Memory module degraded",
        description=(
            "Fires when one or more DIMMs report WARNING or CRITICAL. Only "
            "providers that read per-DIMM health populate this; one that "
            "does not leaves the count at zero and this never fires."
        ),
        policy_key="memory.degraded_dimm",
        category="memory",
        severity=HealthSeverity.WARNING,
        condition=Condition(metric="memory.degraded_dimm_count", operator="GTE", value=1),
        evidence=[
            EvidenceField(key="bad", metric="memory.degraded_dimm_count"),
            EvidenceField(key="total", metric="memory.dimm_count"),
        ],
        message_template="{bad} of {total} memory modules degraded",
        scope=PolicyScope(),
        source="SYSTEM_DEFAULT",
        priority=100,
        system=True,
        created_at=now,
        updated_at=now,
    )

    # MAJOR, between the two: the server is reachable on one link, and the
    # next failure disconnects it. Deliberately EQ 1 rather than LTE 1 —
    # zero links up is `network.all_links_down` above, and a server must
    # not report both.
    single_link_up = HealthPolicy(
        id=new_id("health_policy"),
        name="Only one network link up",
        description=(
            "Fires when exactly one interface is up. Network redundancy is "
            "gone: the server is still reachable, and one more failure "
            "disconnects it."
        ),
        policy_key="network.single_link_up",
        category="network",
        severity=HealthSeverity.MAJOR,
        condition=Condition(
            all_of=[
                Condition(metric="network.links_known_count", operator="GTE", value=2),
                Condition(metric="network.links_up_count", operator="EQ", value=1),
            ]
        ),
        evidence=[
            EvidenceField(key="up", metric="network.links_up_count"),
            EvidenceField(key="interfaces", metric="network.links_known_count"),
        ],
        message_template="only {up} of {interfaces} readable network links is up",
        scope=PolicyScope(),
        source="SYSTEM_DEFAULT",
        priority=100,
        system=True,
        created_at=now,
        updated_at=now,
    )

    failed_gpu = HealthPolicy(
        id=new_id("health_policy"),
        name="GPU failed",
        description=(
            "Fires when one or more GPUs report a failed state, in either "
            "vocabulary the providers use — CRITICAL from Redfish, DOWN from "
            "UCS and Intersight."
        ),
        policy_key="gpu.failed",
        category="gpu",
        severity=HealthSeverity.CRITICAL,
        condition=Condition(metric="gpu.failed_count", operator="GTE", value=1),
        evidence=[
            EvidenceField(key="failed", metric="gpu.failed_count"),
            EvidenceField(key="total", metric="gpu.count"),
        ],
        message_template="{failed} of {total} GPUs failed",
        scope=PolicyScope(),
        source="SYSTEM_DEFAULT",
        priority=100,
        system=True,
        created_at=now,
        updated_at=now,
    )

    # WARNING at the first one, not a threshold: an uncorrectable ECC error
    # is memory the card could not repair, and on accelerator hardware it
    # is a documented predictor of a failing card rather than routine
    # noise. Correctable errors are deliberately not checked at all — those
    # are the mechanism working, and would fire constantly.
    gpu_ecc = HealthPolicy(
        id=new_id("health_policy"),
        name="GPU uncorrectable ECC errors",
        description=(
            "Fires on any uncorrectable ECC error across the server's GPUs. "
            "Only Redfish reports these; a provider that does not leaves the "
            "count at zero and this never fires."
        ),
        policy_key="gpu.uncorrectable_errors",
        category="gpu",
        severity=HealthSeverity.WARNING,
        condition=Condition(metric="gpu.uncorrectable_error_count", operator="GTE", value=1),
        evidence=[EvidenceField(key="errors", metric="gpu.uncorrectable_error_count")],
        message_template="{errors} uncorrectable GPU ECC error(s)",
        scope=PolicyScope(),
        source="SYSTEM_DEFAULT",
        priority=100,
        system=True,
        created_at=now,
        updated_at=now,
    )

    return [
        fabric_warning,
        fabric_critical,
        failed_psu,
        os_disk_major,
        os_disk_critical,
        large_storage_data_warning,
        large_storage_data_critical,
        data_disk_warning,
        large_storage_undersized,
        name_5tb_oversized,
        degraded_dimm,
        all_links_down,
        single_link_up,
        failed_gpu,
        gpu_ecc,
    ]
