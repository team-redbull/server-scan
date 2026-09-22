"""One observation of a server, from a cluster's own point of view.

Pure data and pure functions: `client.py` makes every API call and hands
this module plain dicts. The hostname rules live here because they are the
whole reason this design works across vendors, and they are worth testing
without a cluster.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.domain.enums import OpenShiftState


def name_excluded(hostname: str, exclude_name_parts: tuple[str, ...]) -> bool:
    """
    Whether a hostname contains one of the excluded substrings.

    The one filter for both `nodes` and `agents` (ADR-0024, 2026-09-22).

    Args:
        hostname (str): An already-`clean_hostname`-lowercased hostname.
        exclude_name_parts (tuple[str, ...]): Case-insensitive substrings
            that disqualify it.

    Returns:
        bool: True if any part matches.
    """
    return any(part and part in hostname for part in exclude_name_parts)


def clean_hostname(raw: object) -> str | None:
    """
    Reduce a reported hostname to the form server names are stored in.

    Args:
        raw (object): A hostname as a cluster reported it, in any shape.

    Returns:
        str | None: Lowercased, whitespace-stripped, with any trailing DNS
            domain removed. `None` for anything empty — never `""`, which
            is the distinction the two-step read in `agent_observation`
            depends on.
    """
    if not isinstance(raw, str):
        return None
    host = raw.strip().lower().split(".", 1)[0]
    return host or None


@dataclass(frozen=True, slots=True)
class ClusterObservation:
    """
    What one cluster reports about one machine.

    `hostname` is the match key and is never stored; everything else is
    written onto `Server.openshift` when the hostname resolves to a server.

    Attributes:
        hostname (str): The cleaned hostname to correlate on.
        lifecycle_state (OpenShiftState): What this observation claims.
        cluster_name (str | None): The cluster holding it, if any.
        mce_name (str | None): The reporting MCE, on the agents path only.
    """

    hostname: str
    lifecycle_state: OpenShiftState
    cluster_name: str | None = None
    mce_name: str | None = None


def node_observation(node: dict[str, Any], *, cluster_name: str) -> ClusterObservation | None:
    """
    Read one `Node` as an observation.

    Args:
        node (dict[str, Any]): A `Node` resource.
        cluster_name (str): The cluster this job runs in.

    Returns:
        ClusterObservation | None: The observation, or `None` for a node
            with no usable name, which is counted as unmatched rather than
            guessed at.
    """
    metadata = node.get("metadata") or {}
    hostname = clean_hostname(metadata.get("name"))
    if hostname is None:
        return None
    return ClusterObservation(
        hostname=hostname,
        lifecycle_state=OpenShiftState.INSTALLED,
        cluster_name=cluster_name,
    )


def agent_observation(agent: dict[str, Any], *, mce_name: str) -> ClusterObservation | None:
    """
    Read one `Agent` as an observation.

    The requested hostname wins and the reported one is the fallback; the
    order is what makes this work across vendors (ADR-0024).

    Args:
        agent (dict[str, Any]): An `Agent` custom resource.
        mce_name (str): The MCE this job runs in.

    Returns:
        ClusterObservation | None: `INSTALLED` with the cluster when the
            Agent is bound to one, `INSTALLED_TO_INVENTORY` when it is not.
            `None` when neither hostname is usable.
    """
    spec = agent.get("spec") or {}
    inventory = (agent.get("status") or {}).get("inventory") or {}

    # `clean_hostname` returns None, never "", so a present-but-empty
    # requested hostname falls through instead of short-circuiting.
    hostname = clean_hostname(spec.get("hostname")) or clean_hostname(inventory.get("hostname"))
    if hostname is None:
        return None

    cluster = spec.get("clusterDeploymentName") or {}
    cluster_name = cluster.get("name") if isinstance(cluster, dict) else None

    if cluster_name:
        return ClusterObservation(
            hostname=hostname,
            lifecycle_state=OpenShiftState.INSTALLED,
            cluster_name=str(cluster_name),
            mce_name=mce_name,
        )

    # Registered to the MCE, bound to nothing: the spare pool cluster
    # creation draws on. `cluster_name` stays None — there is no cluster,
    # which is a different claim from a cluster whose name went unread.
    return ClusterObservation(
        hostname=hostname,
        lifecycle_state=OpenShiftState.INSTALLED_TO_INVENTORY,
        mce_name=mce_name,
    )
