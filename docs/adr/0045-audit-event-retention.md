# ADR-0045: Audit events are deleted after a retention window, report-only first

Date: 2026-10-07
Status: Accepted

Extends ADR-0006 (string dates) and ADR-0037 (the prune CronJob pattern). Supersedes the
"no delete exists" claim in `MongoAuditEventRepository`'s old docstring.

## Context

`audit_events` grows without bound: ingestion, maintenance, reservations and `SERVER_PRUNED` all
append, and nothing ever removes a row. The operator asked (2026-10-07) for a retention window.

## Decision

- **A weekly CronJob, `collectors.auditRetention` (`tools.prune_events`), deletes every event whose
  `created_at` is older than `now - INVENTORY_AUDIT_RETENTION_DAYS`** (default 180). The window applies
  to **every event type**, `SERVER_PRUNED` included, which is the operator's call: no exception, no
  export or archive.
- **Not a Mongo TTL index.** `created_at` is an ISO-8601 *string* (ADR-0006) and a TTL index needs a BSON
  date, so the job compares strings: `{"created_at": {"$lt": <cutoff>}}` with the cutoff rendered by the
  repository's `_iso()` (never `.isoformat()`: `+00:00` versus the stored `Z` mis-sorts). The existing
  `created_at_id` index serves the sort and the range.
- **Report-only first.** `INVENTORY_AUDIT_RETENTION_REPORT_ONLY` defaults to true (Helm
  `collectors.auditRetention.reportOnly: true`): the job logs `audit_retention.report_only` with
  `would_delete`, `oldest`, `newest`, `cutoff`, deletes nothing and writes no event. The operator reads
  that line, then sets `reportOnly: false`. `--apply` on the CLI also applies.
- **What the first real run deletes:** every event older than `now - 180 days`, of any type, at most
  `batch_size * max_batches` (default 5000 * 200 = 1,000,000) per run. A bigger backlog is finished by the
  next weekly run (the event's `truncated` is true until then).
- **`INVENTORY_AUDIT_RETENTION_DAYS=0` keeps everything** (the job logs and exits 0); otherwise the value
  must be at least 7, and `purge_before` itself refuses a cutoff inside the last 7 days, so a typo cannot
  empty the log.
- **The deletion is itself audited.** After a purge that deleted anything, one `AUDIT_PURGED` event is
  recorded (actor `SYSTEM`/`audit-retention`, no `server_id`; `data`: `cutoff`, `retention_days`,
  `deleted`, `batches`, `truncated`, `oldest_deleted_created_at`, `newest_deleted_created_at`,
  `reason`). It is written *after* the delete, so a failed purge never claims one happened; a run that
  deletes nothing writes none.
- **Batches.** Ids are read sorted by `(created_at, _id)`, then `delete_many` on those ids *and* the cutoff
  filter. The cutoff is the only filter parameter: no type or server filter exists to misuse.

## The immutability exception

Audit events were "immutable by construction": the repository exposed only `record()`. Now
`purge_before` exists, as the second documented exception beside `rename_legacy_event_types`. It is
reachable only from `tools/prune_events.py`, never from a request path:
`tests/unit/infrastructure/test_audit_purge_isolation.py` asserts nothing under `app/api`,
`app/application` or `app/domain` references it, and the structural repository test lists the methods.

## Consequences

- A viewer's or auditor's Events page and a server's History tab lose events older than the window.
- Pre-check before the first real run: legacy rows whose `created_at` does not end in `Z` would compare
  wrongly. `db.audit_events.countDocuments({created_at: {$not: /Z$/}})` should be 0 (deploy/README.md).
- The job's log lines and the `AUDIT_PURGED` event are the first signal. Since 2026-10-07 the trail's size and
  oldest event are gauges with dashboard panels, and `ServerScanAuditRetentionBehind` fires once `reportOnly` is
  false and the oldest event outlives the window by the slack (ADR-0029, 2026-10-07 update).
- Backups are out of scope; deleted events are gone.

## Notes from review (2026-10-07)

- **A failed purge still leaves a record.** `purge_before` updates a `PurgeProgress` after every batch; if a
  later batch raises (a Mongo failover, the job's `activeDeadlineSeconds` kill), the tool records the
  `AUDIT_PURGED` event for what was already deleted with `complete: false` before re-raising and exiting 1.
- **`AUDIT_PURGED` ages out like every other event.** It is not exempt: the operator asked for one retention
  window for everything. The `audit_retention.purged` log line is the longer-lived record. To keep purge
  records longer, exclude `event_type: AUDIT_PURGED` in `purge_before`'s query.
- **Only `tools/` may reference the purge.** A unit test fails if any file under `backend/app` other than the
  repository mentions `purge_before`, `preview_before` or `PurgeProgress`.
- `INVENTORY_AUDIT_RETENTION_MAX_BATCHES` must be at least 1; a legacy `created_at` that is not an ISO `Z`
  string is never matched (BSON type bracketing), so it fails safe: check
  `countDocuments({created_at: {$not: /Z$/}})` is 0 before the first apply.
