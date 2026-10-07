---
name: server-scan-api
description: Queries the Server Scan REST API to answer questions about the bare-metal server fleet - list or count servers by vendor, site, health, role, OpenShift cluster or availability, look up one server, read audit events, site overviews and health policies. Use whenever the user asks for servers ("all Dell servers in tlv", "which servers are available", "what changed on srv-12", "how many critical servers per site") or mentions Server Scan, the inventory API, or the fleet.
when_to_use: Also for questions about a server's BMC address, MACs, maintenance state, cluster membership or why its health changed.
allowed-tools: Bash(curl *) Bash(jq *)
---

# Server Scan API

Server Scan is an air-gapped inventory of ~2,500 physical servers (Dell, Cisco, HPE,
standalone Redfish). Collectors write to MongoDB; this REST API reads it. You answer
fleet questions by building `curl` requests, never by guessing.

## Connection - edit these two lines for your environment

```
BASE_URL=https://server-scan.example.internal
AUDITOR_TOKEN=REPLACE_WITH_AUDITOR_TOKEN
```

The values above are placeholders. In the air-gapped environment, replace both lines
here (do not commit the real token). Treat `AUDITOR_TOKEN` as a secret: pass it only in the
`Authorization` header and never echo it back to the user or print it in command output.
If the token is still the placeholder, ask the user for it before calling anything. If the
deployment has auth turned off, the header is simply ignored.

```bash
api() { curl -sS -H "Authorization: Bearer $AUDITOR_TOKEN" -H "Accept: application/json" "$BASE_URL$1"; }
api "/api/v1/servers?vendor=dell&site_id=tlv&page_size=200" | jq '.items[] | {name, model, health: .health.overall}'
```

The auditor token is read-only and, unlike the viewer token, may read the audit trail (the
events endpoints). Stay on GET: the write endpoints (maintenance, reservation, reclassify) need
the admin token, and you only use them when the user explicitly asks. If this holds a viewer
token, everything works except events, which answer `403`.

## Choose the endpoint

| The user wants | Call |
|---|---|
| Servers matching vendor / site / health / role / cluster | `GET /api/v1/servers` with filters (below) |
| Free-text find by part of a name, serial or model | `GET /api/v1/servers?search=<text>` |
| Counts or group-bys across the whole fleet (per site, per health) | `GET /api/v1/sites` first; for anything else `GET /api/v1/servers/rows`, then `jq` |
| One server in full detail (hardware, NICs, BMC, health reasons) | `GET /api/v1/servers/{id}` (id comes from a list result) |
| What happened to a server or the fleet | `GET /api/v1/events`, `GET /api/v1/servers/{id}/events` |
| Servers to provision a BareMetalHost on | `GET /api/v1/servers/available` - see the warning below |
| Why a health verdict exists | `GET /api/v1/health-policies`, `GET /api/v1/classification-rules` |

`/api/v1/servers` filters are plain query parameters, combined with AND. The accepted
keys are exactly: `site_id`, `vendor`, `manager_id`, `installation_type`, `health_overall`,
`maintenance` (true/false), `source_provider`, `openshift_state`, `cluster_name`, and
`stale` (true/false). Any other key returns `400 UNKNOWN_FILTER` listing the allowed ones, so
a wrong guess is cheap - but do not invent filters; use `/servers/rows` + `jq` for anything
else (model, MCE, MAC, BMC host, reachability).

Values are case-sensitive:

- `vendor`: `dell` `cisco` `hp` `standalone` (HPE is `hp`)
- `site_id`: the lowercase site code, e.g. `tlv`; `unassigned` for servers whose name has no site. List the real codes with `GET /api/v1/sites`.
- `health_overall`: `HEALTHY` `WARNING` `MAJOR` `CRITICAL` `UNKNOWN`
- `installation_type`: `UPI` `HOSTED_CLUSTER` `MCE` `UNCLASSIFIED`
- `openshift_state`: `AVAILABLE` (no cluster is using it) or `INSTALLED`
- `source_provider`: `UCS_CENTRAL` `INTERSIGHT` `OPENMANAGE` `ONEVIEW` `REDFISH_STANDALONE`

Paging: `page_size` defaults to 50, max 200. The response has `page.next_cursor` and
`page.has_more`; pass `cursor=<next_cursor>` with the same filters until `has_more` is
false. Add `with_count=true` for a total. Sort with `sort` (`name` default, `serial`, `model`,
`updated_at` (last content change), `last_seen_at` (last confirmed by a collector), `openshift_state`, `cluster_name`, `mce_name`) and `sort_desc=true`.
When the user wants "all" servers, loop on the cursor rather than silently returning page one.

## Recipes

```bash
# All Dell servers in Tel Aviv, every page
c=""; while :; do r=$(api "/api/v1/servers?vendor=dell&site_id=tlv&page_size=200${c:+&cursor=$c}");
  echo "$r" | jq -c '.items[] | {id, name, model, health: .health.overall}';
  [ "$(echo "$r" | jq .page.has_more)" = true ] || break; c=$(echo "$r" | jq -r .page.next_cursor); done

# Free servers in tlv that are healthy
api "/api/v1/servers?site_id=tlv&openshift_state=AVAILABLE&health_overall=HEALTHY&page_size=200"

# Fleet-wide breakdown the list endpoint cannot filter: Cisco servers by model
api /api/v1/servers/rows | jq -r '.items[] | select(.vendor=="cisco") | .model' | sort | uniq -c

# Per-site totals and health in one call
api /api/v1/sites | jq '.items[] | {site_id, total, by_health}'
```

`/servers/rows` returns the whole fleet (~190 KB gzipped) in one response with flat fields
(`name, vendor, model, site_id, health, installation_type, openshift_state, cluster_name,
mce_name, bmc_host, serial, macs, maintenance, reservation, stale, reachable`). Prefer it for
grouping, counting or filtering on a field the list endpoint lacks.

## Gotchas

- **`/servers/available` is not a pure read.** It live-queries the vendor manager for the few candidates it returns and writes the refreshed data back, and concurrent callers can be handed the same server. Use it only when the user is picking servers to provision, always give exactly one of `name` (exact) or `pattern` (regex), and keep `count` small (max 20). Otherwise answer "which are free" with the `openshift_state=AVAILABLE` filter above.
- **Health is a verdict, not a reading.** `UNKNOWN` means nothing could be read, not "fine". A server with `reachable=false` (BMC down) is `CRITICAL`. `stale=true` means the collectors have not seen it for 12 h - say so rather than presenting old data as current.
- **Availability is about clusters, not names.** A server named `ocp4-prod-...` can be `AVAILABLE`; trust `openshift_state`.
- **Errors are RFC 9457 JSON** (`application/problem+json`) with a stable `code`. Read `code` and `detail`, fix the request, and retry once: `UNKNOWN_FILTER`, `PAGE_SIZE_TOO_LARGE`, `CURSOR_FILTER_MISMATCH` (cursor reused with different filters), `NOT_FOUND`. `401` means the token is wrong or expired; `403` means a write was attempted with a read-only token, or events were read with a viewer token. Do not retry either - tell the user.
- Keep output small: pipe through `jq` to the fields the user asked about instead of dumping full documents.

## More detail

- Response fields, `available` and events parameters, every endpoint: [references/endpoints.md](references/endpoints.md)
- Interactive docs on a running instance: `$BASE_URL/docs`
- Project overview and design decisions: `README.md`, `docs/adr/0032-available-server-lookup-api.md`, `docs/adr/0034-ad-login-roles-and-api-tokens.md`
