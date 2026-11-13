# ADR-0062: The live recheck leaves the draw — a caller locks one server, then asks for its check

**Status:** Proposed 2026-11-13 with the operator, not implemented. Amends ADR-0032 (where the live recheck
runs) and settles the item CLAUDE.md parked "while bmhgen's replacement (a Temporal flow or similar) is
decided": a concurrency cap on live rechecks. The caller's side is team-redbull/workflows'
`docs/design/add-server-to-mce-lock-then-recheck.md`.

## Context

`GET /servers/available` has one caller: the workflows repo's add-server-to-mce, the Temporal workflow that
replaced bmh-generator-operator. Each run installs ONE server. It asks
`?pattern=^ocp-<infraEnv>&count=<candidate_count>&health=HEALTHY`, then tries the candidates in order:
local checks, the install lock (ADR-0035), the BareMetalHost, an hour's wait for the Agent, and the next
candidate if that fails.

**Observed (2026-11-12):** two requests for 10 installs each left all 20 runs at `acquiring-server`,
indefinitely. What makes that happen, each from code:

1. **Every drawn candidate is live-rechecked, sequentially, before the answer.** `_fill_from_tiers` awaits
   `_recheck(candidate)` in a plain `for` loop and replaces a rejected candidate for up to
   `_MAX_REPLACEMENT_ROUNDS_PER_TIER` (3) rounds, so `count=N` costs N to 3N vendor `get_one()` calls in one
   request. The caller then uses the first one that works; every other check was spent on a server it never
   touched.
2. **One recheck can outlast the caller.** The recheck waits up to the manager's own per-call timeout
   (`recheck.timeouts`: 240 s for `UCS_CENTRAL`, 15 s default; ADR-0032's 2026-10-11 update). The caller's
   HTTP timeout is 60 s under a 90 s activity, retried without limit (workflows
   `activities/server_lifecycle/server_scan.py`, `add_server_to_mce.py`'s `_RETRY_POLICY`). A draw that does
   not answer in 60 s is asked again, from scratch, forever.
3. **The caller sized its draw for a sorted list; the draw is random.** The workflow's default
   `candidate_count` was `busy runs + new runs + 2` (12 and 20 in the incident), on the stated premise that
   every run "takes one machine from the same top of the list". The draw is `$match` then `$sample`
   (`sample_available_tier`, ADR-0032 §3), so runs mostly draw different servers and the spares bought little,
   while multiplying the checks of point 1: about 320 sequential vendor checks inside 20 concurrent requests.
4. **The check runs before the lock.** Two runs can check the same server, and a server checked healthy can be
   locked by another run before this one locks it, which wastes the check.

The vendor-side cost is the risk CLAUDE.md already names: a retry-looping caller can use up a manager's
session cap and fail the 06:00 collector login.

## Decision

### 1. `GET /servers/available?recheck=false` answers from Mongo alone

The same filters, tiers, random draw and 404 reasons, with no `get_one()` and therefore no replacement rounds
(they exist only to replace a candidate a recheck rejected). Every item says `live_recheck_performed: false`.
Applies to `?name=` as well. The default stays `true`, so the current caller is unchanged until it moves.

### 2. `POST /servers/{server_id}/recheck` checks one server

The live check a draw runs today (`_recheck`, then `server_still_qualifies`), as its own request. Admin only:
it writes through `IngestService.ingest_one`, as the draw's recheck does.

- **Body:** `{"workflow_id": str | null, "health": HealthSeverity | null}` (`health` defaults to `HEALTHY`, the
  tier the caller asks the draw for). A live reservation whose `workflow_id` is the one sent is **not** a
  reason: the caller checks a server it has just locked, and without this `server_still_qualifies` would
  reject the caller's own lock.
- **200 is a verdict:** `{"qualifies": bool, "live_recheck_performed": bool, "reasons": [{"code", "text"}],
  "server": AvailableServerItem}`. `qualifies: false` is an answer, not an error. The reason codes are the
  ones `unassignable_reasons` already produces, plus `GONE` when the manager no longer has the server.
  `server` is the fresh document as `/servers/available` would return it, so the caller builds its
  BareMetalHost from what was just read.
- **404** for an unknown id. **503** for a vendor failure (`VendorRecheckError`, as a draw answers today).
  **429** with `Retry-After` when the manager type's slots are full (decision 3).
- An unconfigured manager type, or a server another manager collects, degrades exactly as the draw does:
  the verdict is on the stored document and `live_recheck_performed` is `false`.

### 3. A per-manager-type cap on live rechecks

`INVENTORY_RECHECK_MAX_CONCURRENT` (`TYPE:n,...`; Helm `recheck.maxConcurrent`, default 4 per type) bounds
the `get_one()` calls in flight per API pod, for this endpoint and for a draw with `recheck=true`. A request
that finds no free slot answers **429 + `Retry-After`** rather than queueing, because a queued request runs out
the caller's HTTP timeout while it waits. A `rate_limited_total{manager_type}` counter counts them, reusing the
existing `RateLimitedError`. The manager sees at most pods × cap rechecks at once; keep that below its session
limit minus what the collector needs.

### 4. The caller's order: lock first, then check

Recorded here because it is why decision 2 takes `workflow_id`: draw with `recheck=false`, run the caller's
own checks, take the lock (`POST /servers/{id}/reservation`), then `POST /servers/{id}/recheck`. On a negative
verdict or a vendor failure the caller releases its lock and draws its next server, one at a time, up to its
number of tries: a draw is a Mongo query now, so drawing spares ahead of time saves nothing. A check is spent
only on a server the caller holds: two runs never check one machine, and nothing can take it between the
check and the lock.

## Consequences

- **A run makes about one vendor call instead of 3 to 3N**, each answered within one manager's timeout. The
  20-run incident becomes 20 checks, at most 4 per manager type per pod at once.
- **The draw's answer is stored state**, up to one collection cycle old. Acceptable: nothing is installed
  before the live check on the locked server.
- **Two requests per candidate** (lock, check) instead of one draw for all. Cheap next to a vendor call.
- `?recheck=true` and its replacement rounds stay while any caller or in-flight run uses them; flipping the
  default is a later decision.
- **Not done:** parallel rechecks inside one draw. Once the caller draws with `recheck=false`, they no longer
  help.

## When it is built

The route and service method; the cap's setting, `.env.example` and Helm `recheck.maxConcurrent`;
`.claude/skills/server-scan-api/` (`SKILL.md`, `references/endpoints.md`) for the new parameter and endpoint
(convention 11); a dated pointer in ADR-0032; `docs/architecture.md`'s available-servers section; this ADR's
status and its `docs/arc42.md` row. Tests: `recheck=false` never calls `get_one()`; the caller's own lock is
not a reason and another holder's is; the 429 at the cap.
