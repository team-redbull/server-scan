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
