"""Whether a server is in use, as OpenShift reports it.

Two jobs write here and nothing else does. Every cluster reports its own
worker nodes; each MCE reports its Agents, either bound to a hosted
cluster or unbound. `lifecycle_state` is the field to read before
trusting any other: `cluster_name` is set only when something claims the
server, and `mce_name` only by the MCE job.

**Kept strictly separate from `classification.Classification`, which is a
regex verdict on a hostname.** That separation is the platform's own rule
— a naming convention is not proof of cluster membership — and it is why
`InstallationType` and `OpenShiftState` are two enums rather than one.
`InstallationType` says what *kind* of server this is; this says whether
it is in use. When they disagree the server is misnamed or misplaced, and
that disagreement is the signal. Reconciling them silently would destroy
it.

Vendor collectors never touch this: `IngestService` carries the whole
object forward untouched on every ingest, exactly as it does
`maintenance`. A server's hardware and its cluster membership are
observed by different systems on different schedules, and neither is
entitled to blank the other's findings.

**Nothing ever reports a removal.** A server freed from a cluster simply
stops appearing in that cluster's node list, so `AVAILABLE` can never be
observed directly — it is what remains when no job claims a server. Each
job therefore reconciles the set it owns (the servers naming *its*
cluster) rather than only writing what it saw; see
`app.application.services.openshift_membership`.

A state retired from `OpenShiftState` decodes as `AVAILABLE` rather than
raising — narrowing a persisted enum is a migration, and ADR-0024 records
what skipping it cost.

Correlation is by **hostname**, not MAC. Metal3 binds an Agent to a host
via `bootMACAddress`, which would be the stronger key, but it is only
available through the `BareMetalHost` — and not every server has one, so
reading it would cover part of the fleet while looking complete. The
hostname is on the Agent itself. See the module above for the two-step
read that makes it work across vendors.

Five fields, and the five an earlier shape carried that were dropped:
`docs/adr/0024-openshift-cluster-membership.md`.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, field_validator

from app.domain.enums import OpenShiftState


class OpenShiftLifecycle(BaseModel):
    """
    One server's observed OpenShift membership.

    Attributes:
        lifecycle_state (OpenShiftState): Whether the server is in use.
            `AVAILABLE` until a job claims it, and again once one stops.
        cluster_name (str | None): The cluster holding it, on `INSTALLED`.
            `None` on `AVAILABLE`, and on `INSTALLED_TO_INVENTORY`, where
            an MCE holds the server but no cluster does. The identifier of
            a cluster, on its own: names are unique across this estate,
            including across MCEs.
        mce_name (str | None): The MCE that reported it. Set by the MCE job
            only; `None` on a plain cluster node, which no MCE knows about.
        last_reported_at (datetime | None): When a job last claimed this
            server. Diagnostic rather than load-bearing: the reconcile is
            set-based, so nothing infers availability from this going
            stale.
        reported_by_agent_id (str | None): Which job instance wrote this,
            for tracing a wrong value back to the cluster that reported it.
    """

    lifecycle_state: OpenShiftState = OpenShiftState.AVAILABLE
    cluster_name: str | None = None
    mce_name: str | None = None
    last_reported_at: datetime | None = None
    reported_by_agent_id: str | None = None

    @field_validator("lifecycle_state", mode="before")
    @classmethod
    def _decode_retired_state(cls, value: object) -> object:
        """
        Map a state this enum no longer has onto `AVAILABLE`.

        Args:
            value (object): The stored value, from MongoDB or a caller.

        Returns:
            object: `value` if the enum still has it, else `AVAILABLE`.
        """
        if isinstance(value, str) and value not in OpenShiftState.__members__:
            return OpenShiftState.AVAILABLE
        return value


class MembershipRun(BaseModel):
    """What a membership job's most recent reconcile reported — ADR-0029's 2026-09-24 update.

    Attributes:
        kind (str): `nodes` or `agents` — which CronJob wrote this.
        reported_by (str): The cluster name (`nodes`) or MCE name (`agents`).
        finished_at (datetime): When the run finished.
        duration_seconds (float): Wall-clock seconds the run took.
        observed (int): Hostnames the run reported.
        matched (int): Of those, resolved to a known server.
        unmatched (int): Of those, matched no server — a hostname the
            vendor collectors have never ingested.
        partial (bool): Whether the run exited 3 (`unmatched > 0`).
    """

    kind: str
    reported_by: str
    finished_at: datetime
    duration_seconds: float
    observed: int
    matched: int
    unmatched: int
    partial: bool
