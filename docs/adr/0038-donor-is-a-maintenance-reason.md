# ADR-0038: A parts donor is a maintenance whose reason says "donor"

Date: 2026-10-01
Status: Accepted

Extends ADR-0008 (maintenance) and ADR-0032 (available servers). Supersedes nothing.

## Context

A server with one bad component (a failing DIMM, a PSU) is sometimes kept
racked only so its good parts can be moved into other servers. The operator
wants to mark such a server so it is visibly different in the inventory and
is never handed out by `GET /servers/available`.

## Decision

**No new field, endpoint or audit event.** A donor is a server in
maintenance whose `maintenance.reason` contains the word "donor", in any
case (`/\bdonors?\b/i`): "Donor", "DONOR - DIMM B3 bad", "parts donors".
Text that merely contains the letters ("Donorship") does not count, and a
server that leaves maintenance stops being a donor whatever its old reason.

- **Why a convention, not a flag.** Maintenance already carries exactly the
  behaviour a donor needs: operator-set, admin-only, audited
  (`MAINTENANCE_ENABLED`/`UPDATED`/`DISABLED` with the reason), carried
  forward by ingest, and excluded from `/servers/available` in both the
  Mongo draw and the live recheck (`maintenance.enabled: False`). A separate
  `Donor` sub-document would duplicate all of it for one label.
- **What changes.** Only presentation: `StateBadge` renders a cyan **Donor**
  pill instead of the violet **Maint** pill (`lib/donor.ts`, tokens
  `--tint-donor`/`--text-on-donor`), and the maintenance popover tells the
  operator to include the word "donor". Nothing in the API, the stored
  document or the availability gates changes.
- **Health is untouched.** A donor keeps its real health, so the failing
  part still shows as a problem, and it still counts in the fleet totals.

## Consequences

- The rule lives in the frontend only. An API consumer that wants donors
  reads `maintenance.reason` itself; `/servers/available` already never
  returns them because they are in maintenance.
- The inventory has a **Donor** filter checkbox after "Name mismatch"
  (`?donor=true`, `filters.donor` in `rows.ts`), applied client-side like
  every other filter (ADR-0033); the Maintenance filter still lists donors
  alongside other maintenance servers.
- Reasons are free text, so a typo ("donar") silently means an ordinary
  maintenance. The popover hint is the only guard.
