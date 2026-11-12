# ADR-0061: Confs — server configurations read from a `confs` collection

**Status:** Accepted, implemented 2026-11-11.

## Context

A *conf* is a server specification: a hardware model, its CPUs, memory, drives, GPUs and network
adapters. The operator wants the confs listed in Server Scan as
a tab next to Servers. They are stored in a dedicated `confs` collection in the same database as the
inventory (`INVENTORY_MONGO_DB`).

The document shape was given by the operator. It is the shape of whatever produces confs, not one Server
Scan designed:

- `conf_id` is a catalog id (`CONF200`, a revision `CONF200.2`) or, for a configuration with no catalog
  entry, a scan id (`CONF-21`); such an *unmapped* conf has `name` and `part_number` `null`.
- `cpu` (`model`, nullable `vendor`, `sockets`, `cores_per_cpu`, `total_cores_ht`, `clock_mhz`,
  `max_clock_mhz`, `cache_kb.{l1,l2,l3}`), `ram` (`total_gib`, `dimms`, `per_dimm_gb`, `type: string[]`, `[]`
  when the DIMM read failed), and a `server_count`.
- `disks[]` are `{count, model, capacity_tb, type, protocol}`; `"unknown"` is a real value of `model`,
  `type` (`SSD`/`HDD`) and `protocol` (`PCIe`/`SATA`) when the scan did not record it.
- `gpus[]` are `{count, model}` with **one entry per scanned unit** (`count` 1), not grouped; `gpu_count` is
  the total. `adapters[]` are `{count, model}`; `adapter_count` is the total.

The photographed schema's cache keys read `11`/`12`/`13`; they are taken to be `l1`/`l2`/`l3`.

## Decision

1. **Server Scan only reads `confs`.** Nothing in the platform creates, edits or deletes a conf; the writer is
   outside it. `GET /api/v1/confs` and `GET /api/v1/confs/{conf_id}` are mounted like every authenticated
   read (any role, ADR-0034). Adding writes later means an admin-only route and an audit event, and is a new
   decision.
2. **The stored shape is reproduced field for field** (`app.domain.models.conf.Conf`), its inconsistencies
   included (`total_gib` vs `per_dimm_gb`), **except `server_count`, which is not read for now** (operator,
   2026-11-12): `Conf` has no such field, so a stored one is ignored, not returned, and never makes a document
   unreadable. Vocabulary fields (`disks[].type`, `protocol`, `ram.type`) are plain
   strings, not enums, so a new value from the writer is shown rather than rejected.
3. **A conf is looked up by its `conf_id` field, and `_id` is never read** (`{"_id": 0}` on every `find`): the
   writer's `_id` may be an ObjectId or the id itself. The index on `conf_id` is **not unique**, so it can never
   reject the writer's insert, and it is **best-effort**: it carries MongoDB's default name (`conf_id_1`), and if
   the writer already holds an index on those keys, or this user may not create one, startup logs
   `mongo.foreign_index_skipped` and continues. The drop-and-recreate path every other collection gets is never
   applied to `confs`, so a writer's own (e.g. unique) index is never removed.
4. **One malformed document does not fail the list.** It is skipped, logged (`confs.unreadable_document`) and
   named in the response's `unreadable` list, which the UI shows as a banner. Fetching that one conf
   answers 500, not 404: it exists.
5. **No pagination and no cache.** The collection holds tens to hundreds of documents; the list is read whole
   and sorted naturally by `conf_id` (`CONF9` before `CONF10`, `CONF200.2` after `CONF200`). Without a cache,
   an external write shows on the next request, with nothing to invalidate.
6. **The UI** (`/confs`, "Confs" after "Servers") filters and sorts client-side, groups the per-unit GPU list
   by model, and renders `"unknown"` and an unread RAM type as muted values, not blanks. A conf with no `name`
   carries an "Unmapped" badge.
7. **Fake data** (convention 10): `tools.seed_inventory` upserts 20 fixed confs by `conf_id`
   (`app.infrastructure.providers.fake.confs`), built from the fake fleet's own models and CPUs; the same
   list for every `--seed` and `--count`. Each is replaced whole, so a re-seed neither duplicates a conf nor
   keeps a field `Conf` dropped.

## Consequences

- A conf is not linked to a server: no server field names its conf, so the UI cannot say which or how many
  servers have one. Showing the writer's `server_count` again is a field on `Conf` and a column; linking a conf
  to servers needs a stored field on one side.
- A deployment that runs the fake collector *and* a real conf writer would mix the 20 fake confs into the real
  ones; the hub runs only the fake collector today.
- Duplicate `conf_id`s are listed as they are stored; `GET /confs/{conf_id}` returns one of them.
- If the writer changes the shape, the changed documents appear in `unreadable` until `Conf` follows.
