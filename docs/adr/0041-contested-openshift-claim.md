# ADR-0041: a server two OpenShift jobs keep flipping is flagged as contested

Status: accepted (2026-10-05)

## Context

`Server.openshift` is single-membership, last writer wins, and ADR-0024's
reconcile frees only servers naming its own cluster or MCE. A reinstall that
leaves a stale artifact (2026-10-04: a Dell moved from a hosted cluster into
`ocp4-five`, its old unbound Agent CR still on the MCE) makes two jobs claim
one serial. Each is right for its own scope, so neither frees the other and
the server flips every 15 minutes, with two audit events each time.

## Decision

Detect, do not arbitrate. A *claim* is the reporting job plus the cluster it puts
the server in: a UPI or hosted cluster's nodes job is `cluster`, an MCE's unbound
agent is `mce`, its agent bound to a hosted cluster is `mce/cluster`, so two Agent
CRs on one MCE are two claims. Two claims naming the same cluster agree (a hosted
cluster reported by its own job and by its MCE) and are one claimant.

`_apply` records the claim that held the server before (`previous_reporter`,
`claim_changed_at`). When the writer is the claim that held it before the current
one, within one hour (`_CONTEST_WINDOW`), the server is `contested_with` that claim
and `contested_name` holds the hostname the other claim knows the server by (its own name when it
reports no different one).
A, B, A is a contest; A, B is a move and stays silent, for every pairing of UPI,
MCE and hosted claims (`TestEveryPairingOfClaimants`). The flag clears on the next
report after an hour without flipping, and a freed server forgets its history.
From a fresh server the first flip back shows on the second run, so a two-Agent
duplicate on one MCE is flagged 15 minutes after it first appears.

Surfaced three ways (plus a Grafana stat panel and the recording rule `server_scan:openshift_contested_servers:max`): a structured `openshift.contested_claim` log naming both
claimants, the gauge `server_scan_openshift_contested_servers`, and
`contested_with` on `ServerRow`, which the inventory shows as a "Contested with"
chip and a **Duplicate Server** filter (the server's Overview names the other claim and its hostname) (`?contested=true`), beside **Duplicate
Name** (`?duplicate=true`, the existing same-name check).

## Update 2026-11-01: an Agent is identified by its UID

A bound and an unbound Agent have different claim keys (`mce/X`, `mce`), so one Agent CR
unbinding and rebinding within an hour (a retry, a re-create) read as A, B, A and was flagged
against its own MCE. `agent_observation` now reads `metadata.uid` into `agent_uid`
(`OpenShiftLifecycle.agent_uid`, `previous_agent_uid`; both default `None`). When both sides
carry a UID, the same UID is one claimant whatever it is bound to, and two UIDs are two
claimants even on the same cluster. A node job has no UID and a value stored before this
change has none either: both fall back to the claim-key rule above. The first Agent write on
an old value keeps no claim history (it cannot say which Agent made it), so the flag clears
once. Hosted clusters have no nodes job here (the MCE's Agents are the only source), so a
node lingering after an unbind is not a case this covers.

A run also writes a server once, however many claims reported it: two Agent CRs for one
serial (a stale unbound one beside the live bound one) used to each write, flipping
`INSTALLED_TO_INVENTORY` and `INSTALLED` and two audit events every 15 minutes. The claims are
chained in memory (bound claim last, so the stored state is `INSTALLED`) and the result is
compared with the stored value (`_apply_all`).

## Consequences

No precedence rule: nothing decides who is right, so the flip-flop continues until
an operator deletes the stale Agent CR or node. A genuine move back to the
previous cluster within an hour is flagged once. The new fields default to `None`,
so stored documents decode unchanged.
