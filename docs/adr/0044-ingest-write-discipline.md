# ADR-0044: Ingest writes with a revision check, and skips servers that did not change

Date: 2026-10-07
Status: Accepted

## Context

`IngestService._ingest_one` read a server, rebuilt the whole document in Python and wrote it back with a
blind `replace_one(upsert=True)`. Every other writer of a server (maintenance, reservation, reclassify,
health recalculate, OpenShift membership) already uses `upsert_with_revision_check`, a compare-and-set on
`revision`. Ingest was the one exception, so a write another actor made between ingest's read and its write
was silently overwritten with the older copy: a maintenance toggle, a reservation or a membership update lost
for good. The window is milliseconds per server, but the collectors (3 h) and the membership jobs (15 min)
run on independent schedules and overlap.

## Decision

1. **An existing server is written with a revision check.** `_ingest_one` is a bounded (`_MAX_WRITE_ATTEMPTS`
   = 3) read-build-write loop: a new server goes through `upsert`; an existing one through
   `upsert_with_revision_check(expected_revision=existing.revision)`. A `DuplicateKeyError` (a concurrent insert
   on `uniq_vendor_serial`, ADR-0026), a `RevisionConflictError` or a `NotFoundError` (the Mongo repository
   raises the latter when the document vanished, e.g. pruned, although the port documents the former) makes it
   log `ingest.write_conflict`, re-read, rebuild and try again. After the last attempt the exception propagates,
   `ingest()` counts the server in `IngestSummary.errors`, and the next run picks it up.
2. The same path serves `GET /servers/available`'s live recheck (`ingest_one`), so it no longer loses a
   concurrent maintenance or reservation write either.

## Consequences

- Nothing for an operator to do, and no configuration. A collector run can now report an error for a server it
  lost three races on; before, it reported success and lost the other writer's change.
- Audit events are emitted only after a successful write, from the `existing` the winning attempt read, so a
  retry never double-records a transition.
- The test `tests/integration/test_ingest_write_conflict.py` makes another writer win the first race and asserts
  its maintenance write survives and the revision ends at 3.

## Decision 2: a server whose content did not change is not rewritten

Re-ingesting identical input still changed every document: `revision`, `updated_at`, `last_seen_at`,
`listed_at`, `health.evaluated_at`, `classification.classified_at`, the counter
`classification.classification_version`, and, for Cisco servers, `connectivity.attachments[].last_seen`.
(Measured on 300 seeded servers, 2026-10-07.) Every run therefore replaced every document, bumped its
revision and threw away its Redis detail-cache key, although the content was identical.

- `ingest._stable_view(server)` is the JSON form without exactly those fields (`_VOLATILE_TOP_LEVEL` plus the
  three nested ones and the attachment stamps). Everything else is compared, including `unread_fields`,
  `reachable`/`unreachable_*`, `maintenance`, `reservation` and `openshift`, which are carried forward and so
  equal on both sides unless something really changed.
- If the new and stored stable views are equal, ingest does not replace the document. It calls
  `ServerRepository.touch_seen(id, expected_revision, fields)`: a `$set` of the "last confirmed" stamps
  (`last_seen_at`, `listed_at`, and `health.evaluated_at` / `classification.classified_at` /
  `connectivity.attachments.$[].last_seen` when those are produced) conditional on the revision. `revision`,
  `updated_at` and `classification_version` do not move, and no transition audit event is emitted (none was
  due: `installation_type` and `health.overall` are in the compared content). A touch that matches nothing
  rebuilds through the same bounded loop as a revision conflict.
- **Live readings are refreshed, not compared** (2026-10-08). Dell (via OpenManage's Redfish pass) and UCS
  report a PSU's input watts and a GPU's temperature and power draw, which differ on every read; compared, they
  made 99% of OpenManage and 86% of UCS Central servers "changed" each run. `_stable_view` drops
  `hardware.gpus[].temperature_celsius`/`power_watts` and `hardware.power.psus[].power_watts`, and `_seen_fields`
  writes the fresh values into the stored document by index on every skipped run (equal views mean the same
  GPUs and PSUs in the same order). They are display-only: no health policy reads them.
- **Which field flips is logged.** `ingest.completed` carries `top_changed_paths`, the ten most common leaf
  paths (list indexes shown as `[]`) that made an existing server count as changed. If a collector's unchanged
  share is still low, that line names the field; add it to the volatile set or fix its mapping.
- **Default is to write.** A field that is not on the volatile list and differs forces the full write, so a new
  per-run stamp added to `_build_server` costs the optimisation, never a lost update.
  `tests/integration/test_ingest_skip_unchanged.py` re-ingests the whole fake fleet twice with the engines on
  and fails if anything is "updated"; `tests/unit/application/services/test_ingest_stable_view.py` pins which
  fields are ignored and which are seen.
- `IngestSummary.unchanged` and the `ingest.completed` log line report how many were skipped. `updated` now
  means "content changed and was written"; `ManagerRun.servers_updated` (stored) keeps meaning "existing
  servers ingested", so it is `updated + unchanged`.

### Consequences of decision 2

- `revision` and `updated_at` now mean "the content last changed", not "an ingest run touched it".
  `classification_version` counts real reclassifications instead of runs. `last_seen_at`, `listed_at`
  (prune) and the staleness gauges are unaffected: they are still refreshed on every run.
- The inventory's "Updated ..." time (`generated_at` of `GET /servers/rows`) is now the newest of `updated_at`
  and `last_seen_at`, so it still advances when a collector confirms an unchanged fleet.
- A cached server detail (60 s TTL, keyed by revision) can show a `last_seen_at` / `health.evaluated_at` up to
  60 s old after a touch, where it used to be invalidated by every run. In exchange its key now survives
  between runs.
- Writes, oplog volume and index updates drop by the share of unchanged servers; the log line says how many.
  Expect most of the fleet to skip, but this is measured only on the fake fleet until the first real runs.
- **A document written before a field existed is rewritten, not touched.** Both sides of the comparison go
  through the `Server` model, so a stored document missing a newly added defaulted field would compare equal
  and never be back-filled (it would stay unchanged in Mongo and be invisible to raw `$match`/projection
  readers). `_has_unset_fields` checks `model_fields_set` recursively: current code stores every field, so a
  field that validation had to default means the document is older than the model, and it gets the full write.
  No migration is needed when a field is added.
- **A CAS writer can regress a touched stamp by up to one run.** Maintenance, reservation, membership and the
  reclassify/health endpoints replace the whole document on `revision`, which `touch_seen` does not move, so
  they can write back the previous `last_seen_at`/`listed_at`/`health.evaluated_at`. Bounded and self-healing
  (the next run touches them again); no content is lost and the prune cutoff is days wide.
- `ManagerRun.servers_unchanged` is stored (additive, default 0) and exported as
  `server_scan_collector_last_run_servers_unchanged` with a dashboard row (ADR-0029, 2026-10-07 update), as well as
  in the console line and the `ingest.completed` log. A run recorded before the field existed reads 0 until
  the collector next runs, which looks like a full rewrite on the dashboard.
- The detail page's `updated_at` field is now labelled "Last changed".
