---
paths:
  - "frontend/**"
---

# Frontend — read before touching the SPA

Loaded only when a `frontend/` file is open.

- **The UI is dark only, and four things enforce it together**: the dark
  values are the only token values in `index.css` (no
  `prefers-color-scheme` block), `color-scheme: dark` carries the browser's
  own scrollbars and controls, `index.html` sets a `color-scheme` meta plus
  an `html` background so the pre-stylesheet frame is not white, and
  `@custom-variant dark (&)` makes Tailwind's `dark:` utilities
  unconditional — the easy miss: 49 `dark:` classes across 13 components
  are media-query gated by default, so without it a dark page wears light
  badges on a light OS. **The check worth repeating:
  `grep -c prefers-color-scheme frontend/dist/assets/*.css` must be 0.**
- **The inventory page owns the whole fleet** (ADR-0033): one polled
  `GET /servers/rows`, then `features/inventory/rows.ts`'s pure functions
  do filter/search/sort/facets/paging. Do not reintroduce a per-filter
  request or a server-side page — a client-side filter over a server-side
  page silently filters only the loaded rows (the TanStack guide's one
  hard rule). A new row field goes in `ServerRow` on both sides and in
  `_ROW_PROJECTION`; the row must stay small (~520 B) and the body
  byte-stable for an unchanged fleet, or the ETag/304 stops working.
- **Maintenance is switched only from the inventory list** (per-row
  switch); the detail page shows it read-only. One place, on purpose.
- **The inventory table has no width to spare at 1440px** — with the MCE
  column showing it already overflows its wrapper by ~14px. A new column
  pushes Maintenance off-screen (a `Seen` column was built, measured and
  removed for exactly that on 2026-09-13); put row-level signals in the
  State cell as a chip that wraps under the badge (`StateBadge`'s
  `flex-wrap`), the way `Maint` and `Stale 20h` do. `stale` comes from the
  API as a flag; the frontend never knows the threshold.
- **`HealthSeverity` has five values** — `INFO` was retired 2026-09-13;
  `SEVERITY_GLYPH`, `SEVERITY_ORDER`, `StateBadge`, `HealthBadge` and the
  inventory's Health filter list must stay in step with the enum.
- **A new health category** touches `evaluate.CATEGORIES`, `Health`,
  `health_from_state` *and* the frontend's `HealthSummary`/`OverviewTab`
  — `POLICY_CATEGORIES` listing `gpu` while the rollup skipped it is how
  a failed GPU read HEALTHY overall until 2026-09-13.
- **Playwright: if you add a page with sibling `<select>` fields, do not
  use `getByLabel`.** A real Chromium quirk makes a `<label>`'s computed
  name include every nested `<option>`'s text, so "Source" resolves to
  `"SourceSITE_CUSTOMMANAGER_CUSTOMVENDOR_CUSTOM…"` and collides with the
  Vendor field. ADR-0008 has the XPath workaround; the `labeledField`
  helper that implemented it was removed with the editor pages, so the
  next form page will hit this cold.
- The gate for any frontend change is
  `cd frontend && npm run lint && npm run typecheck && npm run test -- --run && npm run build`
  (`/gate --frontend`); E2E (`npm run test:e2e`) needs the backend and dev
  server running.
