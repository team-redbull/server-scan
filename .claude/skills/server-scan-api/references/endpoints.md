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
- Policies and rules
- Auth and health
- Write endpoints (admin only)

## Servers

`GET /api/v1/servers` - keyset-paginated list. Parameters: `search`, `sort`, `sort_desc`,
`cursor`, `page_size` (default 50, max 200), `with_count`, plus the filter keys in SKILL.md.
`search` matches from any word boundary of the name, serial or model (ADR-0025) (minimum length enforced:
`SEARCH_QUERY_TOO_SHORT`). Response: `{items: [ServerSummary], page: {next_cursor, has_more, page_size, count, count_capped}}`.

`ServerSummary`: `id, name, vendor, model, site_id, manager_id, source_provider,
classification{installation_type, matched_rule_name, matched_pattern, matched_field},
health{overall, memory, storage, network, connectivity, power, gpu, bmc, reasons[], active_policy_keys[]},
maintenance{enabled, reason, created_by, created_at, expected_end},
openshift{lifecycle_state, cluster_name, mce_name, last_reported_at, reported_name, previous_reporter,
claim_changed_at, contested_with},
connectivity, last_seen_at, stale, reachable, unreachable_since, unreachable_reason, updated_at`.

`GET /api/v1/servers/rows` - whole fleet as flat rows, `{items: [ServerRow], generated_at}`.
Weak ETag: send `If-None-Match` to get a bodiless 304 when nothing changed.
`ServerRow`: `id, name, vendor, model, site_id, source_provider, installation_type, health,
maintenance, reservation, openshift_state, cluster_name, mce_name, openshift_reported_name,
contested_with, profile_template_name, last_seen_at, stale, reachable, serial, bmc_host, macs[]`.
Note `health` here is the overall severity string, not an object.

`GET /api/v1/servers/{server_id}` - full `ServerDetail`: hardware (CPU, memory, storage,
GPUs, PSUs), network interfaces, BMC info, identity, health with per-category reasons.

## Available servers

`GET /api/v1/servers/available` - assignable servers for a BareMetalHost, live-rechecked
against the vendor manager. Exactly one of `name` (exact, case-insensitive) or `pattern`
(MongoDB regex on the name). Optional: `count` (pattern mode, default 1, max 20), `vendor`,
`source_provider`, `health` (one of HEALTHY/WARNING/MAJOR; default fills best first),
`min_nic_macs` (0-16; pass 1+ for a BMH, 2 for bonding).
Response: `{items, mode, requested, returned}`; item: `id, name, vendor, source_provider,
bmc_vendor (HP/DELL/CISCO/INTERSIGHT), bmc{host, scheme, port, mac...}, nic_macs[],
interfaces[{name, mac, location, os_name, link_state, speed_mbps}], site_id,
health_overall, live_recheck_performed`.
Errors: `AVAILABLE_LOOKUP_CONFLICTING_PARAMS` (neither/both of name/pattern),
`AVAILABLE_COUNT_TOO_LARGE`, ambiguous name, nothing found.

## Events

Auditor or admin token only.
`GET /api/v1/events` - audit trail, newest first. Parameters: `server_id`, `server_name`,
`event_type`, `actor_id`, `since`, `until` (ISO 8601), `cursor`, `page_size`.
`GET /api/v1/events/actors` - who has acted, with event counts (for the `actor_id` filter).
`GET /api/v1/servers/{server_id}/events` - one server's history.
Event: `id, event_type, server_id, server_name, actor{type,id,...}, request_id, created_at, data`.
Types: `HEALTH_CHANGED` (data has `from_reasons`/`to_reasons`), `CLASSIFICATION_CHANGED`,
`OPENSHIFT_STATE_CHANGED`, `MAINTENANCE_ENABLED|UPDATED|DISABLED`, `SERVER_RESERVED|RELEASED|RESERVATION_REFUSED`,
`SERVER_CREATED|UPDATED|DELETED|PRUNED`, plus rule/policy/site/manager events.

## Sites

`GET /api/v1/sites` - `{items: [SiteStats], fleet: FleetSummary}`. Each site: `site_id, name`,
`total`, `by_vendor`, `by_health`, `in_maintenance`, `by_installation_type`, `by_openshift_state`.
Use it to learn the valid `site_id` codes.

## Policies and rules

`GET /api/v1/health-policies[?enabled=]`, `/{policy_id}`, `GET /api/v1/health-metrics` (what
policies can test), `GET /api/v1/classification-rules[?enabled=]`, `/{rule_id}`. Read-only:
they ship with the platform.

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
`POST /api/v1/servers/{id}/health/recalculate`. Do not call these unless the user asks and
has supplied the admin token.
