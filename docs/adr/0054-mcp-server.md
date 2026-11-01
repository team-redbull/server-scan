# ADR-0054: a read-only MCP server in front of the REST API

Status: accepted (2026-11-01)

## Context

The `server-scan-api` skill makes Claude Code build `curl` calls; it needs `curl`, `jq` and a token pasted
into a file. An MCP server gives the same questions as typed tools, with one `claude mcp add` command.

## Decision

`tools/mcp_server` (`python -m tools.mcp_server --http`), built on the stable 1.x `mcp` SDK (1.30.0).

- **A thin client of the REST API**, not a second data path: it imports nothing from `app`, has no database
  access and no credentials. Every tool is one or two `GET`s; the API stays the only place that decides who
  may read what, and the only place that audits.
- **The caller's own token is passed through** from the request's `Authorization` header (a personal token,
  ADR-0053) and never stored or logged. Over stdio (local use) it reads `SERVER_SCAN_TOKEN`.
- **Streamable HTTP, stateless, JSON responses**, served at `/mcp` on the existing host: the frontend's nginx
  forwards `/mcp` to the `<release>-mcp` Service when `mcp.enabled` (default false). Same image as the API with
  another command. The SDK's DNS-rebinding guard stays on; nginx sends `Host: <release>-mcp`, the only name
  the pod accepts (`INVENTORY_MCP_ALLOWED_HOSTS`).
- **Eight read-only tools** (`readOnlyHint`): `list_servers`, `find_servers`, `get_server`, `fleet_summary`,
  `list_sites`, `server_events`, `recent_events`, `list_health_policies`. Lists are compact and capped
  (`truncated` says more matched); `fleet_summary` counts server-side so Claude need not read the fleet.
  A unit test checks `list_servers`'s filters against the API's `FILTER_FIELDS`.
- **Not in v1: `GET /servers/available`.** It is not a pure read (it rechecks a vendor manager and writes back).
- Server names and health reasons come from vendor systems: the server's instructions tell the model to treat
  them as data.

## Consequences

A viewer's Claude cannot read events (the API answers 403, the tool says why). A change to a REST filter or
response shape can break a tool; the filter test catches the first. The chart gains a Deployment and a Service
that hold no secrets. `mcp` adds ten small dependencies (including `python-multipart`, which this repo once
removed as unused) to `requirements.txt` and `pylock.toml`; confirm the air-gapped mirror carries `mcp==1.30.0`.
