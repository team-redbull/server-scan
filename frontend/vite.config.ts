import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import path from "node:path";
import { configDefaults, defineConfig } from "vitest/config";

// https://vite.dev/config/
export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: {
      // import.meta.dirname (native ESM), not __dirname (CJS-only shim) —
      // Vite's native config loader is dropping support for the latter.
      "@": path.resolve(import.meta.dirname, "./src"),
    },
  },
  server: {
    proxy: {
      // Everything except the SPA's own routes goes to the backend in dev.
      // `/health` is deliberately a `^`-prefixed regex (Vite's syntax for
      // "treat this key as a RegExp") anchored to a trailing slash, not a
      // plain prefix match: a plain "/health" prefix would also swallow
      // the SPA's own `/health-policies` client routes (slice 5) and
      // proxy them to the backend's unrelated `/health/live`+`/health/
      // ready` liveness endpoints, breaking a hard refresh on those pages.
      "/api": "http://localhost:8080",
      "^/health/": "http://localhost:8080",
      "/metrics": "http://localhost:8080",
      // The MCP server (`python -m tools.mcp_server --http`, port 8091 here; ADR-0054).
      "/mcp": "http://localhost:8091",
    },
  },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./src/test/setup.ts"],
    // `e2e/` holds Playwright specs (own runner, own `*.spec.ts` files —
    // see playwright.config.ts) which collide with Vitest's default
    // include glob and fail under Vitest's runner ("did not expect
    // test.describe() to be called here"). Vitest's own default excludes
    // (node_modules, dist, etc.) stay implicit; this adds the one project-
    // specific exclusion on top rather than replacing them.
    exclude: [...configDefaults.exclude, "e2e/**"],
  },
});
