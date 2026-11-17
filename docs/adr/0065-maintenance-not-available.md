# ADR-0065: A server in maintenance is not available

**Status:** Accepted, implemented 2026-11-17.

## Context

`openshift.lifecycle_state` is a reading the membership jobs write (ADR-0024): `AVAILABLE` means no cluster
holds the server. Maintenance is an operator flag beside it. So a free server in maintenance showed two states
at once: the green "Available" Installation badge and the maintenance switch on. It was counted on the Sites
"Available" card (with an "N in maintenance" link under it) and matched `?openshift_state=AVAILABLE`, the filter
the Claude skill and the MCP tool use to answer "which servers are free". `GET /servers/available` already
refused it (`MAINTENANCE` reason, ADR-0032), so only the read surfaces disagreed.

## Decision

Available means "no cluster holds it, and it is not in maintenance". Derived on read, never stored:
`lifecycle_state` stays the collectors' reading (writing an intent into it would be reset by the next
membership run, the same reason `Reservation` is not a lifecycle state).

- **API filter:** `?openshift_state=AVAILABLE` on `GET /servers` adds `maintenance.enabled != true`
  (`search.build_filter_query`, an `$and` clause, so `?maintenance=true` beside it matches nothing).
  `INSTALLED` and `INSTALLED_TO_INVENTORY` are unchanged: an installed server in maintenance is still installed.
- **`GET /sites`:** `by_openshift_state` gains a fourth slice, `MAINTENANCE` (no cluster holds these, and
  they are in maintenance). `AVAILABLE` no longer counts them, and the slices still sum to the total.
- **UI:** the Installation badge reads "Maintenance" for such a server (table and Overview), and the
  inventory's client-side `openshift_state` filter, its facet count, sorting and the XLSX export use the
  same derived value (`lib/installation.ts`), so the Sites "Available" card and the list it links to agree.
- `GET /servers/available` is unchanged; it already excluded maintenance.
- The same "in service" reading the inventory's Health filter took on 2026-11-11 (ADR-0046 update: Healthy
  excludes a server in maintenance); `rows.ts` applies both, `matchesHealth` for Health and `installationOf`
  for Installation.

## Consequences

- A caller that used `?openshift_state=AVAILABLE` to list free *and* in-maintenance servers must now ask
  for `?maintenance=true` without it. The Sites "Available" card no longer shows an "in maintenance" link;
  those servers are under the card's total only through the new `MAINTENANCE` slice.
- No stored shape changed. A `?openshift_state=AVAILABLE` cursor issued before the upgrade fails
  `CURSOR_INVALID` once (its filter changed); the client restarts from page one.
