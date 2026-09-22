"""`InClusterClient.worker_nodes` — no label selector, name exclusion only.

ADR-0024's 2026-09-22 update: the `node-role.kubernetes.io/worker` label
is not reliably present on every worker across this operator's clusters,
so the label selector was dropped and `exclude_name_parts` is the only
filter left. What matters here is that no `labelSelector` reaches the
API at all, and that exclusion is still a plain substring match — a term
like `vcompute` must not also drop a node merely named `compute-01`.
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
        return _nodes_response(["compute-01"])

    nodes = await _client(handler).worker_nodes(exclude_name_parts=())

    assert "labelSelector" not in seen_params
    assert [n["metadata"]["name"] for n in nodes] == ["compute-01"]


async def test_exclude_name_parts_is_a_substring_match_not_a_prefix_of_the_other() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return _nodes_response(
            ["compute-01", "vcompute-01", "infra-01", "control-plane-01", "master-0"]
        )

    nodes = await _client(handler).worker_nodes(
        exclude_name_parts=("infra", "control-plane", "master", "vcompute")
    )

    assert [n["metadata"]["name"] for n in nodes] == ["compute-01"]


async def test_exclude_name_parts_matches_case_insensitively() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return _nodes_response(["Compute-01", "VCompute-01"])

    nodes = await _client(handler).worker_nodes(exclude_name_parts=("vcompute",))

    assert [n["metadata"]["name"] for n in nodes] == ["Compute-01"]
