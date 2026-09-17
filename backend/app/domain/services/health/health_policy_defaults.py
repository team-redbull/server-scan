"""Seeded system-default health policies.

`default_system_policies()` builds them; `app.application.services.bootstrap`
persists and re-syncs them. What each one is and why it carries the severity
it does — the OS/data disk split, the 1.5 TB capacity-mismatch tolerance,
the removed blanket failed-drive default — is in docs/architecture.md,
"Health policy engine" ("The system-default policies").
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
        list[HealthPolicy]: Every system default, each a fresh `HealthPolicy`
            with a new id — building, not persisting, is this function's job.
    """
    now = utcnow()
    # Only a fabric interconnect has fabric paths — see docs/adr/0030.
    fabric_scope = PolicyScope(
        vendor=Vendor.CISCO.value,
        manager_types=[ManagerType.UCS_CENTRAL.value, ManagerType.INTERSIGHT.value],
    )

    # Different keys on purpose: a shared key would make the two compete.
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

    name_capacity_mismatch = HealthPolicy(
        id=new_id("health_policy"),
        name="Storage doesn't match the server's own name",
        description=(
            "Fires when a server's name carries a -<N>tb capacity token "
            "and its total storage differs from that by more than 1.5 TB, "
            "in either direction. Generalizes the old 5TB/10TB-specific "
            "rules to any capacity token (docs/architecture.md update, "
            "2026-09-17)."
        ),
        policy_key="storage.name_capacity_mismatch",
        category="storage",
        severity=HealthSeverity.CRITICAL,
        condition=Condition(
            all_of=[
                Condition(metric="storage.name_capacity_bytes", operator="EXISTS"),
                Condition(
                    metric="storage.capacity_deviation_bytes",
                    operator="GT",
                    value=1_500_000_000_000,
                ),
            ]
        ),
        evidence=[
            EvidenceField(key="total", metric="storage.total_bytes"),
            EvidenceField(key="expected", metric="storage.name_capacity_bytes"),
        ],
        message_template="{total} bytes of storage, but the name promises {expected}",
        scope=PolicyScope(),
        source="SYSTEM_DEFAULT",
        priority=100,
        system=True,
        created_at=now,
        updated_at=now,
    )

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
        name_capacity_mismatch,
        degraded_dimm,
        all_links_down,
        single_link_up,
        failed_gpu,
        gpu_ecc,
    ]
