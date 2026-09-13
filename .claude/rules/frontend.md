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
- **Maintenance is switched only from the inventory list** (per-row
  switch); the detail page shows it read-only. One place, on purpose.
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
