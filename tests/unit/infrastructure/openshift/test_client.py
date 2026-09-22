"""`InClusterClient.worker_nodes`/`agents` — no label selector, no filter.

ADR-0024's 2026-09-22 updates: the `node-role.kubernetes.io/worker` label
is not reliably present on every worker across this operator's clusters,
so it was dropped, and name exclusion moved out of this module entirely —
it is now one filter (`app.infrastructure.openshift.records.name_excluded`)
applied uniformly to the resolved hostname `tools.collect_openshift._observe`
builds, for both `nodes` and `agents`. This module's own job is just
listing raw resources; `test_openshift_records.py` covers the filter, and
`test_collect_openshift.py` covers it applying to both sources.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import httpx
import pytest

from app.infrastructure.openshift.client import InClusterClient

pytestmark = pytest.mark.unit


def _client(handler: Callable[[httpx.Request], httpx.Response]) -> InClusterClient:
    """
    A reader wired to a scripted transport.

    Args:
        handler (Callable[[httpx.Request], httpx.Response]): An
            `httpx.MockTransport` request handler.

    Returns:
        InClusterClient: The client under test.
    """
    http = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://kubernetes.default.svc"
    )
    return InClusterClient(http)


def _nodes_response(names: list[str]) -> httpx.Response:
    """
    One page of `Node` items, named only.

    Args:
        names (list[str]): Each node's `metadata.name`.

    Returns:
        httpx.Response: A 200 carrying them, with no `continue` token.
    """
    body: dict[str, Any] = {
        "items": [{"metadata": {"name": name}} for name in names],
        "metadata": {},
    }
    return httpx.Response(200, json=body)


async def test_worker_nodes_sends_no_label_selector() -> None:
    seen_params: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen_params.update(dict(request.url.params))
        return _nodes_response(["compute-01", "master-0"])

    nodes = await _client(handler).worker_nodes()

    assert "labelSelector" not in seen_params
    assert [n["metadata"]["name"] for n in nodes] == ["compute-01", "master-0"]
