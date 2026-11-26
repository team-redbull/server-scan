# ADR-0070: A server's warranty, copied from the operator's warranty collection

**Status:** Accepted, implemented 2026-11-26.

## Context

The operator keeps warranty data in a MongoDB collection Server Scan does not own: `warranty-excel-new`
in database `MongoDB-Nitur`, possibly on another cluster. One document per server (sometimes more), shaped
by whatever imports it:

```json
{"_id": ObjectId(...), "Name": "ocp-tomer-compute-o01", "Serial": "WMP25492815", "Site": "Bat-yam",
 "Network": "lab", "Warranty": "29-01-2025", "Vendor": "Cisco", "Model": "UCSC-C220-M5SX",
 "Expired": "Expired", "RAM": 1536, "Cores": 96}
```

The operator wants each server's warranty end date in Server Scan, filterable, and **never** a reason for a
server to turn CRITICAL (or change its State at all).

Facts given by the operator (2026-11-26): `Serial` is the same value Server Scan shows; `Warranty` is always
`DD-MM-YYYY`, but an empty one must be logged and treated as not read; there should be one document per serial,
and if there are several the latest date wins; "Expiring soon" is 90 days; the URI, database, collection and
window must be Helm values; the lookup runs monthly, when a server is created, and on "Fetch now"; physical
servers only.

## Decision

1. **Read-only, like `confs` (ADR-0061).** Server Scan never writes the collection and never reads `_id`; it
   projects `Serial` and `Warranty` only. `Name`, `Site`, `Model`, `RAM`, `Cores` are what Server Scan already
   reads from the hardware, and `Expired` is a derived text that can be stale, so none is read. Settings:
   `INVENTORY_WARRANTY_MONGO_URI` (empty = warranty off), `_DATABASE`, `_COLLECTION`, `_EXPIRING_DAYS`. Helm:
   the URI is set once, in the `server-scan` release's `warranty.*` (rendered into the Secret
   `<release>-warranty-db`, or `warranty.existingSecret`); `server-scan-collectors` renders no Secret and reads
   that one by name (`warranty.secretName`, `optional`), so the two releases never render the same Secret.
2. **Matched by serial only**, normalized like `identity.serial_normalized` (case and runs of spaces
   ignored; a serial the import stored as a number matches its digits). `Vendor` is not used: its spelling
   (`Cisco`, `HPE`) is not Server Scan's. A placeholder serial (`To Be Filled By O.E.M.`) is never looked up.
3. **Stored on the server as `Server.warranty`** (`found`, `expires_on` ISO date), not joined at read time:
   `GET /servers/{id}` is cached per revision and must not embed another collection, and `GET /servers/rows`
   relies on a byte-stable body. `None` means never looked up (a VM, a serial-less server, warranty off);
   `found: false` means no document has the serial; `found: true, expires_on: null` means the document's date
   was empty or unreadable (logged as `warranty.date_unread`).
4. **When it is looked up:** when a collector *creates* a server or identifies a serial-less stub it adopts (one case-insensitive query by serial; no index is created on a
   collection Server Scan does not own, and a few thousand documents need none), on "Fetch now"
   (`ingest_one(refresh_warranty=True)`; the `/servers/available` recheck does not), and by the monthly
   `warranty-sync` CronJob (`tools.sync_warranty`, 1st of the month 04:00, triggerable from the Jobs page),
   which reads the whole collection once and writes each physical server whose warranty differs, with a
   revision CAS that bumps `revision` and `updated_at`. Every other collector run carries the stored warranty
   forward, so the daily runs never touch the warranty database. An unreadable collection keeps the stored
   value, never "no record", and after one failure a source stops trying for the rest of its run (one timeout,
   not one per new server). **An empty collection refuses the sync** (exit 1, `warranty.sync_refused`): a
   misnamed database or collection reads as empty, and writing it would mark every server "no record" (the
   same rule as ADR-0024's empty membership read). After a CAS miss the sync re-reads the server and leaves one
   that already holds the wanted value, so a "Fetch now" that won the race is not overwritten.
5. **Physical servers only.** A `KUBEVIRT` VM is never looked up and never synced (`server_type_of`).
6. **Expiry is never stored and never a health reason.** The UI compares `expires_on` with the viewer's local
   date (re-read every minute, so a page left open past midnight moves on): Expired, Expiring soon (within
   `INVENTORY_WARRANTY_EXPIRING_DAYS`, served with `GET /servers/rows` and on `GET /servers/{id}`; the detail
   is cached per revision, so a changed window shows there on the server's next change),
   Active, Date not read, No warranty record. Storing it would be a day late (health is evaluated only at
   ingest), and a health reason carries a severity that rolls up into the State, which the operator ruled out.
   The inventory gets a **Warranty filter** (its own dropdown, options sorted by count, like Reason); the
   server page shows a Warranty line under the health breakdown.
7. **Fake data** (convention 10): `tools.seed_inventory` writes seeded documents (marked
   `_server_scan_seed`, so a re-seed deletes only its own) for the fake fleet only when the warranty URI is the
   inventory's own Mongo (dev), so it never writes a real warranty collection, then
   runs the sync: about 15% expired, 12% expiring soon, 4% with an empty date, 4% with no document, 3% with an
   older duplicate.

## Consequences

- A warranty renewed in the collection shows in Server Scan after the next monthly sync, or at once with
  "Fetch now" on that server.
- Existing servers have no warranty until the first sync: **run `warranty-sync` once from the Jobs page after
  enabling it**, or wait for the 1st of the month. That first sync writes every physical server once (one
  revision bump each).
- **Enable it only after the collectors release runs this image** (ADR-0060 bumps it after server-scan): an
  older collector's `Server` has no `warranty` field, so a server it rewrites loses the stored warranty until
  the next sync.
- The API pod and every collector read the warranty URI; the collectors only use it for a server they create.
- If the writer changes the date format, every date reads "not read" and `warranty.date_unread` logs the
  serials; nothing fails.
