"""The Kubernetes API, read from inside the cluster the job runs in.

`httpx` against `kubernetes.default.svc` rather than a Kubernetes SDK.
Every SDK worth using pulls `google-auth`, `oauthlib`, `requests` and
`websocket-client` behind it, which is a real supply-chain decision in an
air-gapped mirror — ADR-0017 rejected a large SDK on the same grounds —
and this needs two GETs. `httpx` is already a dependency and every other
HTTP collector here uses it.

In-cluster auth is two files, so there is no kubeconfig to parse and no
YAML parser to add: a bearer token and a CA bundle, both mounted by
Kubernetes into every pod with a ServiceAccount token.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import structlog

logger = structlog.get_logger(__name__)

_SA_DIR = Path("/var/run/secrets/kubernetes.io/serviceaccount")
_API = "https://kubernetes.default.svc"

# The Agent CRD's group. The *version* is discovered at runtime rather
# than pinned, which is the one real advantage `oc get agents` had: the
# job keeps working when MCE moves the CRD from v1beta1 to v1.
_AGENT_GROUP = "agent-install.openshift.io"


class ClusterUnreadableError(Exception):
    """The cluster could not be read, so nothing may be concluded from it.

    Raised rather than returning an empty list: the reconcile frees what it
    does not see, so a failed read must not look like an empty cluster.
    """


def in_cluster_client(*, timeout_seconds: float) -> httpx.AsyncClient:
    """
    Build an authenticated client for this pod's own cluster.

    Args:
        timeout_seconds (float): Per-request timeout.

    Returns:
        httpx.AsyncClient: Bearer-authenticated, verifying against the
            cluster's own CA.

    Raises:
        ClusterUnreadableError: If the ServiceAccount token or CA is
            missing — which means the pod was deployed without
            `automountServiceAccountToken`, not that the cluster is down.
    """
    token_file = _SA_DIR / "token"
    ca_file = _SA_DIR / "ca.crt"
    try:
        token = token_file.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise ClusterUnreadableError(
            f"No ServiceAccount token at {token_file}. This job needs "
            "automountServiceAccountToken: true and a ServiceAccount bound to a "
            "ClusterRole that can list nodes and agents."
        ) from exc
    if not ca_file.is_file():
        raise ClusterUnreadableError(f"No cluster CA at {ca_file}.")

    return httpx.AsyncClient(
        base_url=_API,
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        verify=str(ca_file),
        timeout=timeout_seconds,
    )


class InClusterClient:
    """Reads nodes and agents from the cluster this pod runs in."""

    def __init__(self, http: httpx.AsyncClient) -> None:
        """
        Build the reader.

        Args:
            http (httpx.AsyncClient): An authenticated client, from
                `in_cluster_client`.
        """
        self._http = http

    async def _get(self, path: str, params: dict[str, str] | None = None) -> dict[str, Any]:
        """
        One GET, with every failure turned into `ClusterUnreadableError`.

        Args:
            path (str): API path.
            params (dict[str, str] | None): Query parameters.

        Returns:
            dict[str, Any]: The decoded body.

        Raises:
            ClusterUnreadableError: On any transport error or non-2xx.
        """
        try:
            response = await self._http.get(path, params=params)
            response.raise_for_status()
            body: dict[str, Any] = response.json()
        except httpx.HTTPStatusError as exc:
            raise ClusterUnreadableError(
                f"{path} returned {exc.response.status_code}. A 403 here means the "
                "ServiceAccount's ClusterRole is missing list permission."
            ) from exc
        except (httpx.HTTPError, ValueError) as exc:
            raise ClusterUnreadableError(f"{path} could not be read: {exc}") from exc
        else:
            return body

    async def _list_all(self, path: str, params: dict[str, str] | None = None) -> list[Any]:
        """
        Follow `continue` to the end of a paged collection.

        A failure part-way raises rather than returning what arrived: a
        truncated read would silently free real servers.

        Args:
            path (str): Collection path.
            params (dict[str, str] | None): Query parameters.

        Returns:
            list[Any]: Every item across every page.

        Raises:
            ClusterUnreadableError: On any failure, at any page.
        """
        items: list[Any] = []
        page_params = dict(params or {})
        while True:
            body = await self._get(path, page_params)
            items.extend(body.get("items") or [])
            token = (body.get("metadata") or {}).get("continue")
            if not token:
                return items
            page_params["continue"] = str(token)

    async def worker_nodes(self, *, exclude_name_parts: tuple[str, ...]) -> list[dict[str, Any]]:
        """
        Every node in this cluster, minus the excluded names.

        No label selector — name exclusion is the only filter (ADR-0024's
        2026-09-22 update).

        Args:
            exclude_name_parts (tuple[str, ...]): Substrings that
                disqualify a node, matched case-insensitively.

        Returns:
            list[dict[str, Any]]: The `Node` resources that count.

        Raises:
            ClusterUnreadableError: If the cluster could not be read.
        """
        nodes = await self._list_all("/api/v1/nodes")
        kept: list[dict[str, Any]] = []
        for node in nodes:
            name = str((node.get("metadata") or {}).get("name") or "").lower()
            if any(part and part in name for part in exclude_name_parts):
                continue
            kept.append(node)
        logger.info(
            "openshift.nodes_listed",
            listed=len(nodes),
            kept=len(kept),
            excluded=len(nodes) - len(kept),
        )
        return kept

    async def agents(self) -> list[dict[str, Any]]:
        """
        Every `Agent` this MCE knows, across all namespaces.

        Returns:
            list[dict[str, Any]]: The `Agent` custom resources.

        Raises:
            ClusterUnreadableError: If the group is absent — which means
                this is not an MCE hub, and is worth failing on rather
                than reporting zero agents.
        """
        discovery = await self._get(f"/apis/{_AGENT_GROUP}")
        version = (discovery.get("preferredVersion") or {}).get("version")
        if not version:
            raise ClusterUnreadableError(
                f"{_AGENT_GROUP} reports no preferred version; this cluster does not "
                "look like an MCE hub."
            )
        agents = await self._list_all(f"/apis/{_AGENT_GROUP}/{version}/agents")
        logger.info("openshift.agents_listed", version=version, agents=len(agents))
        return agents
