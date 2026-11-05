# ADR-0057: The BMC names the site first, and a disagreement is a CRITICAL `name` health reason

**Status:** Accepted, implemented 2026-11-05.

**Reverses** the order in ADR-0056 (name first, BMC IP last). **Extends** ADR-0011 and ADR-0018.

## Context

ADR-0056 let the BMC IP name a site only when the name and profile DN said nothing. The operator's
call: the iDRAC is the source of truth for where a server is, so a wrong hostname must not win, and
the two disagreeing must be visible, not silent.

## Decision

1. **Order: BMC IP prefix, then name, then profile DN.** `Server.site_id` is the first of the three
   that answers. `Server.site_from_bmc` and `Server.site_from_name` store what the BMC and the name say
   alone (both default `None`, so older documents still load). A run that could not read the BMC address
   keeps the stored `site_from_bmc`, so a failed read does not move the server.
2. **A new health category `name`.** It holds two CRITICAL policies: the capacity-vs-name check moved
   from `storage` (same `policy_key` `storage.name_capacity_mismatch`, so alerts and dashboards keep
   matching) and the new `name.site_conflict` ("BMC IP says bat-yam, but the name says tlv"), which fires
   only when both sides name exactly one site and they differ.
3. **`name.has_data` is true only when there is something to compare** (a capacity token against a
   storage total that was read, or both site answers). Otherwise the category is UNKNOWN, never a free
   HEALTHY (ADR-0027: a server with nothing read stays UNKNOWN overall). The UI hides the Name row while
   it is UNKNOWN.

## Consequences

- With `INVENTORY_SITE_BMC_PREFIXES` set, servers whose name and BMC IP disagree change site on the next
  collector run. A wrong prefix mislabels every server in its range, and each disagreeing server
  turns CRITICAL, which is the signal to fix the rule or the name.
- Health of existing servers changes only when re-evaluated: run the health recalculate endpoint once
  after deploying, because an unchanged server is not re-ingested (ADR-0044).
- The storage category no longer turns CRITICAL for a capacity mismatch; the overall health still does.
