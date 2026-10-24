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

- (Superseded 2026-10-25, see the update below.) A URL with both `maintenance=true` and `health_overall=X`
  showed Maintenance and ignored the severity.
- Exact mode compares whole field values only; there is no exact-on-one-field syntax.

## Update 2026-10-25: Maintenance is its own Include / Hide / Only control

The Health-dropdown option made two questions unaskable: "all healthy servers, without the ones in
maintenance" and "healthy servers that are in maintenance", because choosing Maintenance cleared the
severities. A server in maintenance is still counted under its own health everywhere else (the Sites cards),
so the inventory now has a three-way control next to the Health filter: **Include** (the default, so a
card's "12 healthy" opens a list of 12), **Hide** and **Only**.

- `RowFilters.maintenance` is `"only" | "exclude"`, unset meaning include. The URL stays compatible:
  `maintenance=true` (and `only`) is Only, which keeps every Sites-card link and old bookmark working;
  `maintenance=exclude` is Hide. A URL with `maintenance=true` and `health_overall=X` now applies both.
- The Health dropdown lost its Maintenance entry. Its counts follow the maintenance mode; the control's own
  counts (`Include (35) | Hide (29) | Only (6)`) are taken without the maintenance filter, so all three
  numbers are always visible. A chip ("Maintenance hidden" / "Maintenance only") removes it.
- Default Include rather than Hide on purpose: hiding by default would silently disagree with the Sites
  counts. Frontend only; no API change.
