"""Fake OpenShift membership, assigned after ingest.

Separate from `..fake.generator` on purpose, and the separation is the
point rather than tidiness: a collector produces a `ProviderServer`, which
has no `openshift` field at all, because cluster membership is observed by
a different system on a different schedule. Seeding it through the
provider would model a data path that does not exist.

So this runs where the real jobs run: over servers already in the
database, reading what is there. It stands in for the two CronJobs
`tools.collect_openshift` drives — every cluster listing its own worker
nodes, and each MCE listing its Agents — for a deployment with no clusters
to point at.

Deterministic per server rather than per run: the state is drawn from a
`Random` seeded on the server's own id, so re-running the seeder over the
same fleet produces the same answer without depending on iteration order.
"""

from __future__ import annotations

import random

from app.domain.enums import InstallationType, OpenShiftState
from app.domain.models.openshift import OpenShiftLifecycle
from app.domain.models.server import Server
from app.utils.timeutil import utcnow

# Seeded share of servers no cluster and no MCE holds, so the inventory's
# "available" filter and the sites page's Available card have data.
_AVAILABLE_SHARE = 0.2

# Seeded share of INSTALLED servers whose vendor-manager name has since
# drifted from what OpenShift reports (ADR-0036), so the UI's mismatch chip
# and filter have data against a fleet with no real cluster to point at.
_NAME_MISMATCH_SHARE = 0.02

# Seeded share of INSTALLED servers a second job is also claiming (ADR-0041),
# so the inventory's Duplicate Server filter and the gauge have data.
_CONTESTED_SHARE = 0.01


def _mce_for(site_id: str | None) -> str:
    """
    The MCE that manages one site, one per site.

    Args:
        site_id (str | None): The server's site, or None.

    Returns:
        str: The MCE's name.
    """
    return f"mce-{site_id or 'unassigned'}"


def _reported_name(rng: random.Random, server: Server) -> str | None:
    """
    Occasionally simulate an installed server renamed in its vendor manager since (ADR-0036).

    Args:
        rng (random.Random): The server's own deterministic generator.
        server (Server): The server being installed this draw.

    Returns:
        str | None: A hostname differing from `server.name`, for about 2%
            of installed servers; `None` for the rest — the common case,
            where the vendor name and the cluster's report still agree.
    """
    tokens = server.name.split("-")
    if rng.random() >= _NAME_MISMATCH_SHARE or len(tokens) < 2:
        return None
    tokens[1] = f"renamed{rng.randint(1, 9)}"
    return "-".join(tokens).lower()


def _membership_for(server: Server, rng: random.Random) -> OpenShiftLifecycle:
    """
    What OpenShift would report about one server.

    Coherent with the server's name and classification but not forced to
    agree with it — that disagreement is the signal (ADR-0024).

    Args:
        server (Server): The stored server to report on.
        rng (random.Random): The server's own deterministic generator.

    Returns:
        OpenShiftLifecycle: Its membership. `AVAILABLE` for the share of
            the fleet no cluster holds — the default state, never a
            "nothing reported" one.
    """
    now = utcnow()
    mce = _mce_for(server.site_id)
    draw = rng.random()

    # Before the classification branches, not after: a freed server keeps
    # the `ocp4-...` name it was installed under, so a fleet where only
    # junk-named servers are ever free is the opposite of the real one.
    if draw < _AVAILABLE_SHARE:
        return OpenShiftLifecycle()

    if "hypershift" in server.name.lower() or (
        server.classification.installation_type is InstallationType.HOSTED_CLUSTER
    ):
        cluster = f"hc-{server.site_id or 'unassigned'}-{rng.randint(1, 3):02d}"
        return OpenShiftLifecycle(
            lifecycle_state=OpenShiftState.INSTALLED,
            cluster_name=cluster,
            mce_name=mce,
            last_reported_at=now,
            reported_by_agent_id=mce,
            reported_name=_reported_name(rng, server),
        )

    # A hub's own nodes are cluster nodes like any other, so both are
    # simply INSTALLED; `InstallationType` answers which kind. They get
    # distinct cluster names so `?cluster_name=` can isolate a hub.
    if server.classification.installation_type in (InstallationType.UPI, InstallationType.MCE):
        prefix = "mce" if server.classification.installation_type is InstallationType.MCE else "upi"
        cluster = f"{prefix}-{server.site_id or 'unassigned'}"
        return OpenShiftLifecycle(
            lifecycle_state=OpenShiftState.INSTALLED,
            cluster_name=cluster,
            last_reported_at=now,
            reported_by_agent_id=cluster,
            reported_name=_reported_name(rng, server),
        )

    # Registered to an MCE and bound to nothing. `cluster_name` stays None
    # rather than empty: there is no cluster, which is a different claim
    # from a cluster whose name went unread.
    return OpenShiftLifecycle(
        lifecycle_state=OpenShiftState.INSTALLED_TO_INVENTORY,
        mce_name=mce,
        last_reported_at=now,
        reported_by_agent_id=mce,
        reported_name=_reported_name(rng, server),
    )


def openshift_for(server: Server) -> OpenShiftLifecycle:
    """
    What OpenShift would report about one server, a small share also contested.

    Args:
        server (Server): The stored server to report on.

    Returns:
        OpenShiftLifecycle: Its membership; about 1% of installed servers carry
            `contested_with`, naming the other job claiming them (ADR-0041).
    """
    rng = random.Random(server.id)  # noqa: S311 - deterministic fake data
    membership = _membership_for(server, rng)
    if membership.lifecycle_state is OpenShiftState.AVAILABLE or rng.random() >= _CONTESTED_SHARE:
        return membership
    mce = _mce_for(server.site_id)
    other = f"upi-{server.site_id or 'unassigned'}"
    membership.contested_with = other if membership.reported_by_agent_id == mce else mce
    membership.contested_name = f"{server.name}-old" if rng.random() < 0.5 else server.name
    return membership
