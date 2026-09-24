"""`tools.collect_openshift._observe` — one exclusion filter, both sources.

ADR-0024's 2026-09-22 update: `nodes` and `agents` used to differ (only
`nodes` could be name-excluded); now both go through the same filter,
applied to the resolved `ClusterObservation.hostname` rather than either
source's own raw field (a node's `metadata.name`, or an Agent's
`spec.hostname`/`status.inventory.hostname`).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import pytest
from tools.collect_openshift import _observe, _record_run

from app.application.services.openshift_membership import MembershipSummary
from app.utils.timeutil import utcnow

pytestmark = pytest.mark.unit


class _FakeClient:
    """Hands back scripted nodes/agents; never makes a real request."""

    def __init__(
        self,
        *,
        nodes: Sequence[dict[str, Any]] = (),
        agents: Sequence[dict[str, Any]] = (),
    ) -> None:
        self._nodes = list(nodes)
        self._agents = list(agents)

    async def worker_nodes(self) -> list[dict[str, Any]]:
        return self._nodes

    async def agents(self) -> list[dict[str, Any]]:
        return self._agents


def _node(name: str) -> dict[str, Any]:
    return {"metadata": {"name": name}}


def _agent(hostname: str) -> dict[str, Any]:
    return {"metadata": {"name": "agent-x"}, "spec": {"hostname": hostname}, "status": {}}


async def test_excludes_nodes_by_name() -> None:
    client = _FakeClient(nodes=[_node("compute-01"), _node("master-0")])

    observations = await _observe(
        client,  # ty: ignore[invalid-argument-type]
        source="nodes",
        cluster_name="ocp4-tlv",
        mce_name="",
        exclude_name_parts=("master",),
    )

    assert [o.hostname for o in observations] == ["compute-01"]


async def test_excludes_agents_by_their_resolved_hostname_the_same_way() -> None:
    client = _FakeClient(agents=[_agent("compute-01"), _agent("master-0")])

    observations = await _observe(
        client,  # ty: ignore[invalid-argument-type]
        source="agents",
        cluster_name="",
        mce_name="mce-tlv",
        exclude_name_parts=("master",),
    )

    assert [o.hostname for o in observations] == ["compute-01"]


async def test_vcompute_does_not_also_exclude_compute_for_either_source() -> None:
    client = _FakeClient(
        nodes=[_node("compute-01"), _node("vcompute-01")],
        agents=[_agent("compute-01"), _agent("vcompute-01")],
    )

    nodes = await _observe(
        client,  # ty: ignore[invalid-argument-type]
        source="nodes",
        cluster_name="ocp4-tlv",
        mce_name="",
        exclude_name_parts=("vcompute",),
    )
    agents = await _observe(
        client,  # ty: ignore[invalid-argument-type]
        source="agents",
        cluster_name="",
        mce_name="mce-tlv",
        exclude_name_parts=("vcompute",),
    )

    assert [o.hostname for o in nodes] == ["compute-01"]
    assert [o.hostname for o in agents] == ["compute-01"]


class _FakeMembershipRunRepo:
    """Records every `record_run` call without a database."""

    def __init__(self) -> None:
        self.runs: list[Any] = []

    async def record_run(self, run: Any) -> None:
        self.runs.append(run)


async def test_record_run_writes_unmatched_and_partial() -> None:
    """`partial` mirrors `_report`'s own exit-3 decision: any unmatched host."""
    repo = _FakeMembershipRunRepo()
    summary = MembershipSummary(observed=3, matched=2, claimed=1, freed=0, unmatched=["compute-99"])

    await _record_run(
        repo,  # ty: ignore[invalid-argument-type]
        source="nodes",
        reported_by="ocp4-tlv",
        started_at=utcnow(),
        summary=summary,
    )

    [run] = repo.runs
    assert run.kind == "nodes"
    assert run.reported_by == "ocp4-tlv"
    assert run.observed == 3
    assert run.matched == 2
    assert run.unmatched == 1
    assert run.partial is True


async def test_record_run_is_not_partial_when_everything_matched() -> None:
    repo = _FakeMembershipRunRepo()
    summary = MembershipSummary(observed=1, matched=1, claimed=1, freed=0, unmatched=[])

    await _record_run(
        repo,  # ty: ignore[invalid-argument-type]
        source="agents",
        reported_by="mce-tlv",
        started_at=utcnow(),
        summary=summary,
    )

    assert repo.runs[0].partial is False


async def test_record_run_never_raises_when_the_write_fails() -> None:
    class _BrokenRepo:
        async def record_run(self, run: Any) -> None:
            raise RuntimeError("mongo down")

    summary = MembershipSummary(observed=1, matched=1, claimed=1, freed=0, unmatched=[])

    await _record_run(
        _BrokenRepo(),  # ty: ignore[invalid-argument-type]
        source="nodes",
        reported_by="ocp4-tlv",
        started_at=utcnow(),
        summary=summary,
    )
