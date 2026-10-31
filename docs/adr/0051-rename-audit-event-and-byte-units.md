# ADR-0051: A rename is its own audit event; sizes are shown as decimal TB (storage) and GiB (memory, VRAM)

Date: 2026-11-01
Status: Accepted

Refines ADR-0047 (audit events) and the health policy message templates of ADR-0005.

## Context

1. A server renamed by its manager left no trace unless the rename also flipped its installation type
   (`CLASSIFICATION_CHANGED` carried `name_from`/`name_to` only then).
2. The default policy `storage.name_capacity_mismatch` rendered `5000000000000 bytes of storage, but the name
   promises 10000000000000` into the health reasons and so into the audit trail. The Hardware tab divided by 1024
   but labelled the result KB/MB/GB/TB.

## Decision

- **`EventType.SERVER_RENAMED`**, written by `IngestService._emit_transition_events` whenever the stored name
  differs from the ingested one; `data` is `{name_from, name_to}`, `server_name` the new name. It is independent of
  `CLASSIFICATION_CHANGED`, which keeps its own `name_from`/`name_to` (no stored shape changes).
- **Units:** storage is **decimal TB/GB** (drives are sold that way and the `-<N>tb` name token is parsed as
  `N * 10**12`); memory and GPU VRAM are **binary GiB** (MiB below 1 GiB). Stored values stay raw bytes.
- **Template format specs `tb`, `gib`, `tib`** join `.Nf`, `d`, `,d` in `health/template.py`; they render
  `"5.00 TB"`. A non-number still renders as before (`?` for `None`). The default policy template now reads
  `{total:tb} of storage, but the name promises {expected:tb}`; `ensure_default_health_policies` re-syncs the changed
  definition at the next start.

## Consequences

Audit events and stored health reasons written before this keep the "bytes" wording (events are append-only); a
server's stored reasons refresh on its next ingest. `tools/run_collector.py` already used these units.
