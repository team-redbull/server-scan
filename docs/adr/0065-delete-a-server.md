# ADR-0065: An admin can delete one server, and an in-use one only with `force`

**Status:** Accepted, implemented 2026-11-19.

## Context

The only way a server left the database was `tools.prune_servers` (ADR-0037): automatic, and only for a server its
manager stopped listing. Hardware that was retired, a duplicate or a wrongly created record stayed until someone
edited MongoDB by hand. The operator wants a button and an API route.

## Decision

### `DELETE /api/v1/servers/{id}[?force=true]`, admin only

`ServerDeleteService` (`app/application/services/server_delete.py`) reads the server, **refuses with 409
`SERVER_DELETE_BLOCKED` while it is in use**, records the audit event, deletes the document, and the route drops the
detail and list caches (ADR-0028). 204 on success, 404 for an unknown id (also a second delete).

- **"In use" is three checks, all listed** in `details.blockers` (one human sentence each): `openshift.lifecycle_state`
  is anything but `AVAILABLE` (so `INSTALLED` and `INSTALLED_TO_INVENTORY`, with the cluster named), a **live**
  reservation (an expired one is left on the document and does not block, ADR-0035), and maintenance enabled.
  Deleting such a server loses the very state that made it worth keeping, and a collector run would recreate it
  without it.
- **`force=true` skips the checks.** It is the same admin write, not a second permission. The audit event says what
  was overridden, so a forced delete is always attributable.
- **The delete is atomic with the check**: `ServerRepository.delete(id, expected_revision=)` matches the revision the blockers were read at
  (ADR-0044), so a reservation, maintenance or membership write made in between is re-read and re-checked (3 attempts, then 409).
- **Audited as `SERVER_DELETED`** (an existing, previously unused event type) **after** the delete succeeds (a failed or lost delete
  never leaves a false event; a failed audit write is logged with the full snapshot as `server.delete_audit_failed`, the delete stands), with the server's
  name, serial and BMC snapshotted onto the event and `data` = `vendor`, `source_provider`, `forced`, `overridden`.
  `SERVER_PRUNED` stays the automatic job's event.
- **A server its manager still lists comes back** on the next collector run, as a new document with no maintenance,
  reservation or membership. There is no ignore list; the UI says so. This is for hardware that is really gone.

### UI

A **Remove** button in the detail header (admin only; greyed with a title for a viewer or auditor, like "Fetch now").
Its dialog asks for the server's name. The API, not the client, decides whether it is in use: a 409 shows the
blockers and a "Remove anyway" checkbox, and the second request carries `force=true`. Success returns to `/servers`.

## Consequences

- No new setting, chart value or migration. `ServerRepository` (the port) gains `delete`, which the Mongo repository
  already had.
- `SERVER_DELETED` appears in the Events filter and reads "Deleted by hand" (plus the overridden reasons).
- Known, shared with every operator write (ADR-0028): a detail or list read that began before the delete can refill the cache
  afterwards; the detail pointer lives 60 s, so a deleted server may read 200 for up to that long.
- Not done: a bulk delete, a soft delete / undo, and an ignore list for a server that must not return.
