# ADR-0035: A short-TTL install reservation, so two MCEs cannot draw one server

Date: 2026-09-27
Status: Accepted

## Context

`GET /servers/available` hands out candidates **without reserving them**.
ADR-0032 accepted that explicitly (decision 3: "No reservation/lock:
concurrent callers can race and receive the same server — an accepted gap,
not solved with new state"), and its 2026-09-26 update narrowed the
statement rather than the gap: "Installing several servers from one pool
concurrently needs a real short-TTL reservation here; that is its own
ADR." This is that ADR.

The gap has a shape worth stating precisely, because the obvious
mitigations do not cover it.

A server stays `AVAILABLE` until a **cluster** reports the node, which is
minutes after an install begins — and `openshift.lifecycle_state` is only
refreshed by a collection run, on a 6-hour cron. So between a caller
drawing a machine and that machine looking taken there is a long window in
which the same machine is drawn again.

`install-server` already defends the window two ways, and neither is
enough:

1. **Its workflow id.** `install-server-<infraEnv>` keys on the candidate
   *pool*, deliberately excluding the MCE, so two MCEs filling an InfraEnv
   of the same name collide on one id and the second gets a 409. That
   covers **concurrent** runs on one pool. It cannot cover sequential ones,
   and it cannot cover two pools that overlap.
2. **A probe for an existing BareMetalHost.** Before installing, the
   workflow reads the target namespace and skips a candidate that already
   has a host. But that read goes to the MCE's **own** API server — one
   `server-lifecycle-worker` runs inside each MCE and talks only to its own
   cluster. A machine already installed into MCE-A is therefore *invisible*
   to MCE-B's probe.

So the failing case is: run 1 installs server X into MCE-A and finishes;
run 2 targets MCE-B, draws the pool again, sees X reported `AVAILABLE`, and
its probe finds no BareMetalHost *in MCE-B*. Both clusters now hold a
BareMetalHost for one physical machine, both try to drive its BMC, and
nothing anywhere reports a conflict.

Only server-scan can close this. The installing workflows run on different
clusters and cannot see each other; server-scan is the one thing both
sides already talk to.

## Decision

A `reservation` sub-document on `Server`, taken and released through two
endpoints, honoured by `/servers/available`, and expiring on its own.

### 1. A sub-document, not a lifecycle state

`openshift.lifecycle_state` was the tempting place — an `INSTALLING` value
— and it is the wrong one, twice over.

It is a **reading**. It is derived from what the collectors observe about
cluster membership and defaults to `AVAILABLE`, so the next collection run
would reset an `INSTALLING` written here: the server genuinely is not in a
cluster yet, and an install finishes long before the 6-hour cron comes
round. Writing an intent into a field that records an observation also cuts
against ADR-0027's "unknown is not a reading".

So this follows `Maintenance` instead — an operator-set sub-document,
orthogonal to classification and health, which a server can carry while
being simultaneously `HOSTED_CLUSTER`, `CRITICAL` and in maintenance.

### 2. Ingest carries it forward

`reservation` joins `maintenance` and `openshift` in the set of fields
ingest never touches and always copies from the existing document.

This is the one carry-forward where forgetting is not a lost setting but a
correctness bug: ingest rebuilds every document each run, so a wiped
reservation hands a machine that is *mid-install* straight back to the
pool. It has its own integration test for that reason.

### 3. It expires, and the TTL is bounded at both ends

A lock with no expiry leaks a server out of the fleet on every crashed run,
permanently and silently. With one, the worst case is a machine unavailable
for the rest of the window — which is also what makes the lock safe to take
*before* the work rather than after.

`ttl_seconds` is bounded `300 .. 86_400`. A floor because a lock too short
to outlive the install it guards is worse than none: it expires mid-install
and the machine is handed out anyway. A ceiling because self-healing within
a day is the entire point.

An expired reservation is **left on the document**, not cleared. It is the
record of who last held the machine and why it went quiet, which is exactly
what is asked after an install fails. A release clears it; a new claim
overwrites it.

### 4. Mutual exclusion is `upsert_with_revision_check`

Not a new `findOneAndUpdate`. The repository already has a compare-and-set
primitive with a `RevisionConflictError`, and it is sufficient: two callers
racing for one machine read the same `revision`, both write expecting it,
and exactly one wins. The loser is told the server is taken.

Checking `is_live()` first is only an optimisation — it answers the common
case without a write. It can never be the guarantee, because anything read
before a write is stale by the time it is used.

### 5. Re-claiming is an extension, not a race

A repeat claim from the same `workflow_id` extends its own lock. Temporal
retries activities, so a claim that was not idempotent *for its own holder*
would make a retried claim look like a lost race and send the run off to a
machine it does not need. `created_at` survives the extension, so the
record still says when the machine was first taken.

Sameness keys on the **workflow id**, not the holder: the holder alone
would let two concurrent `install-server` runs extend each other's locks
and defeat the lock entirely.

### 6. The reservation names the MCE, and reaches the inventory row

`mce_cluster` is required on a claim, and is projected into
`GET /servers/rows` alongside `holder`, `infra_env` and `expires_at`.

This is not bookkeeping. Two MCEs drawing from one InfraEnv pool is the
case the lock exists for, so "reserved" that does not say *which cluster*
answers half the question. Putting it in the row rather than only on the
detail page is deliberate too: "which MCE is installing this?" is a
question asked **of the fleet** — scanning for the machine that is stuck —
so the fleet list must answer it without opening each server. With
`workflow_id` on the detail, a held row traces back to the exact run.

`held` is computed against the request's clock rather than stored, so an
expired lock reads as free everywhere at once; a row still claiming an
install days later would send someone looking for a run that ended.

In the UI that lands as a badge **under** the Installation badge, never
replacing it — `openshift.lifecycle_state` is a reading, and a server
mid-install is still genuinely `AVAILABLE` to the collectors, so
overwriting it would make an intent look like an observation. The badge
renders nothing when no lock is held, so the uncommon state costs the
other rows no height. It carries the cluster; holder, InfraEnv and expiry
live on the hover, where width is free. Alongside it: a `reserved` filter
so "what is installing now" is one click rather than a scan, the target
MCE added to row search so searching a cluster finds machines *heading*
there and not only those already in it, an `Installing to` CSV column
kept separate from `MCE` (where a server already lives), and the same
badge spelled out in full on the detail page.

### 7. The draw excludes live reservations with `$nor`, not `$or`

`unreserved_filters()` is merged into a filter that **already carries an
`$or`**: `_name_or_alias_filter` uses one to match a capacity alias
alongside the pattern. An `$or` here silently overwrites that on merge and
drops the *name* restriction — widening a draw from one InfraEnv's pool to
the entire fleet.

That is not hypothetical. The first implementation did exactly that, and it
was caught by two capacity-alias integration tests rather than by review.

Two BSON details are handled explicitly:

- **null sorts below every date**, so a bare `expires_at <= now` matches a
  document whose expiry is `null` — a lock meant to be held indefinitely
  would read as long expired, which is the one mistake that hands a machine
  to two clusters. Hence `holder: {"$ne": None}` gates both branches.
- **a missing field is null**, so a document written before this field
  existed matches neither branch and counts as free, which it is.

`server_still_qualifies()` gains the matching check, as ADR-0032 requires
of every clause in the draw: the predicate applied after a live recheck
must admit exactly what the Mongo filter returned.

## Consequences

**The InfraEnv-wide 409 can be relaxed.** `install-server`'s workflow id
serialises installs from one pool *because* nothing reserved a machine.
With a reservation, the id can become `install-server-<infraEnv>-<mce>` —
the pair it was originally keyed on — and two MCEs can fill the same
InfraEnv concurrently from different machines. That is a change in the
workflows repo, not here, and it is now unblocked.

**A draw can come back empty for a new reason**, so the 404 message names
it: every match may be reserved by an install in progress rather than
unhealthy or claimed.

**Pool depth still rules.** The lock guarantees two MCEs get *different*
servers, never that both get one. With one qualifying candidate, one wins
and the other exhausts its draw and fails — honestly, instead of
double-booking.

**Releasing is not required for correctness**, only for promptness. A run
that dies leaves a lock that expires. A run that tears a candidate down
should release it so the machine returns immediately, and a release with no
`holder` is the operator override for a lock whose run is gone.

**What this does not do.** It does not reserve at draw time. `/available`
still returns candidates without locking them, because the endpoint does
not know which candidate a caller will choose and locking all of them would
withhold `count - 1` machines from everyone else on every call. The claim
belongs to the caller, after it has chosen.
