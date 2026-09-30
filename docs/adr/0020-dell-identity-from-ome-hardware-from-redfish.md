# ADR-0020: Dell collection splits — identity from OME, hardware from Redfish

- Status: Accepted
- Date: 2026-09-02
- Supersedes the collection half of the original Dell collector
  (`docs/dell-collectors.md`, "Collection flow")

## Context

The first Dell collector read everything from one OpenManage Enterprise
appliance: two bulk REST calls to enumerate the estate, then four
`InventoryDetails` calls per matched server for CPU, memory, storage and
NICs. OME sources that detail from each server's iDRAC, so it looked like
the same data at a fraction of the connection count.

Three live-appliance runs said otherwise. Every hardware field that
mattered came back in a shape that had to be guessed at:

- `serverStorage` is not a valid `InventoryType` and returns HTTP 400. The
  real one is `serverArrayDisks`.
- `serverArrayDisks` populated none of the expected capacity fields, and
  the one it did populate (`Size`) was in an ambiguous unit — a 1.92 TB
  disk reported ~1.9e6. Capacity ended up parsed out of the Dell **model
  string** (`"... U.2 1.92TB"`).
- `MediaType` was `UNKNOWN` for NVMe drives, so media had to be inferred by
  scanning `MediaType`, `BusType` and the model string together.
- `serverProcessors` carried no logical-processor count and no
  hyperthreading flag, so thread count fell back to `2 x cores`.

Each of those is a heuristic standing in for a value the hardware knows
exactly. `docs/dell-collectors.md` still carries an open request for one
raw `serverArrayDisks` and one `serverProcessors` entry to replace them.

Meanwhile ADR-0016 built a standalone Redfish collector that reads
`CapacityBytes`, `MediaType`, `TotalThreads` and the rest directly from a
BMC, as measured values, and iDRAC is a conformant Redfish implementation
squarely in that collector's stated scope.

## Decision

Collect Dell in two passes, from the two sources that actually know:

1. **OME says who exists.** Two bulk calls, unchanged, give each server its
   profile name, deployment template (SPT), service tag and iDRAC address.
2. **Each server's BMC says what it is.** The discovered addresses become
   `RedfishTarget`s and the whole hardware pass is
   `app.infrastructure.providers.redfish` unchanged.
3. The two halves are joined on the BMC host, and OME's identity fields are
   put back onto the collected server.

The split follows what each side can see. Only OME knows a server is
`ocp4-nyc-prod-worker-03` — an iDRAC has never heard of that name, and the
name is what site parsing and classification key off. Only the BMC reports
hardware as measured values.

`RedfishStandaloneProvider` already takes its `targets` injected and
`RedfishTarget.name` already flows to
`system_to_provider_server(override_name=...)` — a hook that exists because
ADR-0016 faced the same problem from the other side (an operator who knows
the name, a BMC that does not). No change to the Redfish collector was
needed to reuse it here.

### What this deletes

The OME hardware mappers, ~400 lines: `cpu_from_processors`,
`memory_bytes_from_modules`, `storage_from_devices`, `nics_from_interfaces`
and every capacity/media/thread heuristic above. They existed only because
OME did not report the real values. Keeping them as a fallback was
considered and rejected: it would have kept the heuristics alive, and made
a server's hardware source vary run to run depending on whether its BMC
answered — the hardest kind of inconsistency to debug months later.

`idrac_bmc_address` is deliberately **kept**. The Redfish collector reports
`bmc_address_raw` as `https://<host>`; a Dell server's stored address must
stay the `idrac-virtualmedia://...` form that
`app.domain.value_objects.bmc_address.parse_bmc_address` documents and a
Metal3 `BareMetalHost` round-trips into `spec.bmc.address`. Collecting over
Redfish must not silently downgrade it.

## Consequences

**The cost inverts, and this is the real price.** Dell collection was one
appliance answering everything. It is now ~25 HTTPS round trips against
every collected server. A 400-server estate is ~10,000 requests per sweep;
at this platform's 10,000-server target it is ~250,000. Accordingly:

- `namePattern` is applied *before* any BMC is contacted. Unlike the
  standalone Redfish collector — where the filter is deliberately disabled
  because a BMC does not know the server's name — it applies here, because
  OME supplies the name. This is the single thing that keeps the design
  affordable on an estate where most Dell servers are not ours.
- The CronJob moves from hourly to every 6 hours, matching
  `redfishStandalone`. ADR-0016's warning applies unchanged: embedded
  management hardware degrades when polled.
- The collector pod now needs egress to the whole Dell BMC network, not
  just to the appliance.

**Two logins for one manager type.** This breaks the "one endpoint and one
login per `ManagerType`" invariant in CLAUDE.md, knowingly:
`INVENTORY_OME_USERNAME`/`_PASSWORD` for the appliance and
`INVENTORY_OME_BMC_USERNAME`/`_PASSWORD` for the iDRACs. One shared
read-only iDRAC account for the estate; per-host credentials remain the
standalone collector's job, via its inventory file.

Every other BMC knob — timeouts, budgets, fleet concurrency, the
auth-failure guard, the TLS floor and CA bundle — is the shared
`redfish_*` settings rather than a second `ome_*` set. Same protocol,
same class of device; two sets would drift.

**TLS verification is off by default here** where the standalone collector
leaves it on. iDRACs ship a factory self-signed certificate and this
collector's fleet is whatever OME reports, so there is no per-host place to
name a CA. `INVENTORY_REDFISH_CA_BUNDLE` plus
`INVENTORY_OME_BMC_VERIFY_TLS=true` is the scalable fix and the documented
intent, not leaving verification off forever.

**Per-host failures are stub documents.** *History:* a
per-host failure used to land in `collection_errors` and make the run
PARTIAL; 2026-09-10 made a plain unreachable host a `reachable=False`
document (dated update below), 2026-09-12 a rejected credential too.
**Since 2026-10-01 (ADR-0037) every per-host Redfish failure — TLS, time
budget and protocol as well — yields that stub with an
`unreachable_reason`. Only a plain unreachable host or a rejected login is
benign (exit 0); TLS, budget and protocol failures are still recorded and
make the run PARTIAL (exit 3), as before.** A profile OME gives no iDRAC address for is
skipped and logged (`ome.profile_without_address`), not an error.

**Correlation is unchanged in mechanism, wrong in its field, and now
fixed.** `IngestService` correlates on `(vendor, serial_normalized)`.
`vendor_from_manufacturer` maps `"Dell Inc."` to `Vendor.DELL`, so Dell
servers do not land as `STANDALONE`. The serial itself was the wrong
field until 2026-09-08 — see "Status of verification" below.

## Status of verification

**Item 1 settled, 2026-09-08, against several live iDRAC9 servers — and
the original assumption was wrong.** iDRAC's top-level
`ComputerSystem.SerialNumber` is **not** the Service Tag OME reports as
`DeviceServiceTag`; it is a manufacturing/board serial. The real Service
Tag is `Oem.Dell.DellSystem.NodeID`, Dell's own OEM extension to
`ComputerSystem`, and matched OME's Service Tag on every server checked.
`mapping._dell_serial` now reads it, preferring it over `SerialNumber`
whenever present — which is only on Dell hardware, since no other vendor
populates an `Oem.Dell` block. See `docs/dell-collectors.md`'s "Collection
flow" for the full writeup of what would have gone wrong (a stable but
wrong serial, not a caught error) had this shipped as first assumed.
**Unconfirmed on any Dell generation older than iDRAC9** — this generation
is what was checked; iDRAC7/8 may shape the OEM block differently.

**Item 4 hit in production, 2026-09-10, before the rest of this list was
ever checked.** A server named `ocp4-compute-five-01` failed with
`unreachable, could not reach ocp4-compute-five-01`: OME's console-wide
"Server Device Naming" setting was System Hostname, so `TargetName`
(Profiles) and `DeviceName` (Devices) — the fields this collector read as
the iDRAC address — held the server's own OS hostname instead. Researched
against Dell's own official OME automation source (no live appliance was
available; see `docs/notes/2026-09-openmanage-device-naming-and-ip-address.md`):
every Dell script that needs to *reach* a device reads
`DeviceManagement[0].NetworkAddress` instead, which is immune to this
setting. `mapping._network_address`/`identity_from_profile` now prefer it,
falling back to `TargetName`/`DeviceName` only when a device carries no
`DeviceManagement` entry. The profile-to-device join itself is unchanged
(still `TargetName`/`DeviceName`) — both sides derive from the same
console-wide setting, so they keep agreeing with each other regardless of
which mode is active, even though neither is reliably an address by
itself. See `docs/dell-collectors.md`'s "Collection flow" for the full
writeup, including two items the research left unconfirmed for lack of a
live appliance.

**A second, related decision from the same incident, same day** (widened
to every per-host failure 2026-10-01, ADR-0037): a plain
connection failure on a known profile no longer only produces a log line
and a PARTIAL exit code. `OpenManageProvider` now writes it as a real
`reachable=False` server document, built from OME's own identity — the
BMC is never contacted for this — with every hardware field left `None`
so `IngestService` carries the server's last-known hardware forward
rather than blanking it, exactly as it already does for any other unread
field. *Superseded 2026-10-01:* this was deliberately narrower than "any redfish
failure" (only plain-connection-refused was a stub; auth/TLS/budget
failures had no stub), and is no longer — see above. See `docs/dell-collectors.md`'s "Collection flow" and
`docs/arc42.md` §12 (`Server.reachable`).

Before trusting the rest of this design in production, still confirm on
real hardware:

2. That one read-only iDRAC account authenticates fleet-wide — the Redfish
   auth guard disables a credential after 3 hosts reject it and aborts the
   run after 10 failures, so a partially-deployed account fails loudly.
3. Sweep wall-clock at real fleet size against the 6-hour cadence and
   `redfish_run_budget_seconds`.
4. That the `idrac-virtualmedia://` address still round-trips for the
   servers now collected over Redfish.
