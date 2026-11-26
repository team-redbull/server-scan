# ADR-0071: A 5-minute sweep asks the renamer to rename free servers to the fleet convention

**Status:** Accepted, implemented 2026-11-26 (report-only by default).

## Context

Free servers are to be named `ocp-c<conf_id><marker>-mtl-<site>-<serial>` (e.g. `ocp-c200-mtl-tlv-4b184et`).
A separate renamer service (FastAPI, HTTP Basic with an AD service account) already does the renaming: it
reads a server's live hardware, matches it against the hardware confs, builds the name and renames the Dell
OpenManage profile (by iDRAC IP) or the Cisco UCS Central service profile (by profile name). Server Scan knows
which servers are free, so it decides *who* is sent; it never builds a name.

## Decision

- **`tools.rename_fleet`, CronJob `<release>-rename-fleet` in the `server-scan` chart, every 5 minutes**
  (`renamer.*`, operator's choice: like `nodes-status`, not nightly). `concurrencyPolicy: Forbid`,
  `backoffLimit: 0` (the next run is the retry), triggerable from the Jobs page.
- **Candidates:** `identity.vendor` dell or cisco, `openshift.lifecycle_state` `AVAILABLE`, not in
  maintenance, `reachable`, and then in Python (each skip is counted): name not containing an excluded
  string (`renamer.job.skipNameContains`, default `-mce-,-seed-`, any case: MCE hubs and seed hosts keep their
  names, operator's call 2026-11-26), name not already matching
  `^ocp-c[0-9]+(\.[0-9]+)?[dn]?-mtl-[a-z0-9]+-[a-z0-9]+$` (`app.domain.value_objects.fleet_name`, kept in
  sync with the renamer), collected by a collector the renamer manages (Dell `OPENMANAGE`, Cisco
  `UCS_CENTRAL`/`UCS_MANAGER`; Intersight and Redfish standalone would only 404), no live reservation (a
  server drawn by `POST /servers/available` is never renamed mid-provision), Dell with an IP-shaped BMC host.
- **Batch routes, chunked** (`chunkSize`, default 5 per request, so a request stays under the renamer's
  300 s route limit; a Cisco rename takes up to ~40 s), **at most `maxPerRun` servers per run** (default 50,
  the blast radius of one run; the rest wait for the next run 5 minutes later). No new chunk starts after
  `runBudgetSeconds` (540) so a run ends inside `activeDeadlineSeconds` (900).
- **Each chunk is re-read just before it is sent** (still free, reachable, out of maintenance, unreserved, and not
  renamed since to an excluded or in-convention name);
  a server that changed since selection is dropped. A reservation taken *during* the chunk's request is not
  seen: the window is one request, not the whole run.
- **Each chunk is audited as soon as it returns**, so a run killed later keeps the earlier answers' cooldown.
- **Report-only until `renamer.job.reportOnly: false`**: the renamer's `dry_run: true`.
- **One `RENAME_REQUESTED` audit event per answered server** (outcome `renamed|unchanged|would_rename|conf_mismatch|failed`,
  `name_from`, `name_to`, `conf_id`, `variant`, `error`, `nearest_confs`, `dry_run`). Not `SERVER_RENAMED`:
  that one is the collector observing the new name, which follows on its next run.
- **The audit trail is the cooldown ledger** (as ADR-0050's iDRAC reset): a server with a `RENAME_REQUESTED`
  newer than `cooldownHours` (default 24) is not sent. Without it, a renamed server would be sent again every
  5 minutes until the next collector run shows its new name, and a conf mismatch would write 288 events a day.
- **A request that fails as a whole** (connect error after 2 retries, non-200, unreadable body) writes no
  event, and neither does a server the 200 answer leaves out: those are retried next run. A 4xx is never
  retried within the run. **A read or write timeout is `failed`, never re-sent** (the renamer may already have
  renamed it), and so is a server the answer names twice: both wait for the cooldown and a human.
- The job sets `INVENTORY_AUTH_ENABLED=false`: it never logs anyone in, so no AD settings reach its pod.
- Exit 0 done or no URL; 1 MongoDB failed; 3 some server failed or was not answered.

## Consequences

- A renamed server shows its new name after the next collector run (`SERVER_RENAMED`), not at once.
- A conf mismatch is re-asked once per cooldown until a human adds a conf or fixes the server.
- The renamer's batch response is matched by `idrac_ip` (also carrying the Cisco profile name, as its request
  field does); an identity missing from the response is counted `failed`.
- `renamer.verifyTls: false` turns certificate checks off; `caPem` (pasted into values, rendered into
  `<release>-renamer-ca`) or `caConfigMap` (an existing one, which wins) trusts a private CA instead.
- **Do not roll the API back past this release once the job has run** (report-only included): an older API
  cannot decode `RENAME_REQUESTED` events, so the events page and History tab would fail on them.
