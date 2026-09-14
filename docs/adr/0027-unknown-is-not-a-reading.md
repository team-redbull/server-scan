# ADR-0027: a value the collector could not read is never a health verdict

Date: 2026-09-12
Status: Accepted

Extends `docs/adr/0005-policy-key-shadowing.md`, which is the health
engine this constrains, and sits beside the same rule at the ingest layer:
`Server.unread_fields` and `Server.reachable` both exist so a field that
was not read is not mistaken for a field that was read as zero.

## Context

`network.all_links_down` is a system-default policy: CRITICAL when a
server reports interfaces and none of them is up.

```python
all_of=[
    Condition(metric="network.interface_count", operator="GTE", value=1),
    Condition(metric="network.links_up_count",  operator="EQ",  value=0),
]
```

`links_up_count` counts interfaces whose `link_state` is `UP`.
`interface_count` counts every interface. The gap between them is the
bug: `LinkState` has four members, and `UNKNOWN` is one of them.

**UCS Manager and Intersight report `UNKNOWN` on ~99.75% of real vNICs**
— that figure is measured, and ADR-0009's 2026-09-07 field pass explains
why it is not a defect: `AdaptorHostEthIf.oper_state` is a generic
equipment-operability enum, not a link-state one, so there is no reading
for this collector to get. Cisco collectors have populated
`ProviderServer.nics` since 2026-09-10.

So from that date, essentially every Cisco server in the fleet satisfied
both halves — interfaces reported, none of them `UP` — and read
**CRITICAL on network**, for hardware that was fine. The same arithmetic
put `network.single_link_up` at MAJOR for a Cisco server with one real
uplink and any number of unreadable vNICs beside it.

The engine had no way to express the difference. `evaluate_leaf` is
two-valued: a condition holds or it does not. An unreadable field
therefore had to resolve to *something*, and "not UP" is what a count of
UP-states resolves it to.

### What an audit of the other facts found

Every other health fact in `extract_facts` already counts only definite
readings, and each one has a comment saying so:

| fact | counts | UNKNOWN |
|---|---|---|
| `storage.failed_drive_count` | `CRITICAL` | excluded |
| `storage.warning_drive_count` | `WARNING` | excluded |
| `storage.os_bad_disk_count` / `data_bad_disk_count` | `_NOT_GOOD` | excluded |
| `memory.degraded_dimm_count` | `_NOT_GOOD` | excluded |
| `power.failed_psu_count` | `DOWN` | excluded |
| `gpu.failed_count` | `_FAILED` | excluded |
| `connectivity.fabric_paths_down` | provider-side `DOWN` | excluded |

Network was the one place the comparison ran the other way — a count of
*good* readings against a denominator of *all* interfaces — and that
inversion is what made UNKNOWN act as evidence of failure.

## Decision

**A health policy compares against what was read, never against what was
attempted.** Concretely:

1. A new fact, `network.links_known_count`: interfaces whose link state
   is anything other than `UNKNOWN`. It is the denominator both link
   policies now use.
2. `network.interface_count` keeps its honest meaning — every interface
   reported — and stays registered. Both policies carry it as
   `reported` evidence, so a server showing "no link up across 2 of 8
   interfaces with a readable link state" tells an operator both numbers.
3. Every other fact keeps the exclusion it already had.
   `tests/unit/domain/services/test_health_defaults_coverage.py`'s
   `TestUnknownIsNotAVerdict` pins it, so a future fact that counts
   UNKNOWN as bad fails the suite rather than shipping.

`DISABLED` is deliberately **not** excluded. It is a real reading — the
port is administratively down — and a server whose every link is disabled
genuinely has no network. Only `UNKNOWN` means "nothing to judge".

## Alternatives rejected

**Three-valued condition evaluation.** Make `evaluate_leaf` return
`True`/`False`/`UNKNOWN`, propagate it through `all_of`/`any_of`/`not`,
and skip any policy whose tree resolves to `UNKNOWN`. This is the general
form of the rule and would enforce it for any policy anyone writes later.

Rejected for now because it changes the engine's core contract —
`evaluate_condition`, `evaluate_health`, the category rollup and every
fact resolver — to produce, today, exactly the behaviour the fact-level
fix above already produces. The audit table is the reason: nothing else
in the registry can currently resolve to "unknown" at all, because every
resolver returns a count of definite readings. The general mechanism
would have one user.

It becomes the right answer the moment a metric is added whose value can
itself be unknown — an ENUM resolving to a raw vendor string, say. This
ADR is where to start when that happens.

**Dropping UNKNOWN interfaces from `network.interface_count` itself.**
One fewer metric, but it makes a registered metric quietly mean something
other than its name and description, and `interface_count` is the number
an operator wants in the evidence line.

## Consequences

- A Cisco server whose vNICs report no link state is no longer CRITICAL
  on network. Measured against the seeded fleet: facts
  `interface_count: 4, links_known_count: 0, links_up_count: 0`, the old
  `all_links_down` condition evaluates `True`, the new one `False`. The
  category lands on `HEALTHY` with `evaluated_count: 2, active_count: 0` —
  both link policies still ran, neither found anything, which is the
  engine's existing "evaluated, nothing active" reading and the same one a
  server with no drives gets for storage.
- Stored health does not change until each server's next collection or an
  explicit re-evaluation; the health engine runs at ingest.
- The two policies' definitions changed, so
  `ensure_default_health_policies` re-syncs them on the next API startup
  (drift re-sync, `app.application.services.bootstrap`). No migration.
- A real all-links-down server is still CRITICAL, and one real uplink
  beside one real dead link is still MAJOR. Both are pinned by test.

## Seeded data

The fake provider needed its own change, and the reason is convention 10's
exact failure mode. Before this ADR, seeded Cisco servers tripped both link
policies by accident, which made the checks *look* exercised in every dev
environment, demo and screenshot while nothing real drove them. Fixing the
engine would have left them firing for nothing at all.

So `generator._link_states` gives a deliberate minority real faults: 3%
all-down (CRITICAL) and 4% one-up-rest-down (MAJOR). Measured at
`--count 1000 --seed 42`: 22 CRITICAL, 17 MAJOR, 324 Cisco servers
correctly unjudged, 637 healthy.

Two constraints shaped it:

- **Only collectors that read a link state may carry the fault.** UCS and
  Intersight stay all-UNKNOWN — faking a reading onto them would
  contradict ADR-0009 and re-hide the very case this ADR is about.
- **The fault is keyed off the server's first MAC, not `rng`.** Every
  other defect here is a `rng.random() < share` draw, and that is normally
  right; here it is not. An `rng` call added or removed in this path
  shifts every later draw, so the whole fleet's vendor/site/classification
  mix moves for a given seed and README's quoted figures go stale. `crc32`
  rather than `hash()`, because `hash()` on a `str` is salted per process
  and reproduces nothing.

## Update (2026-09-14): UCS Central gets a real link-state fault too

**"UCS ... stays all-UNKNOWN" above no longer holds — Intersight still
does, UCS Central does not.** `ucs_manager.mapping` now reads a vNIC's
`operability` property instead of `oper_state` for `link_state`
(docs/cisco-collectors.md, "`operability` — a second vNIC signal
ADR-0009 did not check") — a real, populated signal this ADR did not
know existed when it decided UCS could never carry the fault honestly.
`fake.generator._nics_for` was updated the same way: `UCS_CENTRAL` now
draws from `_link_states` like every other collector that reads a real
link state; `INTERSIGHT` is unchanged, still hardcoded `UNKNOWN`, since
no equivalent field has been researched for it. This does not reopen the
false-CRITICAL bug this ADR fixed — `network.links_known_count` still
gates both policies, and a mostly-`operable` fleet simply now has a
non-zero denominator instead of an always-zero one.
