# Dell collectors — verified implementation facts

This is the technical reference behind
`app.infrastructure.providers.openmanage` (`client.py`, `mapping.py`,
`provider.py`). Every docstring in those modules points here rather than
carrying its own explanation, so the `##` headings below are load-bearing:
renaming one breaks the cross-references in the source files.

It is for whoever is about to change the Dell collector. **Provenance
matters here more than usual, and it is unusually weak.** Unlike the Cisco
collector — validated end to end against a live UCS Platform Emulator (see
`docs/adr/0009`) — the OME field names, endpoint paths and unit
assumptions below are carried over from a **production Dell scanner the
user ran in a separate project** (`dell_provider_example.py`, handed to
this repo as the starting point). They are real and worked in production,
but they have **not** been re-validated against a live OME appliance from
*this* codebase. Treat every "Size is MB" and status-code mapping as an
assumption to confirm on first contact with real hardware, and update this
document with what that proves — the same discipline the Cisco ADRs
record.

## OME REST surface

One OpenManage Enterprise appliance manages the whole Dell estate and
answers an OData-shaped REST API at `https://<appliance>/api`. The
collector uses four endpoints, all validated in the production scanner:

- `POST /SessionService/Sessions` — authenticate. The body is
  `{"UserName", "Password", "SessionType": "API"}`; the session token
  comes back in the **`X-Auth-Token` response header** (not the body), and
  every subsequent request must carry it. The body's `Id` is the session
  handle used to `DELETE /SessionService/Sessions('<id>')` on logout.
- `GET /ProfileService/Profiles` — one entry per server profile. An
  undeployed profile has no `TargetName`; it names no server and is
  counted and skipped, not reported as an unreachable host.
  `ProfileName` is the server's operator-facing name (and the platform's
  site/classification source); `TargetName` is the profile's display
  name, used only to join to a `/DeviceService/Devices` entry — **not
  trusted as the iDRAC address**, see the dated update below.
- `GET /DeviceService/Devices` — one entry per managed device. `DeviceName`
  is the device's display name (the join key back to a profile's
  `TargetName` — the two agree with each other regardless of naming
  policy, even though neither is reliably an address), `Model` is the
  hardware model, `DeviceServiceTag` is the service tag used as the server
  serial, and `Id` is the device handle for inventory calls.
  `DeviceManagement[0].NetworkAddress` is the field actually used to reach
  the iDRAC — see the dated update below.
- `GET /DeviceService/Devices(<id>)/InventoryDetails('<section>')` — one
  hardware section for one device, returned in an `InventoryInfo` array.
  The collector reads `serverProcessors`, `serverMemoryDevices`,
  `serverArrayDisks` and `serverNetworkInterfaces`. OME populates these from
  the server's iDRAC, so "collect from OpenManage and iDRAC" is one REST
  surface, not two connections.

  **Physical disks are `serverArrayDisks`, not `serverStorage`.** A live
  appliance returns HTTP 400 for `serverStorage` — it is not a valid
  `InventoryType`. This was an assumption carried over from the production
  scanner that proved wrong here (2026-08-18, live OME run).

Collections are paged: each response carries a `value` array and, when
more remain, an `@odata.nextLink` (an absolute `/api/...` path). The
client follows that link rather than managing `$skip`/`$top` itself, so
the appliance stays authoritative on page size.

## Session lifecycle

`OmeClient` is an async `httpx.AsyncClient` used as an async context
manager: `__aenter__` logs in, `__aexit__` deletes the session and closes
the pool, best-effort (a failed logout is logged, never raised). One
session per collector run, never pooled across runs — the same shape the
Cisco clients use. TLS verification defaults **off** because air-gapped OME
appliances ship a self-signed certificate with no private CA to trust it
against; set `INVENTORY_OME_*` with a real chain and pass `verify_tls=True`
where one exists.

## Collection flow

**OME discovers, Redfish collects** — see
`docs/adr/0020-dell-identity-from-ome-hardware-from-redfish.md` for why,
and for what the OME-only design got wrong.

`OpenManageProvider.list_servers` makes the two bulk calls once
(`/ProfileService/Profiles`, `/DeviceService/Devices`) and joins each
profile to its device by display name (`TargetName`/`DeviceName` — see the
2026-09-10 update below for why that is a name, not necessarily an
address). That yields, per server, the four things only OME knows: the
**profile name** (the server's name), the **deployment template**, the
**service tag** and the **iDRAC address**.

Every matched address then becomes a `RedfishTarget`, and the hardware pass
is `app.infrastructure.providers.redfish` unchanged — CPU, memory, storage,
NICs and GPUs read from the server's own BMC as measured values.
`RedfishTarget.name` carries the OME profile name through to
`system_to_provider_server(override_name=...)`, which is what keeps a Dell
server named the thing site parsing and classification need rather than
whatever iDRAC calls it.

`name_pattern` is applied **before any BMC is contacted**. The expensive
pass is per-server here, so this is what keeps the design affordable; it is
still only an efficiency gate, with the authoritative filter remaining
`tools.run_collector._NameFilteredProvider`. Note this differs from the
standalone Redfish collector, where the pattern is deliberately *not*
applied because a BMC does not know the server's name — here OME supplies
it.

A profile OME reports no iDRAC address for is dropped and recorded: there
is nothing to collect it from. That and every per-host Redfish failure land
on `collection_errors`, which `tools.run_collector` reads to report the run
as **PARTIAL** (exit 3) rather than a silently-complete success.

Correlation keys on `(vendor, serial)`
(`app.application.services.ingest`).

**Settled against real hardware, 2026-09-08 — and the original assumption
was wrong.** The Service Tag OME shows as `DeviceServiceTag`, and the one
printed on the chassis and in the iDRAC UI, is **not**
`ComputerSystem.SerialNumber`. Confirmed against several live iDRAC9
servers: `SerialNumber` is a manufacturing/board serial, while the real
Service Tag is `Oem.Dell.DellSystem.NodeID` — Dell's own OEM extension to
the `ComputerSystem` resource. `mapping._dell_serial` reads it, preferring
it over `SerialNumber` whenever the OEM block is present (i.e. always, on
Dell hardware); every other vendor's `ComputerSystem` carries no `Oem.Dell`
block at all, so `REDFISH_STANDALONE`/HPE/whitebox servers are unaffected.

Had this shipped as originally assumed, every Dell server's serial would
have been a real but wrong value — not absent, not a placeholder, so
nothing in `IngestService` would have caught it — and correlation across
runs would have kept working (the wrong value is at least stable per
server), but it would never have matched anything OME-only tooling reports
by Service Tag, and a future switch to reading the OEM field would have
duplicated every already-ingested Dell server rather than updating it in
place. Confirm `Oem.Dell.DellSystem.NodeID`'s presence on any Dell
generation this platform hasn't tested yet (this was iDRAC9) before
trusting it there.

The BMC address stored is OME's `idrac-virtualmedia://` form, not the
`https://<host>` origin the Redfish collector reports for a standalone BMC:
that is what `parse_bmc_address` documents for Dell and what a Metal3
`BareMetalHost` round-trips.

**Fixed 2026-09-10 — `TargetName`/`DeviceName` are not addresses.** A live
report: a server named `ocp4-compute-five-01` failed collection with
`unreachable, could not reach ocp4-compute-five-01` — the collector was
dialling the server's *OS hostname*, not its iDRAC. Cause: OME's
console-wide "Server Device Naming" setting (REST:
`ApplicationService/Settings`'s `DEVICE_PREFERRED_NAME`, values
`IDRAC_HOSTNAME`/`IDRAC_SYSTEM_HOSTNAME`) can be set to System Hostname,
in which case `TargetName` and `DeviceName` both hold the OS hostname, not
a reachable address — confirmed from Dell's own official OME automation
(`dellemc-openmanage-ansible-modules`, `OpenManage-Enterprise`; see
`docs/notes/2026-09-openmanage-device-naming-and-ip-address.md` for the
full research). Every one of Dell's own scripts that needs to *reach* a
device instead reads `DeviceManagement[0].NetworkAddress`, which is immune
to this setting. `mapping._network_address` now reads it and
`identity_from_profile` prefers it over `TargetName`/`DeviceName`, which
remain only as a fallback and as the profile-to-device join key (the join
still works under either naming mode, since both sides derive from the
same policy and so still agree with each other). **Unconfirmed**: whether
`DeviceManagement` can ever be empty for a managed iDRAC, and whether it
can hold more than one entry such that `[0]` is not always the right one
— see the research doc's "Open questions" for what would settle both.

**Added the same day: a genuinely unreachable iDRAC is now a visible,
`reachable=False` server document, not just a log line.** Even after the
fix above, some fraction of a fleet is legitimately down at any given
moment — a plain connection failure (`RedfishUnreachableError`) is no
longer treated as a run-wide problem. `OpenManageProvider._list_servers`
tracks which of OME's known profiles the Redfish pass actually reached;
for each one that only failed with a plain connection error, it yields a
placeholder built from OME's own identity (name, service tag, model,
template — everything OME knows without ever touching the BMC) with
`reachable=False` and every hardware field `None`. `IngestService` then
does exactly what it already does for any other unread field: carries the
server's last-known hardware forward rather than blanking it, and sets
`Server.unreachable_since` (kept stable across repeated misses, cleared on
recovery). The one thing that changes operationally: this specific
failure no longer counts toward `collection_errors`, so the CronJob pod
exits 0 instead of 3 for it — the per-server document is now the signal,
not the exit code. See `docs/arc42.md` §6.1 and §12 (`Server.reachable`),
and `_is_uncollected`/`_unreachable_server` in `provider.py` for the
exact matching rule.

**The uncollected-host merge runs in a `finally`, behind
`contextlib.aclosing`** (moved here from the code, 2026-09-13). A consumer
that stops early (`--limit`, a killed run) throws `GeneratorExit` in at
the `yield`, which used to skip the pass over `redfish.collection_errors`
entirely — the same shape `..ucs_central.provider` handles, see
`docs/cisco-collectors.md`'s "Sessions". In that pass a dead BMC and a
rejected login are set aside as uncollected; every other failure is still
recorded, still PARTIAL. The placeholder documents themselves are yielded
only on normal completion of the Redfish pass: a consumer that stopped
early never asked to see the rest of the fleet, so nothing is reported
unreachable on its behalf.

**Widened the same day: a rejected BMC credential is exit-0-safe too**,
after a real OME run hit auth failures often enough that PARTIAL stopped
meaning anything unusual — `tools.run_collector.
_is_benign_collection_error` treats `AUTH_REJECTED_MARKER` (a
`..redfish.provider` export) the same as `UNREACHABLE_MARKER`. Still
PARTIAL-worthy: TLS failures, a per-host time budget exceeded, and any
other unrecognized error. See ADR-0016's second 2026-09-10 update.

**Widened again 2026-09-12: a rejected login now writes the same
`reachable=False` placeholder a dead BMC does.** An iDRAC that refuses
the credential leaves the server exactly as unmeasured as one that does
not answer, and OME knows its identity either way — so `_is_uncollected`
matches both markers, and the server stays visible in the inventory with
its last-known hardware rather than quietly missing a run. The same
change removed the credential circuit breaker that used to skip the rest
of the fleet after three rejections: **every iDRAC is now contacted every
run**, however many earlier ones refused. See ADR-0016's 2026-09-12
update — and note the risk that buys, which is now the operator's:
repeating a run with a wrong shared password locks accounts and
IP-blocks this collector from every iDRAC for about an hour.

## Profile template

An OME server profile carries the deployment template it was created from
in its `/ProfileService/Profiles` entry: `TemplateName` and `TemplateId`.
The collector maps those onto `ProviderServer.profile_template_name` /
`profile_template_external_id`, which the ingest pipeline stores as the
server's `ProfileTemplate`. A profile deployed without a template leaves
both `None`. The field names are the usual **unverified assumption** —
confirm `TemplateName`/`TemplateId` exist on the profile object of the OME
version in use.

## BMC address

The iDRAC IP is rendered as
`idrac-virtualmedia://<ip>/redfish/v1/Systems/System.Embedded.1` — the
exact Dell form `app.domain.value_objects.bmc_address.parse_bmc_address`
documents, which round-trips into a Metal3 `BareMetalHost`'s
`spec.bmc.address`. OME's device summary does not expose the iDRAC's own
MAC on the validated path, so `bmc_mac` is left unset.

The `idrac-virtualmedia://` form is kept *in preference to* the
`https://<host>` origin the shared Redfish mapping reports for a
standalone BMC, even though the hardware now comes over Redfish:
`OpenManageProvider._merged` reapplies OME's address after the Redfish
pass, so collecting hardware over Redfish never silently downgrades a
Dell server's stored BMC address (`mapping.idrac_bmc_address`).

## NICs — one per physical port

iDRAC names an `EthernetInterface` by Dell's FQDD, which the shared
Redfish mapping carries through as `ProviderNic.location` (`Name` is the
same "System Ethernet Interface" on every one of them and so names none).
The FQDD is a kind, then `controller-port-partition`:

    NIC.Integrated.1-1-1   integrated controller 1, port 1, partition 1
    NIC.Slot.2-4-3         card in slot 2, port 4, partition 3

The three numbers are what an operator reads as a NIC's location, and
the partition is what NPAR multiplies: NIC partitioning splits each
physical port into up to four logical functions, each reported as its own
`EthernetInterface` with its own MAC, so a partitioned 4-port card is 16
interfaces — the server's *logical* plumbing, not what is cabled. An
operator asking "how many NICs does this box have" means four.

`mapping.dell_port_nics` (applied in `OpenManageProvider._merged`, not in
the shared Redfish mapping — only a Dell collector knows an FQDD well
enough to tell a second partition from a second port) therefore reduces
the list to one entry per physical port, by three rules:

- **Keep the first partition, do not merge.** Partition 1 exists on every
  Dell NIC whether NPAR is enabled or not, so an unpartitioned server is
  unaffected, and the surviving entry is a real interface with a real MAC
  rather than a synthesized summary. `nic_macs` is left whole — it is the
  identity correlation key, and a server already ingested with all
  sixteen MACs must keep matching on any of them.
- **An unrecognized identifier is kept, appended after every FQDD-named
  one.** The filter may only remove something it positively identified as
  a non-first partition; a BMC naming its NICs some other way must not
  have them silently dropped.
- **Sorted by controller/port/partition, not by BMC report order**
  (changed 2026-09-10 at the operator's request): iDRAC's own
  `EthernetInterfaces` order is not documented as stable, so sorting on
  the FQDD's numbers is what makes repeated collections of an unchanged
  server agree — `NIC.Integrated.1-1-1` before `1-2-1` before `1-3-1`,
  never by MAC.

The surviving entry is named by its FQDD and located as
`controller/port/partition` (`1/1/1`).

---

# Superseded: what OME's `InventoryDetails` taught us

**Everything below described the OME-only collector, whose hardware
mappers were deleted in
`docs/adr/0020-dell-identity-from-ome-hardware-from-redfish.md`.** Hardware
now comes from each server's iDRAC over Redfish, which reports measured
values and needs none of these heuristics.

It is kept, in full, for three reasons. Every fact here cost a live
appliance run to learn, and a fact without its provenance becomes folklore
nobody dares change. The OME field names are still the reference if anyone
ever needs `InventoryDetails` again — as a fallback, a cross-check, or for
a field Redfish does not carry. And the *reasons* these fields could not be
trusted are the entire justification for ADR-0020: delete them and the next
person re-derives the same wrong design.

Read it as history. None of the functions named below still exist.

## NIC status (superseded)

`serverNetworkInterfaces` nests interface -> `Ports` -> `Partitions`. The
MAC is per partition (`CurrentMacAddress`); link status and speed are per
port and shared by that port's partitions. `mapping.nics_from_interfaces`
produces one `ProviderNic` per partition — name (`Fqdd`, else `NicId`-`PortId`),
MAC, `speed_mbps`, `link_state` — which ingest turns into
`NetworkInfo.interfaces` (with `link_state` mapped onto
`app.domain.enums.LinkState`). `nic_macs` is then derived from those NICs
as the flat set identity correlation keys on.

**Two encodings here are unverified assumptions, flagged like the capacity
one.** The port's `LinkStatus` is mapped by
`_LINK_STATUS_TEXT_MAP`/`_LINK_STATUS_CODE_MAP`: string values
("Connected"/"Disconnected"/…) are reliable, but whether OME sends a string
or an integer here — and what integer codes mean — must be confirmed on
hardware; the numeric map (`1 -> UP`, `2 -> DOWN`) is a best guess and
anything else becomes `UNKNOWN`. `LinkSpeed` is assumed to be in **Mbps**;
confirm it is not bits/sec or an enum code.

This deliberately differs from the production scanner, which selected a
**single** PXE-boot MAC by server-name heuristics — H100/H200 names took
the third interface, "data"/`-<N>tb-` names the last interface's last
port's last partition, everything else the first. That selection was
specific to DHCP reservation and has no field on `ProviderServer`; the
knowledge is preserved here rather than in code because this platform wants
the full per-NIC view, not the one boot NIC. If a future need arises to
mark the primary NIC, this is the rule it followed in production.

## Capacity units (superseded)

**Memory** (`serverMemoryDevices` `Size`) is read as **megabytes**, carried
over from the production scanner: `memory_total_bytes = sum(Size) * 1024^2`.
Still an assumption (the analogue of the UCS `total_memory` MB assumption
`docs/adr/0009` flags) — confirm a known-32 GiB DIMM reports `Size == 32768`.

**Disks** are **decimal** (base-1000): a "480GB" drive is 480e9 bytes.
`_disk_capacity_bytes` reads the capacity from the Dell **model string
first** ("... M.2 480GB", "... U.2 1.92TB") because it is the one source
that carries its own unit. The numeric fields
(`Size`/`Capacity`/`CapacityBytes`/`SizeInBytes`/`RawSize`/`DiskSize`) are
only a fallback for a disk whose model has no size token.

This ordering is deliberate and was forced by live hardware: a
`serverArrayDisks` entry *does* populate a `Size` field, but in an
ambiguous unit — a 1.92 TB disk reported ~1.9e6 (i.e. MB, not bytes).
Read as bytes it collapses to `0.0 GB`, and because it was tried first it
shadowed the correct model-string value. Reading the unit-bearing model
string first sidesteps the unit ambiguity entirely. If a raw
`serverArrayDisks` entry ever confirms the true unit of a numeric field,
that field can become primary again.

The dry-run renders the per-server total in **TB** and each drive in GB
below 1 TB / TB at or above (`_format_tb`/`_format_disk_size`).

## CPU summary (superseded)

From `serverProcessors` `InventoryInfo`: `sockets` is the entry count,
`cores` sums `NumberOfCores`, and the model is the first entry's
`ModelName`, falling back to `BrandName` then `Family`.

**Threads** proved to be the fragile field on live hardware: none of the
expected fields were present and no HT flag was either, so the sum came out
`0`. `_logical_processors` tries several field names
(`NumberOfLogicalProcessors`, `LogicalProcessorCount`, `NumberOfThreads`,
`ThreadCount`); when none is present, threads falls back **unconditionally**
to `2 * cores`, since the Dell Xeons in this fleet run hyperthreaded.
**This is a heuristic, not observed** — it over-reports on any machine with
HT physically off. Paste one raw `serverProcessors` entry to pin the real
thread field and drop the fallback.

## Drive health (superseded)

`serverArrayDisks` entries report status either as a numeric OME rollup code
(`Status`: 1000 normal, 2000 unknown, 3000 warning, 4000 critical) or a
string (`PrimaryStatus`/`Status`). `mapping._drive_health` handles both.

**Media type** was `UNKNOWN` on live hardware — `MediaType` is not the
plain "SSD"/"HDD" string first assumed. `_media_type` now scans
`MediaType`, `BusType` and the model string together and classifies NVMe /
SSD / HDD from any of them (the fleet's drives name themselves "NVMe" in the
model, so they resolve to `NVME`). Still worth confirming the real
`MediaType` encoding from a raw entry.
