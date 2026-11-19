# Server Scan API reference

All paths are under `$BASE_URL`. JSON everywhere. Read endpoints work with the viewer
or auditor token (the events endpoints need the auditor or admin token, a viewer gets `403`);
mutations need the admin token. Generated from the app's own OpenAPI schema -
`$BASE_URL/openapi.json` is authoritative if this drifts.

## Contents
- Servers
- Available servers
- Events
- Sites
- Confs
- Policies and rules
- Skill
- Auth and health
- Write endpoints (admin only)
- Jobs (admin only)

## Servers

`GET /api/v1/servers` - keyset-paginated list. Parameters: `search`, `sort`, `sort_desc`,
`cursor`, `page_size` (default 50, max 200), `with_count`, plus the filter keys in SKILL.md.
`search` matches from any word boundary of the name, serial or model (ADR-0025) (minimum length enforced:
`SEARCH_QUERY_TOO_SHORT`). Response: `{items: [ServerSummary], page: {next_cursor, has_more, page_size, count, count_capped}}`.

`ServerSummary`: `id, name, vendor, model, site_id, manager_id, source_provider,
classification{installation_type, matched_rule_name, matched_pattern, matched_field},
health{overall, memory, storage, raid, network, connectivity, power, gpu, bmc, name, profile, reasons[], active_policy_keys[]},
maintenance{enabled, reason, created_by, created_at, expected_end},
openshift{lifecycle_state, cluster_name, mce_name, last_reported_at, reported_name, previous_reporter,
claim_changed_at, contested_with},
connectivity, last_seen_at, stale, reachable, unreachable_since, unreachable_reason, unreachable_detail, updated_at`. `updated_at` and `revision` move only when a server's content
changes, not on every collector run; `last_seen_at` is the per-run "confirmed" stamp. `unreachable_reason` is one of `network_unreachable`,
`auth_rejected`, `tls_error`, `timeout`, `protocol_error`, `not_an_idrac`, `collection_error` (the manager listed the server but its record could not be processed);
`unreachable_detail` is the cause in words (null when `reachable` is true).

`GET /api/v1/servers/rows` - whole fleet as flat rows, `{items: [ServerRow], generated_at}` (`generated_at` = newest `updated_at` or `last_seen_at`).
Weak ETag: send `If-None-Match` to get a bodiless 304 when nothing changed.
`ServerRow`: `id, name, vendor, server_type (physical|vm; vm = a KubeVirt VM), model, site_id, source_provider, installation_type, health,
maintenance, reservation, openshift_state, cluster_name, mce_name, openshift_reported_name,
contested_with, profile_template_name, last_seen_at, stale, reachable, serial, bmc_host, macs[]`.
Note `health` here is the overall severity string, not an object. `UNKNOWN` also means every NIC link (OneView excepted) or every drive health was unread, or a stopped KubeVirt VM.

`GET /api/v1/servers/{server_id}` - full `ServerDetail` (also carries `server_type`): hardware (CPU, memory, storage,
GPUs, PSUs), network interfaces, BMC info, identity, health with per-category reasons.
`vm_host` is `{cluster_name, node_name}` for a KubeVirt VM (`node_name` null when stopped), null otherwise.
`profile_deployment` is `{last_run_status, last_run_status_name}` (OME ids: 2060 `Completed`, 2070 `Failed`) for a
Dell `OPENMANAGE` server, both null for any other collector; a `Failed` one makes `health.profile` and `overall` CRITICAL (ADR-0066).

`GET /api/v1/servers/{server_id}/vm-host` - the bare-metal server a KubeVirt VM runs on, resolved
now (404 for a non-VM). Response: `cluster_name, node_name, match, host`; `match` is `MEMBERSHIP`
(through the cluster's nodes-status membership), `NAME` (server name only), `AMBIGUOUS`,
`NOT_IN_INVENTORY` or `NOT_RUNNING`; `host` (only on MEMBERSHIP/NAME) is `id, name, vendor, model,
site_id, cpu{sockets, cores, threads, model}, memory_total_bytes, health, maintenance, cluster_name`.
Use it for "which physical CPU/server does VM X run on"; a VM's own `hardware.cpu.model` is the
guest's virtual CPU (often null).

## Available servers

`GET /api/v1/servers/available` - assignable servers for a BareMetalHost, read from the stored data
(no live recheck, no lock; `live_recheck_performed` is always `false`). Exactly one of `name` (exact,
case-insensitive) or `pattern` (MongoDB regex on the name). Optional: `count` (pattern mode, default 1, max 20),
`vendor`, `source_provider`, `max_health` (the worst acceptable tier, every better one allowed: `HEALTHY`,
`WARNING`, `MAJOR` (default) or `CRITICAL`; `UNKNOWN` is a 400; the old `health` is a 400), filled best first,
`min_nic_macs` (0-16; pass 1+ for a BMH, 2 for bonding).
Response: `{items, mode, requested, returned}`; item: `id, name, vendor, source_provider,
bmc_vendor (HP/DELL/CISCO/INTERSIGHT; null for a STANDALONE server and a KUBEVIRT VM), bmc{host, scheme, port, mac...}, nic_macs[],
interfaces[{name, mac, location, os_name, link_state, speed_mbps}] (natural order of `name`, `eno2` before `eno10`; never rely on stored or `os_name` order), site_id,
health_overall, live_recheck_performed`.
Errors: `AVAILABLE_LOOKUP_CONFLICTING_PARAMS` (neither/both of name/pattern, a bad `max_health`, `health`),
`AVAILABLE_COUNT_TOO_LARGE`, ambiguous name, nothing found (404 `AVAILABLE_SERVER_NOT_FOUND`: `detail` says why,
e.g. "already installed in cluster x" or "in maintenance", `details.reasons` has codes such as
`INSTALLED`, `CLAIMED`, `MAINTENANCE`, `UNREACHABLE`, `RESERVED`, `HEALTH`, `NETWORK`).

`POST /api/v1/servers/available` - **takes one server (admin token only; changes data)**. JSON body: exactly one
of `name`/`pattern`; `holder` and `mce_cluster` (required); `workflow_id`, `infra_env`, `namespace`;
`ttl_seconds` (300-86400, default 7200: how long the server stays locked); `max_health`, `vendor`,
`source_provider`, `min_nic_macs` as above. No `count` (unknown fields are a 422). It locks a candidate, live-checks
it against the vendor manager while locked, and returns it; a candidate that fails the check (health above
`max_health`, unreachable, in maintenance, gone, vendor error) is released and the next is tried, at most 5 per
request. Response: the GET envelope with exactly one item (`live_recheck_performed` is `true` unless the manager
is not configured). Errors: 400 (neither/both of name/pattern, `max_health=UNKNOWN`), 409 ambiguous name,
422 (unknown or missing field), and **503 `NO_AVAILABLE_SERVER` for every case where nothing could be reserved**
(`detail` names each candidate's failure; `details.reasons` has `INSTALLED`, `CLAIMED`, `MAINTENANCE`,
`UNREACHABLE`, `RESERVED`, `HEALTH`, `NETWORK`, `NIC_MACS`, `VENDOR_ERROR`, `GONE_FROM_MANAGER`) - retry later.
Release a server you will not use with `DELETE /api/v1/servers/{id}/reservation`; it also frees itself at the TTL.

## Events

Auditor or admin token only.
`GET /api/v1/events` - audit trail, newest first. Parameters: `server_id`, `server_name` (case-insensitive substring of the server's name, serial or BMC host; pruned servers still match),
`event_type`, `actor_id`, `since`, `until` (ISO 8601), `cursor`, `page_size`. Response `page` is `{next_cursor, has_more, page_size, total}`; `total` (events matching the filters) is set on the first page only, null once a `cursor` is sent.
`GET /api/v1/events/actors` - who has acted, with event counts (for the `actor_id` filter).
`GET /api/v1/servers/{server_id}/events` - one server's history.
Event: `id, event_type, server_id, server_name, server_serial, server_bmc (snapshots at write time; null on old events of a server that is gone), actor{type,id,...}, request_id, created_at, data`.
Types: `HEALTH_CHANGED` (data has `from_reasons`/`to_reasons`), `CLASSIFICATION_CHANGED` (data has `from`/`to` and, when the server was renamed in the same collection, `name_from`/`name_to`),
`OPENSHIFT_STATE_CHANGED`, `MAINTENANCE_ENABLED|UPDATED|DISABLED`, `SERVER_RESERVED|RELEASED|RESERVATION_REFUSED`,
`SERVER_CREATED|UPDATED|DELETED|PRUNED` (`SERVER_DELETED` = an admin removed it by hand; data has `vendor`, `source_provider`, `forced`, `overridden`), `SERVER_RENAMED` (the manager reports a new name for a known server; data has `name_from`, `name_to`; `server_name` is the new one), `IDRAC_RESET` (the Dell collector restarted a server's iDRAC because every disk or NIC read UNKNOWN; data has `trigger` `disks|nics|both`, `outcome` `requested|unconfirmed|failed`, `bmc_host`, `detail`, `active_jobs`, `powered_on` = the host was off and was powered on first, `power_state` = its `PowerState` read before anything was sent, `On`/`Off`/null), `AUDIT_PURGED` (weekly retention job; data has `deleted`, `retention_days`, `cutoff`; events older than 180 days by default are gone), `LOGIN_SUCCEEDED` / `LOGIN_DENIED` (every authenticated login; `actor.id` and `data.user` are the username, `data.role` is `ADMIN|AUDITOR|VIEWER` or `NO_PERMISSION` for a refused one; no server), `TOKEN_CREATED|TOKEN_REVOKED`, `JOB_TRIGGERED` (an admin ran a CronJob by hand from the UI; data has `cronjob`, `job`, `namespace`; no server), `SERVER_REFRESHED` (an admin fetched one server from its manager; data has `manager_type`, `changed`), plus rule/policy/site/manager events.

## Sites

`GET /api/v1/sites` - `{items: [SiteStats], fleet: FleetSummary}`. Each site: `site_id, name`,
`total`, `by_vendor` (one entry per vendor, `kubevirt` included, zero where none), `by_health` (servers in service only; those in maintenance are counted in `in_maintenance`, so `total = sum(by_health) + in_maintenance`), `in_maintenance`, `by_installation_type`, `by_openshift_state`.
Use it to learn the valid `site_id` codes.

## Confs

`GET /api/v1/confs` - `{items: [Conf], unreadable: [conf_id]}`, every server configuration (spec), sorted
naturally by `conf_id`; unpaginated. `unreadable` names stored documents that do not match the schema (skipped).
`GET /api/v1/confs/{conf_id}` - one `Conf`; `404` when no conf has that id. Read-only, any role (ADR-0061).
A `Conf`: `conf_id` (`CONF200`, a revision `CONF200.2`, or a scan id `CONF-21` for an unmapped conf),
`part_number` (`null` when unmapped), `model`, `cpu {model, vendor, sockets,
cores_per_cpu, total_cores_ht, clock_mhz, max_clock_mhz, cache_kb {l1, l2, l3}}`, `ram {total_gib, dimms,
per_dimm_gb, type: [string]}` (`total_gib` 0 = the memory scan failed; `type` `[]` when the DIMM read failed),
`disks [{count, type, capacity_tb}]` (no drive model; `type` such as `NVME`, `SSD`, or `"unknown"`),
`gpus [{count, model}]` (grouped by model; sum per model anyway, older documents listed one per unit; may be `[]`
while `gpu_count` is not 0), `gpu_count`, `gpu_vram_gb` (VRAM per GPU, `null` or `0` on a GPU-less conf),
`adapters [{count, ports, speed}]` (no adapter model; `speed` per port, read as Gb/s), `adapter_count` (the
writer's total, not always the sum of `adapters[].count`). A conf is not linked to servers: no server field
names its conf, and the API returns neither a server count nor the conf's name.

## Policies and rules

`GET /api/v1/health-policies[?enabled=]`, `/{policy_id}`, `GET /api/v1/health-metrics` (what
policies can test), `GET /api/v1/classification-rules[?enabled=]`, `/{rule_id}`. Read-only:
they ship with the platform.

## Skill

`GET /api/v1/skill` - this skill's own files and metadata, read from disk per call: `{name, description,
when_to_use, allowed_tools, version, updated_at, files: [{path, content}]}`. `GET /api/v1/skill/download?base_url=`
- the same as a zip for `~/.claude/skills/`. Any role.

A signed-in user's one personal token (a session cookie is required; a token cannot issue one):
`GET/POST/DELETE /api/v1/auth/token` (POST returns the secret once). Admin: `GET /api/v1/auth/tokens`,
`DELETE /api/v1/auth/tokens/{username}`. They answer 404 when login is off.

## Auth and health

`GET /api/v1/auth/me` - the browser session's identity and role; it reads the cookie only, so a bearer token always
looks unauthenticated here. To check a token, make a cheap read such as `GET /api/v1/sites`.
`POST /api/v1/auth/login|logout` - browser session only; machine callers use the bearer token.
Login answers `429 RATE_LIMITED` with a `Retry-After` header after repeated wrong passwords
for one username; wait it out, do not retry.
`GET /health/live`, `GET /health/ready` - unauthenticated probes.

## Write endpoints (admin only)

`PUT|DELETE /api/v1/servers/{id}/maintenance`, `POST|DELETE /api/v1/servers/{id}/reservation`
(install lock, ADR-0035), `POST /api/v1/servers/{id}/reclassify`,
`POST /api/v1/servers/{id}/health/recalculate`, `POST /api/v1/servers/{id}/refresh` (reads that one
server from its vendor manager now and returns the `ServerDetail`; `revision` moves only if its data
changed; 409 `SERVER_REFRESH_UNSUPPORTED` when the API is not connected to that manager, e.g. standalone
Redfish, 409 `SERVER_NOT_AT_MANAGER` when the manager no longer reports it, 503 when the manager fails;
audited as `SERVER_REFRESHED`), and `DELETE /api/v1/servers/{id}` (removes the server from the database, 204;
409 `SERVER_DELETE_BLOCKED` with the reasons in `details.blockers` while it is in use by OpenShift, reserved or
in maintenance; `?force=true` deletes it anyway; 404 for an unknown id; audited as `SERVER_DELETED` with `forced` and
`overridden`; a server its manager still lists is recreated by the next collector run). Do not call these unless the user asks and has supplied the admin token.

## Jobs (admin only, every route, reads included)

A viewer, an auditor and every personal token get `403`. Do not call these unless the user asks and
has supplied the admin token. ADR-0055.
`GET /api/v1/jobs` - `{enabled, backend, namespace, cronjobs[]}`; each cronjob has `name, schedule, suspend, last_schedule_time, last_successful_time, warning, active` (a Job of it is running), `enabled` (the job's flag in the deployment's values.yaml; only the prune jobs are in that catalog, so a collector is always `true` and a switched-off collector is simply absent, ADR-0060), `deployed` (the CronJob exists; a switched-off one does not) and `jobs[]` newest first (`name, created_at, started_at, finished_at, active, succeeded, failed, state` = `active|succeeded|failed|pending`, `manual, triggered_by, pods[{name, phase, started_at}]`). `enabled: false` means the deployment does not offer it.
`POST /api/v1/jobs/{cronjob}/run` - starts a real Job from the CronJob (`201`, the new Job). `409` when one of its Jobs is running, `404` when the name is not a triggerable CronJob (only the platform chart's prune CronJobs and the collector CronJobs of the `server-scan-collectors` release in the same namespace are; never the `nodes-status` jobs or a KubeVirt cluster's own collector). The Job runs exactly as scheduled: the Dell collector can restart an iDRAC and the prune jobs delete data.
`GET /api/v1/jobs/{cronjob}/runs/{job}/pods/{pod}/logs?tail=500` - `{pod, tail, text}`; `404` unless the pod belongs to that Job of that CronJob.
