# ADR-0036: OpenShift membership falls back to the hardware serial when a hostname is not a unique match

Date: 2026-09-27
Status: Accepted

Extends ADR-0024. Supersedes nothing.

## Context

ADR-0024's reconcile correlates a cluster's reported hostname against
`Server.name_normalized` and nothing else. That is exactly right when the
hostname is the server's only identity a cluster can see — but it silently
breaks the moment the two disagree about what a still-installed machine is
called.

The operator's own case: a server is installed into OpenShift as
`ocp-tomer-compute-01`. Someone later renames it in the vendor manager
(UCS/OME/OneView) to `ocp-toto-compute-01` — the hardware, its BMC and its
serial are untouched, and `IngestService` correlates on
`(vendor, serial_normalized)` (`docs/adr/0001`), so this is the same
`Server` document with a new `name`. The next membership run reports
`ocp-tomer-compute-01`, finds no server by that name, logs
`openshift.host_not_in_inventory`, and — because ADR-0024 frees whatever
this cluster no longer lists — releases the server to `AVAILABLE`. A
machine actually running a workload now looks free, and `GET
/servers/available` (ADR-0032) can hand it to a BMH-creation caller.

The same hostname-only correlation has a second failure mode the operator
also asked to be covered: two servers sharing one name (a genuine
duplicate — `docs/adr/0016`'s 2026-09-16 duplicate-name investigation).
Today's `_find_by_hostname` (page size 1) silently claims whichever one
Mongo's `name` sort returns first — arbitrary, and wrong whenever the two
are different physical machines.

## Alternatives considered and rejected

**`BareMetalHost`.** Ironic-inspected hardware, including a real serial,
would need no SSH at all. Rejected: this operator's clusters are UPI, and
`BareMetalHost`/metal3 does not exist on a UPI cluster (confirmed —
`hardwareProfile` came back empty on inspection). ADR-0024 already
declined to read BMH for coverage reasons (not every server has one); a
UPI estate has none.

**`oc debug node/<node>`.** Runs a command on the node via a scheduled
debug pod, using only the Kubernetes API the job already has access to —
no new credential, no new network path. Rejected: it requires the target
node to accept a new pod, so it cannot reach exactly the case that matters
most here — a node that has gone `NotReady`, which is plausibly correlated
with the same drift this fallback exists to catch.

**`Node.status.nodeInfo.systemUUID` / `Server.identity.system_uuid`.**
Already read off every node with no new I/O. Rejected on two counts: OME
never populates a comparable identity on Dell, and on Cisco UCS,
`system_uuid` is drawn from an admin-managed UUID Suffix Pool tied to the
*service profile*, not the physical chassis — a live domain proved two
different real servers can legitimately share one (ADR-0016's 2026-09-09
update; `app.infrastructure.mongodb.indexes`'s module docstring). It is
why `system_uuid`'s index was made non-unique and why correlation has
never used it. Building a *new* correlation path on the same field ADR-
0016 already disqualified for this exact reason would reopen a settled
question.

## Decision

### 1. Resolve by serial only when the hostname is not a unique match

`OpenShiftMembershipService._find_by_hostname` now asks for up to two
candidates (`page_size=2`) instead of one, so it can tell "no match,"
"exactly one match" and "two or more share this name" apart.

- **Exactly one candidate**: unchanged — claimed by name, as ADR-0024
  always did.
- **Zero, or two-or-more candidates**: the service resolves the machine's
  real hardware serial and looks up `identity.serial_normalized` instead.
  A resolved, unique serial match is claimed regardless of what its name
  is; if that name differs from what the cluster reported, the reported
  hostname is recorded (`OpenShiftLifecycle.reported_name`, see below).
  Two-or-more servers sharing that *serial* is logged
  (`openshift.serial_ambiguous`) and left unmatched — uniqueness on
  `identity.serial_normalized` is enforced only per vendor
  (`indexes.uniq_vendor_serial`), so a cross-vendor collision, while rare,
  is possible and must not be guessed at.

This applies uniformly to a rename (zero hostname candidates, one real
server) and a duplicate name (two hostname candidates, the serial picks
the right one and the loser is freed like any other server this cluster
no longer holds).

### 2. Where the serial comes from

- **Agents**: `status.inventory.systemVendor.serialNumber` on the Agent CR
  itself (`records.agent_observation`) — assisted-service's own hardware
  inventory, already collected, no SSH needed.
- **Nodes**: no equivalent exists on a plain `Node` object, so
  `SshSerialReader` (`app.infrastructure.openshift.node_serial`) connects
  to the node's `InternalIP` (`records.node_internal_ip`, read from
  `status.addresses` — no new RBAC, `list nodes` already returns it) as
  `core` with a shared private key, and runs
  `sudo cat /sys/class/dmi/id/product_serial`. RHCOS's `core` user carries
  passwordless sudo by default. A placeholder BIOS value
  (`normalize.PLACEHOLDER_SERIALS` — the same SMBIOS placeholder list
  Redfish's `mapping._clean_serial` already used, promoted to
  `app.domain.services.normalize` as a shared source of truth) is treated
  as unreadable, not as a real serial.

Resolution runs **only** for the misses/duplicates on one run, never for
every node every 15 minutes — bounded further by
`INVENTORY_OPENSHIFT_SSH_CONCURRENCY` (default 10) inside
`SshSerialReader`.

### 3. `asyncssh`, not the `ssh` binary

The API image (`Containerfile`) is `ubi9-minimal` with no
`openssh-clients` installed, runs under OpenShift's arbitrary-UID model
with no matching `/etc/passwd` entry (which OpenSSH refuses to run
under), and the nodes-status chart's container `securityContext`
(`_helpers.tpl`'s `nodesStatus.securityContext`) sets
`readOnlyRootFilesystem: true`, so there is nowhere to write
`~/.ssh/known_hosts` even if the binary existed. `asyncssh` is pure
Python, needs no on-disk `HOME`, runs natively on the job's existing
`asyncio` loop, and its only hard dependency, `cryptography`, is already
pinned (`pyproject.toml`). Rejected for the same reason ADR-0024's
Decision 2 rejected a Kubernetes SDK: the alternative (shelling out) drags
in a system package this air-gapped image does not carry and fights the
container's own security posture instead of working with it.

### 4. Host keys are not verified

`SshSerialReader` connects with `known_hosts=None`. This matches the
operator's own existing SSH tooling for these nodes (a shared key, no
host-key pinning) rather than inventing a stricter model this session
would then have to operate. Accepted risk, scoped to the internal node
network these jobs already run on — a spoofed node could return a false
serial, which at worst makes one server's `openshift.reported_name`
wrong, never a credential or command-execution risk beyond what the
shared key already grants. A future session wanting to close this needs a
`known_hosts` distribution mechanism (a ConfigMap seeded at node
provisioning), which is genuinely new operational state, not a code
change.

### 5. An unresolved serial protects only its own candidates from freeing

If an observation's serial cannot be read this run (SSH failure, timeout,
placeholder value), that hostname is left unmatched, recorded as
`MembershipSummary.unresolved`/`MembershipRun.unresolved`. For a
**duplicate name**, its specific hostname candidates (already known —
`_find_by_hostname` found them) are held back from this run's
free-on-absence pass (ADR-0024) and nothing else in scope is affected. A
**plain miss** has no candidate to hold back at all; it protects nothing
beyond itself. See the 2026-09-27 update below for why this is scoped
rather than blanket.

### 6. `OpenShiftLifecycle.reported_name`

A new, sixth field: the hostname the cluster actually reported, set only
when it differs from `Server.name`; `None` otherwise, including once a
rename is corrected. ADR-0024's Decision 3 explicitly dropped a
`node_name` field ("it is the server's name, which is what the hostname
correlation just proved") — `reported_name` is not that field revived. It
exists only to hold the cluster's *conflicting* claim, precisely when the
hostname correlation did **not** prove the two agree — the case ADR-0024
had no way to represent. Defaults to `None` so a document written before
this field existed decodes unchanged (no stored-shape migration needed).

## Consequences

- **New dependency**: `asyncssh==2.24.0` (`pyproject.toml`,
  `requirements.txt`/`pylock.toml` regenerated — `docs/air-gap.md`).
- **New credential surface**: an SSH private key, shared across every node
  in a cluster, held in a chart-managed Secret and mounted into the
  nodes-status CronJob pod — a new kind of secret this platform did not
  previously hold, alongside the existing Mongo URI/cursor-secret and
  vendor-manager credentials.
- **New network path**: the nodes-status pod now needs to reach every
  node's `InternalIP` on port 22, not just the Kubernetes API server.
- **Known gap**: a rename **onto another server's exact existing name**
  still hostname-matches the wrong server, because serial resolution only
  triggers on a miss or a duplicate — a unique (if wrong) hostname match
  is never second-guessed. Closing this would mean verifying every
  hostname match's serial on every run, which reintroduces the SSH fan-out
  this decision deliberately avoided.
- `matched_by_serial`/`unresolved` join `MembershipRun` and the
  Prometheus gauges (`server_scan_membership_last_run_matched_by_serial`/
  `_unresolved`), and `openshift_name_mismatches` joins `FleetSnapshot`
  (`server_scan_openshift_name_mismatch_servers`) — see ADR-0029's dated
  update.
- Agents were already unaffected by any of the SSH machinery above; only
  the nodes path gained a dependency on a reachable, key-authorized node.

## Update (2026-09-27): scope the freeze to what's actually ambiguous

Decision 5's original blanket rule shipped a real correctness failure of
its own, hit within hours of deploying: a UPI cluster had one node
(`compute-80`) that was `NotReady` and never answered SSH at all. Every
15-minute run therefore had one unresolved observation, which — under the
original rule — skipped the **entire** cluster's free-on-absence pass,
every run, indefinitely. Separately, that same cluster had a genuine
duplicate name (`ocp-tomer-compute-06` / `ocp-tomer-compute-6`, one HP one
Cisco) that a pre-ADR-0036 run had already claimed onto the wrong physical
machine. The fallback now correctly re-resolves that duplicate to the
right (Cisco) server on every run — but because `compute-80`'s unrelated
failure blocked all freeing, the stale wrong (HP) claim could never be
released. Two machines both showed `INSTALLED` for the same node name,
and would have stayed that way for as long as `compute-80` stayed broken
— a `NotReady` node with nothing to do with either duplicate candidate.

**The blast radius was wrong, not the caution.** The actual risk Decision
5 protects against is narrow and identifiable: for a *duplicate* name, the
serial fallback already knows the small set of candidate servers a failed
read leaves ambiguous — freeing either one on a guess is the risk. For a
*miss* (zero hostname candidates), there is no candidate at all: the
unreadable node's true identity, if any, could in principle be any server
in the whole fleet, not specifically one of the servers this cluster
already claims. Freezing every other server in scope to guard against an
unidentifiable single machine trades a rare, narrow risk (wrongly freeing
one server, only if a rename and an SSH failure happen to coincide on the
very same node in the very same run) for a guaranteed, unbounded one
(freeing never works at all, for the whole cluster, for as long as any one
node stays broken).

**Fix:** `_resolve` now takes a `protected_ids: set[str]` accumulator.
A duplicate whose serial can't be read adds its own candidates' ids to it;
a miss adds nothing. `reconcile`'s free-on-absence pass skips a server if
its id is in `seen_ids` **or** `protected_ids` — everything else in scope
is freed normally regardless of what else was unresolved that run.
`MembershipSummary.unresolved` still records every unresolved hostname (for
`_report` and the Prometheus gauges), it just no longer gates freeing by
itself. Options considered and rejected: requiring N consecutive absent
runs before freeing anything (a real per-server state machine — new stored
fields, a schema migration, and materially more to test — for a case
Decision 5's original blast radius, not its intent, actually caused);
excluding a long-`NotReady` node from observations entirely (does not
generalize — a node can be `Ready` and still fail SSH, e.g. a firewall
change or a rotated key, and the fix needed to cover that too).

**Residual, accepted risk:** a miss whose true physical server happens to
already be `INSTALLED` for this exact cluster under some other name, on a
run where that same node also fails to answer SSH, is freed rather than
protected. This requires a rename and an SSH outage to land on the same
node in the same run — narrower than the bug this ADR originally fixed,
and self-corrects the next time SSH succeeds (the fallback re-resolves it
by serial and re-claims it).
