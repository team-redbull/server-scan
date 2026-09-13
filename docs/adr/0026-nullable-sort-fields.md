# ADR-0026: nullable sort fields, and the index migration that was left manual

Date: 2026-09-10
Status: Accepted

Extends `docs/adr/0007-scale-verification-and-request-coalescing.md`, which
established keyset pagination, and `docs/adr/0024-openshift-cluster-
membership.md`, which added the two nullable fields this is about.

## Context 1: sorting the inventory by cluster or MCE

`Server.openshift.cluster_name` and `.mce_name` are `str | None` by
design — a server no cluster holds has no cluster name, and ADR-0024
records why that is `None` rather than `""`. Sorting the inventory on
either therefore means sorting on a nullable field, which every prior
sort field avoided: `name_normalized`, `model_normalized` and
`identity.serial_normalized` are always-present strings precisely so this
question never came up.

**MongoDB's range operators are type-bracketed.** Measured on 6.x:

| query | matches | why it matters |
|---|---|---|
| `{f: {$gt: null}}` | **nothing** | not "everything after null" |
| `{f: {$lt: "abc"}}` | strings only | **skips every null** |
| `{f: {$ne: null}}` | all strings | the usable form |

Sort order is separate and does span types: ascending puts every null
first, then strings; descending reverses it.

So the two disagree. `_cursor_position_clause` built
`{$or: [{f: {$gt: v}}, {f: v, _id: {$gt: id}}]}`, which is correct only
while `f` is never null. On a nullable field it strands rows silently —
descending, `$lt` excludes every null, so all of them vanish from the
walk with no error anywhere. Keyset pagination fails quietly by
construction: the caller gets a shorter list, not an exception.

## Decision 1

`CursorPosition.sort_value` becomes `str | datetime | None`, carrying its
own `_TYPE_NULL` tag rather than encoding `None` as `""` — null and the
empty string sort to different places, so conflating them reintroduces
the bug in a harder-to-see form.

`_cursor_position_clause` gains two null-aware branches:

| direction | position | clause |
|---|---|---|
| asc | null | `{f: {$ne: null}}` OR tie on `_id` |
| asc | string | `{f: {$gt: v}}` OR tie — nulls are already behind |
| desc | string | `{f: {$lt: v}}` OR **`{f: null}`** OR tie |
| desc | null | tie on `_id` alone — nothing sorts after null |

The `{f: null}` leg descending is the whole fix: type bracketing leaves
nulls out of `$lt`, and descending they are exactly what remains.

Verified over 600 seeded servers (181 null clusters, 385 null MCEs), page
size 23, both directions: every walk returned all 600 exactly once in the
correct order. An integration test pins it.

`cluster_name` and `mce_name` join `SORT_FIELDS`, backed by the
`openshift_cluster_name_id` and `openshift_mce_name_id` compound indexes
that already existed.

## Context 2: `uniq_system_uuid` kept rejecting servers

The same day, a collector run against a real deployment reported
`DuplicateKeyError ... index: uniq_system_uuid` for servers across four
vendors. That index was made non-unique and renamed to `system_uuid` on
2026-09-09, and `indexes.py` carried a comment saying so — while also
noting that `_create_indexes` reconciles on *name*, so the rename would
leave the old unique index in place on any already-deployed database, and
that an operator would have to drop it by hand.

Nobody did. The declaration said "not unique" and the database went on
enforcing uniqueness, exactly as the comment predicted, for as long as it
took someone to notice failing collector runs.

## Decision 2

`RETIRED_INDEXES` lists index names this file no longer declares, and
`ensure_indexes` drops them before creating the current set, logging
`mongo.index_retired`. An absent index is the normal case and is not an
error.

Renaming or removing an index now means adding its old name to that list,
and the migration happens on the next process start with no operator step.

## Consequences

- Any nullable field can now be a sort key. The cursor change is general;
  nothing about it is specific to these two fields.
- A retired index is dropped by whichever process starts first — the API
  or a collector — so there is a brief window on upgrade where neither
  index exists. Both of these are non-unique lookups, so the cost is a
  slower query, not a failure.
- `RETIRED_INDEXES` grows forever and is never safe to prune blindly: an
  entry can only be removed once no deployment could still be carrying
  that index.
- **The general rule, this session's third instance of it:** a change that
  is correct on a fresh database is not automatically correct on an
  existing one. Narrowing a persisted enum (ADR-0024's correction) and
  renaming an index both shipped green because every test ran against a
  database the new code had just created.

## Update (2026-09-13): index design notes

Moved here from `app.infrastructure.mongodb.indexes`'s comments, since
this ADR is where the index-reconciliation rule already lives. The
module docstring keeps the two big ones (why every `servers` compound
index ends in `_id`, and why `system_uuid` is not unique); these are the
smaller facts that each cost a real finding.

- **Partial-filter expressions have no `$ne`.** MongoDB allows only
  `$eq`, `$exists`, `$gt`/`$gte`/`$lt`/`$lte`, `$type` and `$and` of
  those in a `partialFilterExpression`. `uniq_vendor_serial` therefore
  spells "non-empty string" as `{"$gt": ""}` — every non-empty string
  sorts lexicographically after `""`. And `system_uuid` uses
  `{"$type": "string"}` rather than `{"$exists": true}` because
  `$exists` is true for a field that is *present and null*, which
  `model_dump(mode="json")` always emits (ADR-0016 has the original
  finding). That filter is now purely a size optimisation — it keeps
  every UUID-less server out of the index — rather than what stops a
  null-keyed collision, since the index stopped being unique on
  2026-09-09, but it is still worth having.
- **`last_seen_at` shipped as a single-field index with no `_id`
  tiebreak**, unlike every other entry, so an unfiltered
  `sort=last_seen_at` request fell back to a full `COLLSCAN` plus a
  blocking in-memory sort at 10k+. Caught by `tools/verify_indexes.py`
  running `.explain()` against a real 50k-document collection — fixture-
  sized data never exposed it, because the planner happily picks a
  `COLLSCAN` over a barely-selective index at low document counts anyway
  (ADR-0007 §1). The rule it left behind: every `SORT_FIELDS` entry needs
  a `(field, _id)` index for the unfiltered case, on top of the per-filter
  compound indexes.
- **The rule and policy compound indexes mirror the engines' in-memory
  sort orders.** `enabled_policy_key_priority_order_id` is exactly the
  family-resolution order `health.evaluate.resolve_families`/
  `_family_sort_key` applies, and `enabled_priority_order_id` is
  `classification._sort_key`'s order minus the specificity tiebreak an
  index cannot express — so "load all enabled" is one ordered `IXSCAN`
  end to end and the evaluator's own sort runs over an already-ordered
  stream. The single-field `scope.*`/`policy_key`/`category` indexes back
  admin filtering ("every rule scoped to this site"), not resolution.
- **`audit_events` is unbounded and append-only** — it grows for the
  deployment's lifetime and every read is "most recent N, optionally
  filtered" — so all four of its indexes end in `(created_at DESC, _id
  DESC)`, the keyset pagination's fixed sort (ADR-0006). Global feed, one
  server's history, one event type and one actor's history are each an
  `IXSCAN`, never an in-memory sort.
- **`openshift_cluster_name_id` is also the membership jobs' working
  set** (ADR-0024): each run reads every server naming its cluster to
  free the ones the cluster stopped listing, from every cluster at once,
  every 15 minutes. `source_provider_last_seen` is the fleet gauges'
  staleness query (ADR-0029).
