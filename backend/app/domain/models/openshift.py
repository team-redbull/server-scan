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

Eight fields (three track a contested claim, ADR-0041); the five an earlier shape
carried and dropped:
`docs/adr/0024-openshift-cluster-membership.md`.

**`reported_name` (ADR-0036) is the one exception to hostname-only
correlation.** A vendor-side rename (UCS/OME/OneView) changes `Server.name`
but not the hardware, so the hostname the cluster reports stops matching
the server it names. When that happens the reconcile falls back to the
machine's hardware serial and sets `reported_name` to what the cluster
actually reported, so the server stays `INSTALLED` instead of being freed
as absent. `None` means the two agree.
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
        reported_name (str | None): The hostname OpenShift reported, set
            only when it differs from `Server.name` (ADR-0036). `None`
            means the cluster's name and the inventory's agree.
        previous_reporter (str | None): The claim that held this server
            before the current one took it over (a reporter, or
            `mce/hosted-cluster` for a bound agent); `None` if nobody did.
        claim_changed_at (datetime | None): When `reported_by_agent_id`
            took it over from `previous_reporter`.
        contested_with (str | None): The other claim still on this
            server, set when ownership flipped back to `previous_reporter`
            within an hour (ADR-0041); `None` when uncontested.
        contested_name (str | None): The hostname the other claim reports for
            the server (`Server.name` when it reports no different one); `None`
            when uncontested.
    """

    lifecycle_state: OpenShiftState = OpenShiftState.AVAILABLE
    cluster_name: str | None = None
    mce_name: str | None = None
    last_reported_at: datetime | None = None
    reported_by_agent_id: str | None = None
    reported_name: str | None = None
    previous_reporter: str | None = None
    claim_changed_at: datetime | None = None
    contested_with: str | None = None
    contested_name: str | None = None

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
            vendor collectors have never ingested, or whose hardware serial
            could not be resolved either (ADR-0036).
        partial (bool): Whether the run exited 3 (`unmatched > 0`).
        matched_by_serial (int): Of `matched`, resolved via the serial
            fallback rather than a unique hostname match (ADR-0036).
        unresolved (int): Hostnames that missed by name and whose serial
            could not be read this run (SSH failure, timeout, or a
            placeholder BIOS value) — a subset of `unmatched`. A non-zero
            count also skips freeing this run, since the ambiguous host
            might be the very server this cluster still holds.
    """

    kind: str
    reported_by: str
    finished_at: datetime
    duration_seconds: float
    observed: int
    matched: int
    unmatched: int
    partial: bool
    matched_by_serial: int = 0
    unresolved: int = 0
