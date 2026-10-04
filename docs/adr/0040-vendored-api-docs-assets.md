# ADR-0040: `/docs` and `/redoc` serve their JavaScript from the image

Status: accepted (2026-10-04)

## Context

FastAPI's default Swagger UI and ReDoc pages load their bundles from
`cdn.jsdelivr.net` (and ReDoc loads Google Fonts). The estate is air-gapped,
so both pages rendered blank: reachable HTML, unreachable script.

## Decision

The bundles are vendored in `backend/app/static/docs/` (versions in its
README) and mounted at `/api/docs-assets`. That prefix is already forwarded by
the frontend nginx proxy, so no new `location` is needed. `/redoc` is our own
route (`get_redoc_html(with_google_fonts=False)`), since the `FastAPI`
constructor has no switch for the fonts. The mount is unauthenticated, like
`/docs` itself. `/openapi.json` is generated from the routes at first request,
so it always matches the running image.

## Consequences

About 2.9 MB added to the image. Bundle versions no longer float; refreshing
them is a manual re-download.
