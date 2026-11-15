# ADR-0062: One failed server never aborts a run; it becomes a `COLLECTION_ERROR` stub

**Status:** Accepted, implemented 2026-11-15.

## Context

`IngestService.ingest` guards each server with a `try` around `_ingest_one` only. The `async for` over
`provider.collect()` sits outside it, so an exception raised while a provider *builds* one server (a mapper
meeting an odd record: `KeyError`, `TypeError`, `OverflowError`, `httpx.InvalidURL`) leaves the generator, which
cannot be resumed. `tools.run_collector` caught it as `collector.manager_failed`, never read
`collection_errors`, exited 1 and lost every later server. An audit (2026-11-15) found the pattern in OneView,
Intersight, UCS Manager (the whole domain), UCS Central (a domain's already-collected servers were discarded),
Redfish (the whole run, and one bad `ComputerSystem` failed its whole BMC), OpenManage, KubeVirt and the BMC
probe wrapper (one malformed address lost a batch of 64). The log named neither the server nor the field.

## Decision

1. **Each provider guards one server's mapping** and calls
   `ServerInventoryProvider._contain_server_failure`, which logs `<vendor>.server_failed` (server name, id,
   serial, stage, endpoint, exception with traceback), calls `_record_error` and counts the failure.
   `except Exception` only; `CancelledError` still flows.
2. **The server is written as a `reachable=False` stub**, so the UI shows that a collection was attempted and
   failed. `unreachable_reason` is the new `COLLECTION_ERROR`; the new optional `unreachable_detail` holds the
   cause in words (`mapping hardware: KeyError: 'processorCount'`). Ingest's carry-forward keeps every stored
   value (hardware, classification, maintenance, reservation, openshift); a stub neither advances
   `last_seen_at` nor creates a second document. It does refresh `listed_at`, so pruning is unaffected. The next
   good read clears both fields. Existing documents load unchanged (the field defaults to `None`).
3. **A stub needs a real serial.** Ingest treats a serial-less record as new, except `REDFISH_STANDALONE`, which
   matches by BMC host (ADR-0037). The identity is read from the raw record *before* mapping; when the serial
   cannot be read the helper returns `None` and the failure is only logged and recorded, never a duplicate
   document. KubeVirt (ADR-0058, which said it never writes `reachable=False`) writes a stub only for a VM
   whose System was read and then failed to map.
4. **The run exits 3 (PARTIAL)**: the recorded message holds neither benign marker (`: unreachable — `,
   `: login failed for credential `). **If at least 5 servers failed and more than 20 % of the fleet did
   (`is_systemic_failure`), the run exits 1** and prints the first causes: a defect in the collector, not bad
   servers. The stubs are already written.
5. **Health:** a stub raises `bmc.unreachable` (CRITICAL) with the reason as evidence. A mapper bug on a healthy
   server therefore shows as CRITICAL `collection_error` until the next good read. That is deliberate: someone
   must look.
6. **Wrappers delegate the counters.** `_NameFilteredProvider` and `BmcProbedProvider` pass `servers_seen` and
   `server_failures` through, as they do `collection_errors`. UCS Central and OpenManage merge their engines'
   errors and failure counts into their own.

Also fixed in the same change: `parse_bmc_address` never raises; UCS Central keeps a failing domain's
already-collected servers; a bad Redfish `ComputerSystem` drops only itself; the BMC probe no longer loses a
batch; UCS `_as_int` returns `None` (not 0) for an unreadable count; per-server log lines gained the BMC host and
system path; silent `None` returns in `get_one` are logged. Also:

- UCS Central and OpenManage use "first wins" for a duplicate iDRAC address or DeviceName (it was last).
- Redfish stubs reuse the mapper's serial (the Dell service tag), name (HostName first) and `external_id`; a
  failing System on a multi-system BMC gets a stub only when it has a serial, and an early sub-resource failure
  there marks the sibling hosts' GPUs unread instead of writing a possibly-ghost tray document.
- Ingest keeps the stored BMC address, UUID, attachments and VM host for a `reachable=False` stub.
- `redact_secrets` masks URL user-info and secret query values in every failure message and in the stored
  `unreachable_detail`.
- `servers_seen` counts dropped failures too; the helper's recorded message always contains
  `SERVER_FAILED_MARKER`, which `_is_benign_collection_error` treats as hard.
- `BmcProbedProvider` counts its own probe failures; a systemic exit 1 is recorded with `partial=True`.
- KubeVirt `get_one` raises a non-Redfish bug instead of returning `None`.

### Known limits

- `_NameFilteredProvider` and the systemic ratio count the pre-filter fleet.
- A UCS Central domain-level failure is one recorded error, not counted per server.
- The 5 / 20 % threshold is a judgment, not measured.
- The fake provider never seeds `COLLECTION_ERROR`.
- The health policy shows a stub as CRITICAL `bmc.unreachable` until the next good read.

## Consequences

- A systematic mapper bug produces up to N stubs before the run fails loudly; the threshold is a constant in
  `app.domain.ports.provider`.
- `ServerSummary`/`ServerDetail` gain `unreachable_detail`; `ServerRow` does not (it stays small).
- The skill `server-scan-api` documents the new field and enum value.
- **Rollout and rollback.** Deploy the API image before the collectors (ADR-0060): a new collector can write
  `unreachable_reason: "collection_error"`, which an older API cannot load (the enum is closed), so that server's
  detail, list and ingest would fail. Old documents load unchanged on the new code. **Rolling the API back after a
  stub exists** needs this first:
  `db.servers.updateMany({unreachable_reason: "collection_error"}, {$set: {unreachable_reason: "protocol_error"}})`.
  Rolling back only the collectors is harmless.
