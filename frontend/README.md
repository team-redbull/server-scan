# Server Scan — Frontend

Vite + React 19 + TypeScript (strict) SPA for the inventory platform. See
the repo root `README.md` for the full local-dev workflow and `docs/` for
architecture notes.

Not a Next.js app on purpose: this is an internal dashboard with no SEO or
SSR requirement, so a static Vite build is the simplest, most portable
air-gapped deployment target — `npm run build` produces static assets
served by `Containerfile` (UBI9 nginx), with no Node runtime in production.

## Scripts

```bash
npm run dev         # dev server with HMR, proxies /api,/health,/metrics to :8080
npm run build        # type-check (tsc -b) + production build
npm run typecheck    # type-check only
npm run lint          # oxlint, type-aware rules enabled (.oxlintrc.json)
npm run test          # vitest (excludes e2e/ — see playwright.config.ts)
npm run test:e2e      # Playwright — needs the dev stack, backend, and this dev server all running
```

## Stack notes

- **TanStack Query v5** for server state (`src/lib/query-client.ts`);
  `src/api/client.ts` is a thin `fetch` wrapper that parses the backend's
  RFC 9457 problem-details error envelope into a typed `ApiError`.
- **Tailwind v4** via `@tailwindcss/vite` — no separate PostCSS config.
- **react-router** with `createBrowserRouter`/`RouterProvider`.
- **oxlint** (Rust-based, type-aware) rather than ESLint — this is Vite's
  own current scaffolding default and is materially faster; type-aware
  rules are enabled per oxlint's own production recommendation (see
  `.oxlintrc.json`).
- Path alias `@/*` → `src/*` (`tsconfig.app.json`, mirrored in
  `vite.config.ts`).
