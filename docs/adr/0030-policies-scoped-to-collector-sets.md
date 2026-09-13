# ADR-0030: a health policy is scoped to a set of collectors, and the page groups by scope

Date: 2026-09-13
Status: Accepted

Extends ADR-0005's `policy_key` families and `PolicyScope`. Applies the
stored-shape rule from ADR-0026.

## Context

Every one of the sixteen system-default health policies shipped unscoped,
including the two that only make sense behind a fabric interconnect —
`connectivity.fabric_paths_down_warning` and `_critical`. They never fired
on a Dell, HPE or standalone server, because the fact they read is zero
there, but the page listed them next to "GPU failed" as if a OneView
server could ever have a fabric path down. The operator's request was to
make the page honest about *which servers a policy is for*, and to order
what it shows: general policies first, then per-vendor ones, and within
each group the CRITICAL ones before MAJOR before WARNING.

Doing that exposed two things the model could not express.

**A scope could name one manager type, and "FI-managed Cisco" is two.**
`PolicyScope.manager_type` was a single `str | None`. The fabric policies
belong to servers a fabric interconnect owns — those collected by
`UCS_CENTRAL` and by `INTERSIGHT` — and explicitly not to a Cisco box
reached at its own BMC through `REDFISH_STANDALONE`. `vendor="cisco"`
would include that box; one manager type would cover half the fleet;
two copies of each policy would double the rows and split the
`policy_key` family the whole design rests on.

**A manager-scoped policy has never matched any server.** `Server` has no
`manager_type` field, and all three evaluation call sites passed
`manager_type=None` with a comment calling it a known gap. Scoping the
fabric policies without closing that gap would have switched them off
silently — the exact failure ADR-0027 was written about, from the other
direction.

## Decision

1. **`PolicyScope.manager_types: list[str]`** replaces `manager_type`.
   Empty means no restriction; non-empty means the server's collector must
   be one of the listed `ManagerType` values. Specificity is unchanged —
   a set still scores 2 — so ADR-0005's precedence is untouched.
   `RuleScope` (classification) is deliberately not changed: nothing asked
   for it, and a one-sided rename is cheaper to reason about than a
   speculative symmetric one.
2. **`Server.source_provider` is the manager type at evaluation.** It is
   already the collector's `ManagerType` value (`tools.run_collector` and
   the seeder both write it), so ingest, the policy service and the
   re-evaluate endpoint now pass it instead of `None`. The "known gap"
   comments are gone because the gap is.
3. **The fabric policies are scoped to `[UCS_CENTRAL, INTERSIGHT]`.**
   Every other default stays general. `bootstrap` re-syncs a system
   policy's definition on startup, so a deployed database picks the scope
   up without a migration.
4. **The read-only page groups by scope and sorts by severity.** "General"
   first, then one section per distinct scope labelled in words
   ("Cisco — UCS Central, Intersight"), and inside each section CRITICAL,
   MAJOR, WARNING, then name — the same `HEALTH_SEVERITY_RANK` order the
   engine uses, not a second one.

## Stored shape

Existing documents carry `scope.manager_type: null` (every default) or a
string (a custom policy). A `mode="before"` validator maps the old key
onto the new list — `null` → `[]`, `"X"` → `["X"]` — and an integration
test writes the old shape into a real database and reads it back, per
ADR-0026. No index changes: the health-policy collection never had an
index on `scope.manager_type` (the `scope_manager_type` index in
`indexes.py` belongs to classification rules), and nothing queries on a
policy's scope — policies are loaded whole and matched in Python.

## Consequences

- The two network-link policies stay general, Cisco included. That was
  a considered choice, not an oversight: ADR-0027 already makes them
  count only links whose state is known, so UCS's `UNKNOWN` vNICs cannot
  false-fire them.
- The fake provider only builds fabric attachments for a Cisco server
  whose collector is `UCS_CENTRAL` or `INTERSIGHT`; a seeded standalone
  Cisco box no longer carries fabric paths it could not have (CLAUDE.md
  convention 10).
- A manager-scoped *classification rule* now also matches during ingest,
  for the first time, because the same `manager_type` value reaches
  `classify`. No system default uses one, so nothing observable changes
  on a stock deployment.
