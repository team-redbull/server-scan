"""Health evaluation tests, including the platform spec's own named
acceptance scenario (Cisco UCS fabric path health) and the site-override
shadowing mechanism that's the core design problem this engine solves.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from app.domain.enums import HealthSeverity
from app.domain.models.health_policy import HealthPolicy, PolicyScope
from app.domain.services.health.conditions import Condition
from app.domain.services.health.evaluate import evaluate_health, resolve_families
from app.domain.services.health.metrics import MetricDef, MetricRegistry, MetricType

NOW = datetime.now(UTC)


def _registry() -> MetricRegistry:
    r = MetricRegistry()
    r.register(
        MetricDef(
            name="connectivity.fabric_paths_down",
            type=MetricType.INT,
            category="connectivity",
            description="",
            resolver=lambda f: f.get("connectivity.fabric_paths_down", 0),
        )
    )
    r.register(
        MetricDef(
            name="connectivity.fabric_paths_up",
            type=MetricType.INT,
            category="connectivity",
            description="",
            resolver=lambda f: f.get("connectivity.fabric_paths_up", 0),
        )
    )
    r.register(
        MetricDef(
            name="storage.failed_drive_count",
            type=MetricType.INT,
            category="storage",
            description="",
            resolver=lambda f: f.get("storage.failed_drive_count", 0),
        )
    )
    return r


REGISTRY = _registry()


def _policy(
    policy_id: str,
    *,
    policy_key: str,
    category: str,
    severity: HealthSeverity,
    condition: Condition,
    priority: int = 100,
    source: str = "SYSTEM_DEFAULT",
    scope: PolicyScope | None = None,
    mode: str = "EVALUATE",
    enabled: bool = True,
    message_template: str = "{count}",
) -> HealthPolicy:
    return HealthPolicy(
        _id=policy_id,
        name=policy_id,
        policy_key=policy_key,
        mode=mode,
        category=category,
        severity=severity,
        condition=condition,
        message_template=message_template,
        scope=scope or PolicyScope(),
        source=source,
        priority=priority,
        enabled=enabled,
        created_at=NOW,
        updated_at=NOW,
    )


# --- Spec §71 acceptance scenario: Cisco UCS fabric path health ---

ONE_PATH_DOWN_WARNING = _policy(
    "hp_one_down",
    policy_key="connectivity.fabric_paths_down",
    category="connectivity",
    severity=HealthSeverity.WARNING,
    condition=Condition(metric="connectivity.fabric_paths_down", operator="EQ", value=1),
    priority=100,
)
TWO_PATHS_DOWN_CRITICAL = _policy(
    "hp_two_down",
    policy_key="connectivity.fabric_paths_down_critical",
    category="connectivity",
    severity=HealthSeverity.CRITICAL,
    condition=Condition(metric="connectivity.fabric_paths_down", operator="GTE", value=2),
    priority=100,
)
FABRIC_POLICIES = [ONE_PATH_DOWN_WARNING, TWO_PATHS_DOWN_CRITICAL]


def test_two_paths_up_zero_down_is_healthy() -> None:
    facts = {"connectivity.fabric_paths_up": 2, "connectivity.fabric_paths_down": 0}
    state = evaluate_health(
        facts, FABRIC_POLICIES, REGISTRY, vendor="cisco", manager_type=None, site_id=None
    )
    assert state.categories["connectivity"].severity == HealthSeverity.HEALTHY
    assert state.overall == HealthSeverity.HEALTHY


def test_one_path_down_is_warning() -> None:
    facts = {"connectivity.fabric_paths_up": 1, "connectivity.fabric_paths_down": 1}
    state = evaluate_health(
        facts, FABRIC_POLICIES, REGISTRY, vendor="cisco", manager_type=None, site_id=None
    )
    assert state.categories["connectivity"].severity == HealthSeverity.WARNING
    assert state.overall == HealthSeverity.WARNING
    firing = [e for e in state.evaluations if e.active]
    assert len(firing) == 1
    assert firing[0].policy_id == "hp_one_down"


def test_two_paths_down_is_critical() -> None:
    facts = {"connectivity.fabric_paths_up": 0, "connectivity.fabric_paths_down": 2}
    state = evaluate_health(
        facts, FABRIC_POLICIES, REGISTRY, vendor="cisco", manager_type=None, site_id=None
    )
    assert state.categories["connectivity"].severity == HealthSeverity.CRITICAL
    assert state.overall == HealthSeverity.CRITICAL


def test_changing_threshold_flips_evaluation_with_no_code_change() -> None:
    """The one test that actually proves the engine isn't hard-coded: the
    exact same evaluate_health() call, with only a policy's `value`
    changed, produces a different severity.
    """
    facts = {"connectivity.fabric_paths_down": 1}
    lenient = _policy(
        "hp_x",
        policy_key="k",
        category="connectivity",
        severity=HealthSeverity.CRITICAL,
        condition=Condition(metric="connectivity.fabric_paths_down", operator="GTE", value=5),
    )
    state = evaluate_health(
        facts, [lenient], REGISTRY, vendor="cisco", manager_type=None, site_id=None
    )
    assert state.overall == HealthSeverity.HEALTHY

    strict = _policy(
        "hp_x",
        policy_key="k",
        category="connectivity",
        severity=HealthSeverity.CRITICAL,
        condition=Condition(metric="connectivity.fabric_paths_down", operator="GTE", value=1),
    )
    state2 = evaluate_health(
        facts, [strict], REGISTRY, vendor="cisco", manager_type=None, site_id=None
    )
    assert state2.overall == HealthSeverity.CRITICAL


# --- The override / shadowing mechanism ---


def test_site_override_shadows_global_default_for_that_site_only() -> None:
    global_critical = _policy(
        "hp_global",
        policy_key="connectivity.fabric_paths_down",
        category="connectivity",
        severity=HealthSeverity.CRITICAL,
        condition=Condition(metric="connectivity.fabric_paths_down", operator="GTE", value=2),
        source="GLOBAL_CUSTOM",
        priority=200,
    )
    site_warning = _policy(
        "hp_site_lab",
        policy_key="connectivity.fabric_paths_down",  # SAME key -> same family
        category="connectivity",
        severity=HealthSeverity.WARNING,
        condition=Condition(metric="connectivity.fabric_paths_down", operator="GTE", value=2),
        source="SITE_CUSTOM",
        priority=500,
        scope=PolicyScope(site_id="site_lab"),
    )
    facts = {"connectivity.fabric_paths_down": 2}

    lab_state = evaluate_health(
        facts,
        [global_critical, site_warning],
        REGISTRY,
        vendor="cisco",
        manager_type=None,
        site_id="site_lab",
    )
    assert lab_state.overall == HealthSeverity.WARNING
    assert len(lab_state.evaluations) == 1
    assert lab_state.evaluations[0].policy_id == "hp_site_lab"

    other_site_state = evaluate_health(
        facts,
        [global_critical, site_warning],
        REGISTRY,
        vendor="cisco",
        manager_type=None,
        site_id="site_other",
    )
    assert other_site_state.overall == HealthSeverity.CRITICAL
    assert other_site_state.evaluations[0].policy_id == "hp_global"


def test_shadowed_family_member_is_recorded() -> None:
    global_critical = _policy(
        "hp_global",
        policy_key="k",
        category="connectivity",
        severity=HealthSeverity.CRITICAL,
        condition=Condition(metric="connectivity.fabric_paths_down", operator="GTE", value=1),
        priority=200,
        source="GLOBAL_CUSTOM",
    )
    site_warning = _policy(
        "hp_site",
        policy_key="k",
        category="connectivity",
        severity=HealthSeverity.WARNING,
        condition=Condition(metric="connectivity.fabric_paths_down", operator="GTE", value=1),
        priority=500,
        source="SITE_CUSTOM",
        scope=PolicyScope(site_id="site_lab"),
    )
    _winners, shadowed = resolve_families(
        [global_critical, site_warning], vendor="cisco", manager_type=None, site_id="site_lab"
    )
    assert len(shadowed) == 1
    assert shadowed[0].policy_id == "hp_global"
    assert shadowed[0].shadowed_by == "hp_site"


def test_disabled_high_precedence_family_member_suppresses_the_whole_family() -> None:
    global_default = _policy(
        "hp_global",
        policy_key="k",
        category="connectivity",
        severity=HealthSeverity.CRITICAL,
        condition=Condition(metric="connectivity.fabric_paths_down", operator="GTE", value=1),
        priority=200,
        source="GLOBAL_CUSTOM",
    )
    site_disable = _policy(
        "hp_site_off",
        policy_key="k",
        category="connectivity",
        severity=HealthSeverity.CRITICAL,
        condition=Condition(metric="connectivity.fabric_paths_down", operator="GTE", value=1),
        priority=500,
        source="SITE_CUSTOM",
        scope=PolicyScope(site_id="site_lab"),
        enabled=False,
    )
    facts = {"connectivity.fabric_paths_down": 1}
    state = evaluate_health(
        facts,
        [global_default, site_disable],
        REGISTRY,
        vendor="cisco",
        manager_type=None,
        site_id="site_lab",
    )
    assert state.evaluations == []
    assert state.categories["connectivity"].severity == HealthSeverity.UNKNOWN


def test_suppress_mode_records_suppression_and_fires_nothing() -> None:
    policy = _policy(
        "hp_suppressed",
        policy_key="k",
        category="power",
        severity=HealthSeverity.WARNING,
        condition=Condition(metric="storage.failed_drive_count", operator="GTE", value=1),
        mode="SUPPRESS",
    )
    state = evaluate_health({}, [policy], REGISTRY, vendor="cisco", manager_type=None, site_id=None)
    assert state.evaluations == []
    assert len(state.suppressed) == 1
    assert state.suppressed[0].policy_id == "hp_suppressed"


def test_different_policy_keys_fire_independently() -> None:
    fabric_policy = _policy(
        "hp_fabric",
        policy_key="fabric",
        category="connectivity",
        severity=HealthSeverity.CRITICAL,
        condition=Condition(metric="connectivity.fabric_paths_down", operator="GTE", value=1),
    )
    storage_policy = _policy(
        "hp_storage",
        policy_key="storage",
        category="storage",
        severity=HealthSeverity.CRITICAL,
        condition=Condition(metric="storage.failed_drive_count", operator="GTE", value=1),
    )
    facts = {"connectivity.fabric_paths_down": 1, "storage.failed_drive_count": 1}
    state = evaluate_health(
        facts,
        [fabric_policy, storage_policy],
        REGISTRY,
        vendor="cisco",
        manager_type=None,
        site_id=None,
    )
    assert len(state.evaluations) == 2
    assert {e.policy_id for e in state.evaluations} == {"hp_fabric", "hp_storage"}


# --- Aggregation edge cases ---


def test_category_with_no_policies_at_all_is_unknown_and_does_not_lower_overall() -> None:
    policy = _policy(
        "hp_x",
        policy_key="k",
        category="connectivity",
        severity=HealthSeverity.CRITICAL,
        condition=Condition(metric="connectivity.fabric_paths_down", operator="GTE", value=1),
    )
    state = evaluate_health(
        {"connectivity.fabric_paths_down": 1},
        [policy],
        REGISTRY,
        vendor="cisco",
        manager_type=None,
        site_id=None,
    )
    # "storage" has zero policies -> UNKNOWN, but overall must still be
    # CRITICAL (from connectivity), not dragged down by UNKNOWN=0.
    assert state.categories["storage"].severity == HealthSeverity.UNKNOWN
    assert state.overall == HealthSeverity.CRITICAL


def test_evaluated_but_not_firing_category_is_healthy_not_unknown() -> None:
    policy = _policy(
        "hp_x",
        policy_key="k",
        category="power",
        severity=HealthSeverity.CRITICAL,
        condition=Condition(metric="storage.failed_drive_count", operator="GTE", value=99),
    )
    state = evaluate_health({}, [policy], REGISTRY, vendor="cisco", manager_type=None, site_id=None)
    assert state.categories["power"].severity == HealthSeverity.HEALTHY


# --- All-unknown NICs make the server UNKNOWN; all-unknown disks only the storage row ---


def _storage_policy() -> HealthPolicy:
    return _policy(
        "hp_disk",
        policy_key="k",
        category="storage",
        severity=HealthSeverity.CRITICAL,
        condition=Condition(metric="storage.failed_drive_count", operator="GTE", value=1),
    )


def _overall(facts: dict[str, Any]) -> HealthSeverity:
    state = evaluate_health(
        facts, [_storage_policy()], REGISTRY, vendor="dell", manager_type=None, site_id=None
    )
    return state.overall


def test_all_unknown_nics_turn_a_healthy_server_unknown() -> None:
    assert _overall({"storage.failed_drive_count": 0, "network.all_unknown": True}) == (
        HealthSeverity.UNKNOWN
    )


def test_all_unknown_disks_show_storage_unknown_but_the_server_stays_healthy() -> None:
    facts = {
        "storage.failed_drive_count": 0,
        "storage.all_unknown": True,
        "connectivity.fabric_paths_down": 0,
    }
    state = evaluate_health(
        facts,
        [_storage_policy(), ONE_PATH_DOWN_WARNING],
        REGISTRY,
        vendor="dell",
        manager_type=None,
        site_id=None,
    )
    assert state.categories["storage"].severity == HealthSeverity.UNKNOWN
    assert state.categories["connectivity"].severity == HealthSeverity.HEALTHY
    assert state.overall == HealthSeverity.HEALTHY


def test_all_unknown_disks_never_hide_a_failure_in_another_category() -> None:
    facts = {
        "storage.failed_drive_count": 0,
        "storage.all_unknown": True,
        "connectivity.fabric_paths_down": 1,
    }
    state = evaluate_health(
        facts, [_storage_policy(), ONE_PATH_DOWN_WARNING], REGISTRY,
        vendor="dell", manager_type=None, site_id=None,
    )  # fmt: skip
    assert state.overall == HealthSeverity.WARNING


def test_all_unknown_never_hides_a_real_failure() -> None:
    facts = {"storage.failed_drive_count": 1, "network.all_unknown": True}
    assert _overall(facts) == HealthSeverity.CRITICAL


def test_known_readings_leave_a_healthy_server_healthy() -> None:
    facts = {"storage.failed_drive_count": 0, "network.all_unknown": False}
    assert _overall(facts) == HealthSeverity.HEALTHY
