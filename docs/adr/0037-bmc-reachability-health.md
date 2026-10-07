# ADR-0037: BMC reachability is a health category, probed with an unauthenticated Redfish GET

Date: 2026-09-30
Status: Accepted

Extends ADR-0016, ADR-0020, ADR-0022, ADR-0027 and ADR-0029. Supersedes nothing.

## Context

`Server.reachable` / `unreachable_since` exist, but only `OPENMANAGE` ever
writes `reachable=False`, and neither field feeds `health.overall`: a server
whose BMC has been dead for a week can read HEALTHY. Three collectors need
attention; the two Cisco collectors do not (UCS Central/Manager and
Intersight reach servers through fabric interconnects or the cloud
appliance, which already report their own state).

- **`REDFISH_STANDALONE`** talks to each BMC directly and already tells the
  failure modes apart (`RedfishAuthError`, `RedfishTlsError`,
  `RedfishUnreachableError`, timeout), then throws the distinction away: a
  failed host is logged and yields no `ProviderServer`, so it never appears
  in the UI.
- **`OPENMANAGE`** yields a `reachable=False` stub for a dead or
  auth-rejected iDRAC but records no reason.
- **`ONEVIEW`** never touches iLO. The iLO address is in the payload
  (`mpHostInfo.mpIpAddresses` -> `bmc_address_raw`) but nothing contacts it.

Separately, nothing in the platform ever deletes a server that a manager
stops listing. `IngestService.ingest` is an upsert loop; the only signal is
`last_seen_at` ageing into the Stale chip (ADR-0029).

## Decision

### 1. `unreachable_reason`, a closed vocabulary

A new field beside `reachable`/`unreachable_since`, set whenever
`reachable=False` and cleared with it: `network_unreachable`,
`auth_rejected`, `tls_error`, `timeout`, `protocol_error`.

### 2. A `bmc` health category

`bmc` joins `evaluate.CATEGORIES` (after `gpu`) with a `bmc.has_data` fact
and a `bmc.reachable` fact, and one default policy: **CRITICAL, global
scope**. Overall health is already the worst category (`evaluate.py`), so an
unreachable BMC makes the server CRITICAL with no special case. A server that
was never probed (UCS, Intersight) has no `bmc.has_data`, so the category is
UNKNOWN and skipped, per ADR-0027. The existing `connectivity` category is
UCS-fabric-paths-only and Cisco-scoped, so it is not reused. The UI's health
breakdown gets a `BMC` row after GPU.

### 3. The probe: one unauthenticated `GET /redfish/v1`

For `ONEVIEW` only (the other two already make a stronger authenticated
read), a wrapper provider - the `_NameFilteredProvider` pattern - probes each
yielded server's BMC host after the provider yields it.

- **`GET /redfish/v1`, not TCP connect and not ICMP.** The Redfish service
  root is defined to be readable without authentication (DMTF DSP0266), and
  the standalone collector already starts every host with this exact call
  (ADR-0016). A TCP connect only proves a port is open; a BMC whose web
  service has hung still accepts the handshake. ICMP would need `ping`
  subprocesses (one fork per host) or `CAP_NET_RAW`, which OpenShift's
  default SCC does not grant. iLO 4 serves `/redfish/v1` (HPE's iLO 4 REST
  API reference), and the rule below tolerates firmware that does not.
- **One shared `httpx.AsyncClient`**, `verify=False`, a short timeout, no
  retries, bounded `max_connections` and a semaphore. Everything here already
  runs on one event loop with httpx; no thread is created per probe. Pooling
  gains nothing across distinct hosts, so the shared client is about bounded
  concurrency, not connection reuse.
- **Classification.** *Any* HTTP response - 200, 401, 404, a redirect -
  means `reachable`. A TLS handshake failure also means reachable, because
  something answered (old iLO builds can fail modern OpenSSL defaults) and a
  false CRITICAL is worse than a missed one here. Only connection refused,
  timeout and no-route are unreachable.
- **No credentials.** ADR-0022's decision - one collection standard for all
  HP hardware, no Redfish pass, no BMC credentials - is untouched: the probe
  is additive and never logs in. It therefore can never report
  `auth_rejected` for OneView.

Cost estimate (not measured): ~800 iLOs at concurrency 64 is seconds when
healthy, ~60 s worst case if all time out at 5 s, against ~500 s through the
full collector path at its default concurrency of 16.

### 4. `REDFISH_STANDALONE` and `OPENMANAGE` report failures instead of dropping them

- Every failure branch of `_collect_host` yields a stub `ProviderServer`
  (`reachable=False`, reason mapped from the exception) instead of only
  calling `_record_error`, mirroring `OpenManageProvider._unreachable_server`.
  Every host in the inventory file therefore appears in the UI, and a wrong
  login is distinguishable from an unreachable host.
- `REDFISH_STANDALONE` and `OPENMANAGE`: the exit code is unchanged (`_is_benign_collection_error`
  is unchanged): a dead or rejecting BMC is benign (a health signal, not a
  failed CronJob), but a TLS failure, blown budget or protocol error is still
  recorded and makes the run PARTIAL.
- `OPENMANAGE` keeps its existing stub and gains the reason from the same
  inner Redfish error; its exit codes follow the same rule.
- The stub's name is `RedfishTarget.name` (operator-supplied in the inventory
  TOML), falling back to the host address.

### 5. A serial-less stub is matched by BMC address (option A)

Correlation is `(vendor, serial_normalized)` and a record with no serial is
always a new document. A failed standalone host has no serial, so:

- When a `ProviderServer` has no serial, `IngestService` looks up the
  existing document by `(source_provider, BMC host)` and updates it in
  place. The match is scoped to `REDFISH_STANDALONE`, so an address reused by
  another vendor cannot merge two machines.
- **A host that answered yesterday and fails today** updates the existing
  document (serial and last good hardware data carried forward, only the
  state changes, `last_seen_at` frozen) - the main case the feature exists
  for.
- **A host that never answered** gets one document, updated each run, with
  the serial filled in when it first succeeds.

Rejected - a synthetic serial (`host:<ip>`): it needs no ingest change but
creates a second, twin document every time a known server fails, leaving the
real one showing HEALTHY, and puts a fake serial into search and
`/servers/available` output.

### 6. `listed_at` and guarded pruning

A server a manager stops listing is deleted after 24 h (four runs at the
current schedule).

- **New field `Server.listed_at`**, set whenever the manager yields the
  server, reachable or not. `last_seen_at` cannot be used: it freezes for an
  unreachable server, so a dead-iDRAC server OME still lists would be pruned -
  exactly the server the new check exists to show.
- This also resolves an OME profile with no `TargetName` (unassigned): the
  collector skips it (`ome.profile_without_address`), so its `listed_at`
  stops moving and it ages out. A host still in the inventory file or still
  listed by OME is never pruned, however long it stays dead.
- **Guards:** prune only when that manager's latest run completed cleanly
  (otherwise a 25-hour OME outage would delete every Dell server); cap the
  share of one manager's servers deleted in a single pass; write an audit
  entry per deletion; dry-run by default, real deletion enabled by an
  environment variable.

## Consequences

- Two new stored fields, `unreachable_reason` and `listed_at`. Documents
  written before this change lack both; both must load as absent
  (`stored-shape-reviewer`).
- `fake/generator.py` models reachability and reasons for every vendor, not
  only OpenManage's existing flake rate (convention 10).
- `.claude/rules/collectors.md` currently says `REDFISH_STANDALONE` writes no
  `reachable=False` because it has "no serial to correlate on"; decision 5
  removes that reason and the line is updated with the change.
- Accepted residual risks: a BMC whose inventory IP changes is pruned after
  24 h and re-created on its next success (matched by serial, so no
  duplicate); a server briefly un-assigned in OME and pruned comes back as a
  new document without its old maintenance notes.

## Implementation notes (2026-10-01)

- **`cpu` category removed.** No default policy ever had category `cpu`, so
  the row could only read UNKNOWN. It is gone from `evaluate.CATEGORIES`,
  `Health`, the UI breakdown and the policy-category list, along with the
  unused `cpu.has_data`/`cpu.socket_count` facts. Documents that stored
  `health.cpu` still load (the model ignores extra keys).
- **`bmc.has_data`** is true for a `REDFISH_STANDALONE`, `OPENMANAGE` or
  `ONEVIEW` server that answered or failed, and false for any other
  collector or a OneView server with no iLO address.
- **BMC row in the UI.** The detail page hides the `BMC` row for UCS Central,
  UCS Manager and Intersight servers, which are never probed and could only
  read Unknown. Dell, HP and standalone always show it: there an Unknown means
  the check ran and got no answer (`BMC_UNCHECKED_PROVIDERS` in `OverviewTab`).
- **`last_seen_at`.** A stub that read nothing leaves it frozen. A server
  the manager did read (OneView with a dead iLO) still advances it, so it
  does not show Stale while OneView's data is fresh.
- **OpenManage stubs.** OpenManage now also yields the stub for TLS, budget
  and protocol failures, which it previously only recorded as PARTIAL; the
  exit code is unchanged. The stub's
  `get_one` fallback has no reason, because the inner `get_one` swallows it.
- **Redfish stub address.** `bmc_address_raw` is `redfish://<host>[:port]`,
  matching the success path without the `@odata.id` suffix.
- **Serial-less match** is `IngestService._find_by_bmc_host`, for
  `REDFISH_STANDALONE` only. It relies on the existing
  `source_provider_name_id` index prefix; no new index. An unreachable
  record with no model keeps the stored model.
- **OneView probe** is `infrastructure/bmc_probe.py`: concurrency 64, 5 s
  timeout, port 443, stamping in batches of 64. Any other `httpx.HTTPError`
  counts as reachable; any other exception as `network_unreachable`.
- **Pruning** is `tools/prune_servers.py`, dry-run unless `--apply` or
  `INVENTORY_PRUNE_ENABLED=true`. Settings: `INVENTORY_PRUNE_AFTER_SECONDS`
  (24 h), `INVENTORY_PRUNE_MAX_FRACTION` (0.2),
  `INVENTORY_PRUNE_MAX_RUN_AGE_SECONDS` (6 h). Per `source_provider`, every
  enabled manager's `last_run` must be non-partial, error-free and recent.
  Documents without `listed_at` (written before this change) are stamped
  with the current time on the first `--apply` and never deleted that run.
  The chart ships it as the `collectors.prune` CronJob, disabled and
  report-only by default (`apply: false`).
- **Audit.** Each deletion is an `EventType.SERVER_PRUNED` event (actor
  `SYSTEM`, id `prune`) whose `data` carries `server_id`, `name`, `serial`,
  `vendor`, `source_provider`, `manager_id`, `listed_at`, `last_seen_at`,
  `reason`, plus a `server.pruned` log line. Operators list them with
  `GET /api/v1/events?event_type=SERVER_PRUNED` (kept for `auditRetention.retentionDays`,
  180 by default, ADR-0045).
- **Metrics.** `server_scan_servers_pruned_24h{source_provider}` is derived
  from those audit events (ADR-0029 pattern); the Grafana dashboard gains
  total-servers and pruned-servers panels. Prometheus carries counts only;
  server names live in the audit log.

## Open items

- Probe timeout and concurrency are estimates; measure against the 821-server
  OneView appliance before fixing defaults.
