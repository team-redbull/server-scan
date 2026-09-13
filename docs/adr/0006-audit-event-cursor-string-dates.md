# ADR-0006: Audit event cursors compare dates as strings, matching how they're stored

## Status

Accepted

## Context

Every repository in this codebase persists documents via
`model.model_dump(by_alias=True, mode="json")`. Pydantic's `mode="json"`
serializes `datetime` fields to ISO 8601 strings — so `created_at` and
every other timestamp field is stored in MongoDB as a **string**, not a
native BSON `Date`. This has been true since slice 1 and is not something
this ADR changes; it's the existing, working convention every repository
in the codebase already follows.

The `audit_events` keyset cursor (`app.infrastructure.mongodb.
audit_event_repository`) is the first place that convention actually
mattered: its first implementation decoded a cursor's timestamp into a
Python `datetime` object and used that `datetime` directly as the operand
of a MongoDB `$lt` query against the stored (string) `created_at` field.

This is a cross-BSON-type comparison. MongoDB's comparison operators
order values by BSON type first, then by value within a type — a `Date`
and a `String` are different types, so `{"created_at": {"$lt": <a Date>}}`
against a document where `created_at` is stored as a `String` does not
compare the *values* the way it looks like it would. The bug did not
surface as an error: it silently returned wrong result sets. A test that
inserted 25 events and paginated through all of them at `page_size=7`
caught it — the second page returned zero items instead of the next 7,
because every event happened to share the same page-1 boundary
`created_at` (all inserted within the same millisecond in a tight loop),
and the mistyped `$or` clause matched nothing for any of them.

## Decision

The cursor's date component stays a **string** end to end — decoded from
the cursor, used directly as the `$lt`/equality operand in the Mongo
query, and re-encoded from the *raw stored string* (not a re-serialized
`datetime`) when building the next page's cursor. `_decode_cursor` still
parses the string with `datetime.fromisoformat()` once, but only to
validate that a client-supplied cursor contains a real timestamp — the
parsed result is discarded, never used in a query.

A second, related mismatch was caught in the same pass: reconstructing
the cursor from `item.created_at.isoformat()` (the *parsed* Pydantic
model's own `datetime.isoformat()`) renders a UTC offset as `+00:00`,
while Pydantic's JSON-mode serialization (what `record()` actually wrote
to Mongo) renders it as `Z` — two different strings that would stop
matching the stored value on the very next page. The fix reads the cursor
source directly from the raw Mongo document (`docs[-1]["created_at"]`)
rather than round-tripping through the parsed model and a second
serializer.

ISO 8601 strings with zero-padded fields and a `Z` suffix sort
lexicographically identically to chronological order — which is exactly
what makes staying in string form both correct and free; no conversion,
no native Date migration, no change to how any other repository persists
dates.

## Consequences

- `MongoAuditEventRepository` is the only repository in this codebase that
  performs a range comparison (`$lt`) against a string-stored date field.
  Any future repository that adds cursor- or range-based date filtering
  must make the same choice deliberately — compare as the stored type
  (string), not the domain type (`datetime`) — or hit this exact bug.
- This does **not** fix or even touch the underlying "dates are stored as
  strings, not BSON dates" choice across the rest of the codebase. That
  remains fine for every existing sort (`updated_at DESC` in
  `SERVER_INDEXES`, etc. — lexicographic ISO 8601 order equals
  chronological order) and fine for equality filters, but the same
  cross-type trap is waiting for any future code that builds a `$gt`/`$lt`
  query with a native `datetime` operand against any collection in this
  database. Worth a repo-wide sweep before any collection grows a
  date-range query that isn't a pre-decoded cursor.
- A TTL index (`expireAfterSeconds`) cannot be added to any of these
  timestamp fields without first migrating them to real BSON dates — TTL
  indexes require a native `Date` field. `events` has an optional,
  currently-disabled TTL index mentioned in the platform spec; enabling it
  will require that migration first.

## Update (2026-09-13): the same bug in the servers list cursor

The trap this ADR describes bit a second time, in `MongoServerRepository`:
sorting `/servers` by `updated_at` or `last_seen_at` returned a full first
page and an empty second one. The cursor is built from the last
`Server`'s attribute — a real `datetime` — round-tripped through the
cursor's `datetime` type tag, and put straight into `$gt`/`$lt` against
the ISO-string field. Confirmed live with 30 seeded servers (page 2:
`name` 10, `updated_at` 0, `last_seen_at` 0). `_cursor_position_clause`
now renders a `datetime` sort value through Pydantic's JSON serializer
before comparing, the way `fleet_snapshot` already did, and an
integration test pages both fields in both directions. The rule stands:
**any value compared against a stored timestamp must be the stored
string, never the parsed `datetime`.**
