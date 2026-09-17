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
- **`OverviewTab`'s two columns are two explicit `<dl>`s, not one
  auto-flowing grid** (2026-09-17) — a single `grid-cols-2` over a flat
  field list means a vendor missing one conditional field (the profile
  template row `REDFISH_STANDALONE` never has, or "Collection" on a
  reachable server) reflows every field after it into the other visual
  column, so a standalone server's Overview looked structurally
  different from every other vendor's even though the field list is the
  same. Add a new field to the fixed left or right array, never back to
  one flat list.
- **A row's link into `/servers/:id` passes `state: { from }`** (its own
  `pathname + search`, 2026-09-17) so the detail page's "Back to
  inventory" returns to the same filtered view, not a blank one. Falls
  back to `/servers` — **not `/`**, the sites overview — for a direct
  visit with no router state. Both `InventoryTable`'s `<Link>` and its
  row `onClick`'s `navigate()` must pass it; either one bypassing it
  loses the filter for that click path only, silently.
- **The inventory table has no width to spare at 1440px** — with the MCE
  column showing it already overflows its wrapper by ~14px. A new column
  pushes Maintenance off-screen (a `Seen` column was built, measured and
  removed for exactly that on 2026-09-13); put row-level signals in the
  State cell as a chip that wraps under the badge (`StateBadge`'s
  `flex-wrap`), the way `Maint` and `Stale 20h` do. `stale` comes from the
  API as a flag; the frontend never knows the threshold. **A narrow
  icon-only column fits where a text column doesn't**: the "BMC" column
  (2026-09-17, opens `https://<bmc_host>` in a new tab, `size-7` like
  `MaintenanceToggle`'s own button) measured zero horizontal overflow at
  1440px with MCE showing too — verified with a real headless Chromium
  run (`document.body.scrollWidth` vs `window.innerWidth`), not by eye.
  Renders nothing for a server with no `bmc_host` read.
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
  next form page will hit this cold. **A second `getByLabel` trap, hit
  renaming "Maintenance only" to "Maintenance" (2026-09-16, broke CI):**
  `getByLabel` matches any accessible name, not just a `<label>` — the
  inventory row's `aria-label="Put X into maintenance"` buttons made a
  bare `getByLabel("Maintenance")` a 51-element strict-mode violation.
  Scope with `getByRole("checkbox", { name: "Maintenance" })` instead —
  substring matching still survives the checked state's `" (N)"` suffix,
  but `role: "checkbox"` excludes every button. **A third one, same day
  the BMC column shipped (also broke CI, also missed locally because
  `npm run test/lint/typecheck/build` never runs Playwright — only
  `npx playwright test` does):** an inventory row now carries two
  `<a>`s (Name, BMC), so `row.getByRole("link")` and
  `getByRole("link", { name: server.name })` both go ambiguous — the
  BMC link's `aria-label` ("Open BMC console for `<name>`") contains
  the plain name as a substring. Scope a row-level link query to
  `row.locator("td").first()` (the Name cell); scope a page-level one
  with `{ name, exact: true }`. **Any new element added inside a table
  row is a candidate for this — re-run `npx playwright test` locally,
  not just the unit/lint/build gate, before pushing a row change.**
- **The Architecture page (`/architecture`) embeds pre-generated static
  HTML**, never live data: `frontend/public/architecture/*.html`, built
  from `docs/diagrams/*.json` with the `archify` skill
  (`node bin/archify.mjs deliver <type> <spec>.json <output>.html
  --quality showcase`). Adding a collector's diagram means a new spec, a
  new entry in `features/architecture/diagrams.ts`, and delivering the
  HTML into `public/architecture/` — nothing in this repo's own build
  regenerates them. `docs/architecture.md`, "Architecture diagrams" has
  the full contract.
- The gate for any frontend change is
  `cd frontend && npm run lint && npm run typecheck && npm run test -- --run && npm run build`
  (`/gate --frontend`); E2E (`npm run test:e2e`) needs the backend and dev
  server running.
