# ADR-0046: The inventory search has an exact and a regex form, chosen by how you type

Date: 2026-10-11
Status: Accepted

Refines ADR-0033 (the inventory searches the whole fleet in the browser). ADR-0025 (the API's
prefix-token search behind `GET /servers`) is unchanged.

## Context

The inventory search is a case-insensitive substring over name, model, serial, vendor, site,
installation type, BMC host, MACs and a reservation's MCE. With BMCs `10.0.0.1`, `.10`, `.11`, `.13`
and `.17`, typing `10.0.0.1` shows all five, because `10.0.0.10` contains it. There was no way to ask
for one address, and typing a regex searched for its literal characters.

## Decision

The box text picks the mode (`parseSearch`, `features/inventory/rows.ts`):

| Text | Mode |
|---|---|
| `10.0.0.1` | substring (unchanged, the default) |
| `"10.0.0.1"` | exact: a whole field must equal it (a MAC also matches bare hex) |
| `/10\.0\.0\.(1\|13)$/` | case-insensitive JavaScript regex over the same fields |

Why this shape: the forgiving default stays, so partial searches (`cisco-m6`) keep working, and the
precise forms are opt-in, as in Kibana, GitHub and Gmail. Guessing intent (treating anything shaped
like an IP as exact) was rejected: an implicit rule surprises whoever typed half an address.

- **A bad regex never blanks the table.** An invalid pattern or one over `MAX_REGEX_LENGTH` (64, the
  API's own search cap) sets `SearchMatcher.error`, shown under the box (`role="alert"`), and every row
  stays visible.
- **Safe because it runs in the viewer's browser** on rows already downloaded: a catastrophic pattern
  can only stall that tab. Nothing reaches MongoDB or the API, which is why ADR-0025's rule (user input
  is never a raw regex server-side) is untouched.
- The placeholder and the box's tooltip document the syntax.

## Same change: Maintenance joins the Health dropdown

Maintenance is a flag, not a severity, and a server in maintenance still counts under its own severity.
The Health dropdown now ends with a **Maintenance** option that sets the existing `maintenance=true`
param and clears `health_overall` (and a severity clears `maintenance`): the two are exclusive, the
operator's call. The separate Maintenance checkbox was removed as a duplicate control. The Sites page's
"N in maintenance" is a link to `?maintenance=true`, like critical/major/warning. No API change.

## Consequences

- A URL with both `maintenance=true` and `health_overall=X` shows Maintenance; the severity is ignored.
- Exact mode compares whole field values only; there is no exact-on-one-field syntax.
