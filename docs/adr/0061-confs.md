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
  entry, a scan id (`CONF-21`); such an *unmapped* conf has `part_number` `null` (and the writer's
  name `null`). The writer
  calls `conf_id` its unique key.
- `cpu` (`model`, nullable `vendor`, `sockets`, `cores_per_cpu`, `total_cores_ht`, `clock_mhz`,
  `max_clock_mhz`, `cache_kb.{l1,l2,l3}`), `ram` (`total_gib`, `dimms`, `per_dimm_gb`, `type: string[]`;
  `total_gib` 0 means the memory scan failed, `type` `[]` that the DIMM read failed).
- `disks[]` are `{count, type, capacity_tb}`: no drive model or protocol (third schema, below). `type` is a
  plain string (`NVME` in the sample; earlier schemas also showed `SSD`, `HDD` and `"unknown"`).
- `gpus[]` are `{count, model}`, **grouped by model** (`{"count": 8, "model": "NVIDIA H200 SXM"}`), and `[]` when
  there is none; `gpu_count` is the total and `gpu_vram_gb` the VRAM per GPU (`null`, absent or `0` on a GPU-less
  conf; the writer's real documents store `0` beside a `gpu_count` of 0, operator 2026-11-12). The UI still sums
  entries per model, so the second schema's one-entry-per-unit list shows the same.
- `adapters[]` are `{count, ports, speed}`: no adapter model. `speed` is read as **Gb/s per port** (the sample's
  `100` beside `ports: 2`; the writer does not name the unit, so this is an assumption to confirm). `adapter_count`
  is the writer's total, shown as stored, never checked against the list.

**Revised 2026-11-12** (a second photographed schema): `name` became `conf_name`, `gpu_vram_gb` is new,
`ram.total_gib` 0 is a failed scan, `NVMe` is a disk `type`, and the earlier `server_count` is gone. Both
photographs show the cache keys as `11`/`12`/`13`, which in that font may be `l1`/`l2`/`l3`, so both
spellings are read (Pydantic `AliasChoices`) and the API always returns `l1`/`l2`/`l3`.

**Revised again 2026-11-12** (a third photographed schema, the sample `CONF208`, a PowerEdge XE9680): disks lost
`model` and `protocol`, adapters lost `model` and gained `ports` and `speed`, GPUs are grouped by model, and the
document has no `conf_name` and no `{"detected": [...]}` wrapper (one document per conf, as assumed). A stored
disk `model`/`protocol` or adapter `model` is ignored; an adapter without `ports` or `speed` (the second schema's
`{count, model}`) makes its document unreadable, which is what ADR consequence "the writer changes the shape" means.

## Decision

1. **Server Scan only reads `confs`.** Nothing in the platform creates, edits or deletes a conf; the writer is
   outside it. `GET /api/v1/confs` and `GET /api/v1/confs/{conf_id}` are mounted like every authenticated
   read (any role, ADR-0034). Adding writes later means an admin-only route and an audit event, and is a new
   decision.
2. **The stored shape is reproduced field for field** (`app.domain.models.conf.Conf`), its inconsistencies
   included (`total_gib` vs `per_dimm_gb`), **except `server_count` and the name (`conf_name`, earlier
   `name`), which are not read** (operator, 2026-11-12; `server_count` is also gone from the revised schema):
   `Conf` has no such fields, so a stored one is ignored, not returned, and never makes a document
   unreadable. Any other unknown key is ignored the same way. Vocabulary fields (`disks[].type`, `ram.type`) are plain
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
6. **The UI** (`/confs`, "Confs" after "Servers") filters and sorts client-side, sums the GPU list
   per model (or shows `gpu_count`× unknown when the list is empty), shows `gpu_vram_gb` per GPU, an adapter group as
   `2× 2-port 100 Gb/s` and a disk group as `2× 1.92 TB NVME`, omits the GPU line and VRAM entirely when there is no GPU (a `0` is never
   shown as "GPUs (0)" or "0 GB VRAM"), and renders
   `"unknown"`, an unread RAM type and a `total_gib` of 0 ("not read (scan failed)") as muted values, not
   blanks or zeros. The Part number column carries an "Unmapped" badge for a conf with no
   `part_number`.
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
- The first two photographed samples were wrapped as `{"detected": [ ... ]}`; the third is a bare document, which
  confirms the assumption that each conf is its own document. A single document holding a whole `detected`
  array would still be listed as one unreadable `"?"`.
