# ADR-0002: RFC 9457 Problem Details for API errors

## Status

Accepted

## Context

Every API needs one consistent error shape so clients (the UI now, other
automation later) never need endpoint-specific parsing. The two live
options are a bespoke JSON envelope, or the IETF's standardized format.

## Decision

Use [RFC 9457](https://www.rfc-editor.org/rfc/rfc9457) "Problem Details for
HTTP APIs", served as `application/problem+json`:

```json
{
  "type": "/problems/revision-conflict",
  "title": "Revision Conflict",
  "status": 409,
  "detail": "The record was modified since you last read it.",
  "instance": "/api/v1/servers/srv_abc123",
  "code": "REVISION_CONFLICT",
  "request_id": "req_...",
  "details": {"current_revision": 5}
}
```

`type`/`title`/`status`/`detail`/`instance` are RFC 9457's fixed members.
`code`, `request_id`, and `details` are RFC 9457 "extension members" —
explicitly permitted by the spec — giving this platform's own clients a
stable, machine-matchable code that doesn't shift if `detail`'s wording is
edited. `type` is a relative reference (`/problems/<slug>`) rather than a
dereferenceable absolute URL, which the RFC allows and which fits an
air-gapped platform with no public domain for it to resolve against.

Implemented by hand in `app.errors` / `app.exception_handlers` rather than
via the one community package for this (`fastapi-problem`) — the format is
a handful of fields, not worth an extra dependency in an air-gapped
mirror.

## Consequences

- Every `AppError` subclass and the built-in FastAPI/Starlette exception
  types funnel through one renderer (`app.exception_handlers`); no route
  handler builds its own error body.
- `ErrorCode` is an append-only registry (`app/errors.py`) — codes are
  never renumbered or removed once shipped, since clients may match on
  them.
- Frontend error handling has one shape to parse everywhere, including for
  errors FastAPI/Starlette raise internally (404 route-not-found, 422
  validation) — those are also normalized into the same envelope.

**Addendum, 2026-09-06:** `GET /health/ready` (`app.api.health`) is a
deliberate exception to "every error funnels through one renderer" — its
body is a plain `dict[str, object]` (`{"status": ..., "dependencies":
{...}}`), never a problem-details envelope, even on its 503. This is
intentional, not an oversight: it's consumed by infrastructure (container
orchestrator readiness probes), not this platform's own API clients, and
`/health/live`/`/health/ready` are already deliberately unversioned and
outside `/api/v1` for the same reason (see that module's own docstring).
An orchestrator probe reads an HTTP status code and, at most, a `status`
field — never `type`/`code`/`details` — so there is no client this ADR's
consistency goal is protecting here, and shaping the body as problem
details would add nothing a probe reads while still needing bespoke
handling to build (it isn't raised as an `AppError`).

**Addendum, 2026-11-09 — every string in an error body is bounded.** The
renderer cuts `detail`, and every string inside `details` however deeply
nested, to `MAX_ERROR_TEXT_CHARS` (1,000) characters, ending a cut one with
`… [N more characters cut from this response; see the server log for
request_id <id>]` and logging `request.error_detail_truncated` with the full
length. The 503 for a failed live recheck also bounds the vendor's exception
text at its source (`recheck_failure_reason`, 500 characters; the full text
is in the `available.recheck_failed` log line). Why: callers copy `detail`
into their own errors. The workflows' add-server-to-mce run failed with
Temporal's "Failure exceeds size limit." — Temporal replaces any activity
failure over 2 MiB with that message, non-retryable — because its
server-scan client copied an error body over 2 MiB whole. That body did not
come from `main`'s 404 (counts per reason, bounded), but nothing stopped an
`AppError` built from external text, such as a vendor exception, from
growing that large. The client now cuts what it copies as well; this is the
same rule at the source. A short `detail` is unchanged, so no caller that
matches on it sees a difference.
