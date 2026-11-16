# ADR-0063: `POST /servers/available` locks a server, live-checks it while locked, and returns it

**Status:** Accepted, implemented 2026-11-16. Supersedes ADR-0032's decision 3 ("no reservation"; the
2026-09-26 update already said it needed its own ADR, which was ADR-0035) for the callers that move to
the POST; extends ADR-0035.

## Context

`GET /servers/available` (ADR-0032) ranks candidates in Mongo, live-rechecks the few it returns and hands
them out **without reserving them**. Two callers asking for the same pattern at the same moment could be
given the same server; the workflow then took the lock in a second call (`POST /servers/{id}/reservation`,
ADR-0035), after the draw. The recheck also made a read endpoint write (`ingest_one`), and `health` could only
name one exact tier, so "anything up to Major" was not expressible.

## Decision

1. **`max_health` replaces `health`**, on both verbs. It is the worst acceptable tier and allows every better
   one: `HEALTHY` = Healthy; `WARNING` = Healthy, Warning; `MAJOR` = up to Major (the default, as before);
   `CRITICAL` = all four. `UNKNOWN` is in none (ADR-0027) and is a 400. `health` is **refused with a 400**,
   not ignored: a caller still sending it would otherwise be silently widened to the default. The network
   category must still be Healthy at every setting, so a Critical server qualifies only when its network
   read passes.
2. **`GET` is stored-document only.** It ranks and draws from Mongo, performs no live recheck and takes no
   lock; `live_recheck_performed` is `false`. `count` and the list shape are unchanged.
3. **`POST /servers/available` takes the server.** Admin-only (a write). Body: exactly one of `name`/`pattern`,
   `max_health`, `vendor`, `source_provider`, `min_nic_macs`, and the reservation fields `holder`,
   `mce_cluster` (both required), `workflow_id`, `infra_env`, `namespace`, `ttl_seconds` (300..86400, default
   7200). No `count`: unknown fields are a 422. It returns the GET envelope with exactly one item.
4. **Lock first, check while locked, release on a miss.** One candidate at a time, best tier first:
   claim it (the `revision` compare-and-set of ADR-0035 decision 4), run the live recheck (which also persists
   the fresh state), and judge the result against `max_health`. A pass extends the lock to the caller's TTL and
   returns it. A fail (degraded past `max_health`, unreachable, in maintenance, gone from its manager, vendor
   error) **releases the lock before the next candidate is drawn**, so a request never holds two. A candidate
   another request locked first costs no attempt (bounded at 20 races). Candidates are tried at most
   `INVENTORY_AVAILABLE_MAX_ATTEMPTS` (default 5) times per request; this replaces ADR-0032's three
   replacement rounds per tier.
5. **A probe lease guards the check.** The claim first holds the server for `max(120 s, longest recheck + 60 s)`
   (`INVENTORY_AVAILABLE_RECHECK_TIMEOUTS`: UCS Central 240 s; OpenManage and standalone Redfish use
   `INVENTORY_REDFISH_HOST_BUDGET_SECONDS`, 180 s, because their `get_one` is many sequential calls), so a process that dies mid-check frees the
   server in minutes, not after the caller's whole TTL; the lease must outlive the vendor call or another
   request could draw the server during its own check. On a pass it becomes `ttl_seconds`.
6. **Every "nothing reserved" is a 503 `NO_AVAILABLE_SERVER`** whatever the cause (nothing at that health, all
   installed or in maintenance, all locked, every candidate failing its check, no name matching), with
   `details.reasons` (`INSTALLED`, `CLAIMED`, `MAINTENANCE`, `UNREACHABLE`, `RESERVED`, `HEALTH`, `NETWORK`,
   `NIC_MACS`, `VENDOR_ERROR`, `GONE_FROM_MANAGER`, `RESERVE_FAILED`) and a `detail` naming each candidate's
   failure. A bad request stays a 4xx (400 neither/both of `name`/`pattern` or `max_health=UNKNOWN`; 409 an
   ambiguous `name`; 422 an unknown field).
7. **Audit and cache.** Only the final lock is audited (`SERVER_RESERVED`, `data.via = "available"`); a rejected
   candidate's claim and release write no event, so a walk past several Critical servers leaves no noise.
   The list cache is invalidated once per request.

## Consequences

- **Not idempotent by `workflow_id`.** One workflow may legitimately take several servers, so a repeated call
  takes another. A server whose caller never used it frees itself at its TTL, or the workflow releases it with
  `DELETE /servers/{id}/reservation`.
- **Breaking for GET callers** that send `health` (400) or rely on the live recheck; the Temporal workflows move
  to the POST.
- **Vendor load is unbounded per manager**: parallel requests run parallel rechecks. The per-`ManagerType`
  concurrency cap (CLAUDE.md, "Explicitly NOT done yet") stays parked; a request's own work is bounded by the
  attempt cap.
- **A crash between claim and release** hides a server for at most the probe lease.

## Hardening after review (2026-11-16)

A Codex review of the first version found five holes, all closed here:

- **Ownership is a per-request token** (`Reservation.claim_token`, optional, so old documents load): holder and
  workflow id are caller-supplied and may repeat, so a second request with the same fields could adopt, return or
  release the first one's lock.
- **The live check has its own deadline** (probe lease minus half the slack), so a provider that makes several
  sequential calls (UCS) can never outlive the lock it runs under; an overrun is a `VENDOR_ERROR` candidate.
- **The claim write is inside the cleanup** (`try`/`finally`), so a cancellation after Mongo applied it still releases.
- **A failed release ends the request** (`RELEASE_FAILED`, 503), so one request never holds two servers.
- **The final extension re-reads the document and re-applies `server_still_qualifies`** (`still_ok`), so a
  maintenance or membership write after the live check is not returned; an ingest `ConflictError` in the recheck
  fails that candidate instead of the request.
