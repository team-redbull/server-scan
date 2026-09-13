# HPE collectors — verified implementation facts

This is the technical reference behind
`app.infrastructure.providers.oneview` (`client.py`, `mapping.py`,
`provider.py`). Every docstring in those modules points here rather than
carrying its own explanation, so the `##` headings below are load-bearing:
renaming one breaks the cross-references in the source files.

It is for whoever is about to change the HPE collector.

**Provenance, stated up front.** Everything here comes from HPE's own API
Reference — `dp00003271en_us` (OneView for VMs, 8.00 / API 4600) and
`dp00006616en_us` (OneView for HPE Synergy, 10.20 / API 8000) — plus the
`hpeOneView` 11.4.0 SDK source and the OneView 10.0 Support Matrix
(`sd00006056en_us`). The full research notes with reproduction
instructions for every citation are `docs/notes/oneview-api.md`.

**Confirmed against a live HPE appliance on 2026-09-07** (821 servers, 685
service profiles, iLO 5 and iLO 6 both present) — see ADR-0022's
"Results, 2026-09-07" for the full write-up, and this file's own dated
corrections below for what that run found and fixed (a real storage-
mapping bug and a PSU health-vocabulary bug, both same-day). Before that
run, this reference was built entirely from HPE's own API Reference —
there is no OneView equivalent of Cisco's UCS Platform Emulator, so
everything here traced back to documentation rather than an observed
fact. A fact not otherwise dated as confirmed live is still that
documentation-derived state; run
`uv run python -m tools.verify_oneview` before trusting a number this
file doesn't already say was checked. See
`docs/adr/0022-oneview-only-hpe-collector.md`.

## OneView is the only source, whatever the iLO

There is no Redfish pass, no BMC login and no per-generation branch. An
iLO 4 server and a Gen11 go through the identical code. This is a
deliberate decision for a mixed iLO 4/5/6 fleet — one collection standard
for all HP hardware — and what it costs on the newer half (thread counts,
NIC speed and link state, PSUs, GPU telemetry) is tabulated in ADR-0022.

Do not "fix" this by adding a Redfish hand-off. If a future change adds
one, it must be additive for iLO 5+ and must not change what OneView
already fills.

## REST surface and cost model

One appliance answers at `https://<appliance>/rest/...`.

**`GET /rest/server-hardware` returns the complete `ServerHardwareV12`
object per member, not a summary** — verified by diffing the collection's
`members[]` field table against `GET /rest/server-hardware/{id}`'s own
response table: every field in the collection member exists in the
single-resource GET, with none present in only one. Adding `expand=all`
folds each server's subresource *data* into that same response. So the
whole appliance is a handful of requests:

- `GET /rest/version` — `{"currentVersion", "minimumVersion"}`. The one
  operation documented as requiring neither `Auth` nor `X-Api-Version`,
  which is what lets `health_check` prove reachability without spending
  a session.
- `POST /rest/login-sessions` → `{"sessionID"}`; `DELETE` the same path
  logs out, 204.
- `GET /rest/server-profiles` — names, and the template each came from.
- `GET /rest/server-profile-templates` — template URI → template name.
- `GET /rest/server-hardware?expand=all` — serials, models, addresses,
  CPU/memory, `portMap`, **and** every subresource's data (DIMMs, drives
  in both schemas, PCI devices including GPUs) inline.
- `GET /rest/server-hardware/{id}/powerSupplies` — the one per-server
  call, gated by `INVENTORY_ONEVIEW_COLLECT_PSUS`.

With `N` servers on the appliance:

| Tier | Calls | Yields |
|---|---|---|
| Bulk | `1` + `⌈profiles/256⌉` + `⌈N/25⌉` + `1` | everything except PSUs |
| + PSUs | `+ N` | `ProviderServer.psus` |

That bulk tier is two orders of magnitude cheaper than ADR-0020's Dell
design, which costs ~25 round trips *per server*.

**Not collected, deliberately** — each would cost calls for something
`ProviderServer` cannot carry, and the reasoning is in ADR-0022:
fleet firmware (`GET /rest/server-hardware/*/firmware`, one bulk call —
but there is no system-firmware field), fans (`/thermal`),
temperature and power draw (`/utilization`), and `/processors` for
`cpu_threads`.

## Sessions

`POST /rest/login-sessions` takes `{"userName", "password",
"loginMsgAck"}`. `Content-Type: application/json` is required; any other
value returns 415, and absent it `application/octet-stream` is assumed.

`loginMsgAck` is always sent as `true`, copying the SDK
(`connection.py:468`): an appliance configured to require
login-message acknowledgement rejects a login without it, and there is no
downside on one that does not.

The token comes back as `sessionID` and is replayed in a bare **`Auth`**
header — *not* `Authorization: Bearer`. The SDK sends it lowercase
(`auth`); HTTP header names are case-insensitive, and this collector uses
the documented spelling.

**Always log out.** The limits are real and enforced:

- 2400 active sessions per appliance (`SESSION_CRITICAL_LIMIT`).
- **960 from one source IP** (`SESSION_CLIENT_LIMIT`) — a collector pod is
  one source IP.
- 24-hour default idle timeout.

So `OneViewClient.logout()` runs from `__aexit__`, is best-effort, and
swallows its own errors so it can never mask the failure a caller is
already handling.

## API version

`X-Api-Version` is `required` on every documented operation. Which
versions an appliance accepts is a runtime question with an
unauthenticated answer, so the collector discovers it rather than
hardcoding a constant.

**And then clamps it** to `client.MAX_TESTED_API_VERSION` (8000, OneView
10.20 — the newest reference these mappings were read against). The
asymmetry is the point: HPE states an API version's behaviour "remains
the same … upward compatible from release to release", so an *older*
version stays correct on a newer appliance, while a *newer*
`currentVersion` is a contract nobody here has read. Without the clamp,
upgrading an appliance silently changes what the collector is handed.

The clamp is skipped in exactly one case — an appliance whose
`minimumVersion` is already above 8000, where sending 8000 would be
rejected outright. That logs `oneview.api_version_above_tested` at WARNING
naming `tools/verify_oneview.py`.

`INVENTORY_ONEVIEW_API_VERSION` overrides both, and is validated against
the appliance's own `[minimumVersion, currentVersion]` range before use.

Version→release, for reading the table above: 8.50/5600, 9.00/6600,
10.00/7600, 10.20/8000, 11.40/8800. HPE supports an API version for two
years after its release.

**Confirmed 2026-09-07, against a live appliance:** omitting the header
does not error — it silently serves an older schema (`status=200` either
way, but `members[0].type` was `server-hardware-12` with the header and
`server-hardware-1` without it). The header must always be sent; omitting
it fails silently rather than loudly.

## Pagination, and the 256 ceiling

Every collection GET returns `start`, `count`, `total`, `members`,
`nextPageUri`, `prevPageUri`. Following `nextPageUri` until it is null is
the **only** correct loop: HPE states the appliance "may limit the number
of resources returned", so `start += count` can skip members.

Two guards, both copied from the SDK (`resource.py:778`) and both real:
a `nextPageUri` equal to the page's own `uri`, and a repeat of a URI
already fetched. Appliances have returned the former; without the guard
the loop never terminates.

### Never send `count=-1` to `/rest/server-profiles`

Verbatim, and identical in both API references:

> "Providing a -1 for the count parameter will restrict the result set
> size to 64 server profiles. The maximum number of profiles is
> restricted to 256, i.e., if user requests more than 256, this will be
> internally limited to 256."

and, in the example prose: "If the number of profiles does not exceed the
limit, then all profiles are returned; otherwise, **the list is
truncated**."

This is the single most likely way to ship a collector that silently sees
a fraction of the estate. An explicit `count` is always sent
(`INVENTORY_ONEVIEW_PAGE_SIZE`, default 256, which is also safe for
`/rest/server-hardware`, whose maximum is documented only by absence).

### Truncation is detected, not assumed away

"The list is truncated" does not say whether `nextPageUri` is populated
past the 256th profile. Rather than guess, `get_all` compares what it
fetched against the collection's own `total` and, when fewer members were
fetched, logs `oneview.collection_truncated` at **ERROR** naming both
numbers and stating that servers were not collected.

A collector that silently sees a third of the estate looks exactly like a
healthy run against a smaller fleet — which is how a wrong number stays
invisible for weeks.

A truncated profiles, templates or hardware page means real servers were
never even listed — the same failure class as an unreachable UCS domain
or Redfish host, unlike a per-server unreadable subresource (handled by
`psus=None`/`unread_fields`, never a collection error). So each message
`get_all` records on `OneViewClient.truncations` — the three numbers in
one line, so an operator reading a failed CronJob can tell a lost
connection from the paging ceiling without opening the logs — is copied
onto `OneViewProvider.collection_errors` after the bulk calls, and the run
reports PARTIAL rather than a silently-complete success (ADR-0023).

**Confirmed 2026-09-07, against a live appliance with 685 profiles:** the
cap is **per request**, not per query. `nextPageUri` was followed past
the first 256 and fetched all 685. Paging works; no `filter` sharding is
needed even on an estate over the 256/request cap.

## The name trap

`ProviderServer.name` comes from the **server profile**. This is the same
trap ADR-0009 records for UCS, in the same shape.

| Field | HPE's own description |
|---|---|
| `server-hardware.name` | "For blade servers, it is the location based name of the server, which is formed by concatenating the enclosure name and the bay number. For rack servers, it is the serial number prefixed by word "ILO" (e.g. ILOUSE31835LS)." |
| `server-hardware.serverName` | "The name of the server as reported by the iLO. The iLO gets this information from a running operating system that has monitoring software installed, like Agentless Management Service." |
| `server-profiles[].name` | "Unique display name of this Server Profile." |

The first is a bay location and carries neither a site token nor a
classifiable pattern. The second is an OS hostname that only exists where
HPE AMS is running — a decoy. **Partially observed 2026-09-07**: on the
one sampled server the probe printed, `serverName` (`CP-five-8504`)
matched the profile name rather than `hardware.name`'s bay-location
string (`CP-five-8504-ilo`) — but whether AMS was actually running on
that host, and so whether this is the AMS-populated case or a
coincidence of naming convention, was not established. Whether
`serverName` is ever empty when AMS is absent is still open. Only the
third (`server-profiles[].name`) is trusted as the operator's name either
way — this collector never reads `serverName` for it.

**Hardware with no assigned profile is skipped**, counted, and logged
once per appliance as `oneview.hardware_without_profile`. Such a server
has no usable name at all: falling back to `ILO<serial>` would create a
document that parses to no site and matches no classification rule, and
an unassigned server is by definition carrying no workload.

### The join, and the serial

Hardware → profile is `server-hardware.serverProfileUri` ("If not
assigned this value is null"); profile → hardware is
`server-profiles[].serverHardwareUri`. The collector joins on the former.

`associatedServer` is a trap and is not used: it is a *serial*, not a
URI, and it is sticky — "the server hardware that the server profile is
currently applied to **or was most recently assigned to**".

`ProviderServer.serial` is `server-hardware.serialNumber`, the physical
one. **Never the profile's** `serialNumber`, which is documented as
possibly "a virtual serial number, user defined serial number or physical
serial number", with `serialNumberType` defaulting to `Virtual`. Ingest
correlates on `(vendor, serial_normalized)`, so a virtual serial would
split one machine into two documents.

## CPU, memory and the units

| This platform | OneView | Note |
|---|---|---|
| `cpu_sockets` | `processorCount` | "Number of processors installed" |
| `cpu_cores` | `processorCount * processorCoreCount` | **see below** |
| `cpu_threads` | `sum(Processors[].TotalThreads)` | **see "CPU threads" below** |
| `cpu_model` | `processorType` | |
| `memory_total_bytes` | `memoryMb * 1048576` | |

**`processorCoreCount` is per processor.** HPE: "Number of cores
available **per processor**". This platform's `cpu_cores` is a
whole-system figure (`..redfish.mapping` sums every processor's
`TotalCores`). Writing `cpu_cores = processorCoreCount` would under-report
every two-socket server by half, silently, forever. Either half absent
yields `None`, never a partial product.

**`memoryMb` is the best-documented unit of any vendor in this repo:**
"Amount of memory installed on this server hardware in MiB (1 MiB =
1,048,576 bytes)". The factor is spelled out inline. Contrast ADR-0017's
`TotalMemory`, which carries no documented unit anywhere.

### CPU threads

**No thread count exists on `server-hardware` itself** — there is no
`logicalProcessorCount`, `threadCount` or hyperthreading flag anywhere in
`ServerHardwareV12`. The only source is `/processors`' per-socket
`TotalThreads` — the same endpoint `tools/verify_oneview.py`'s
core-count cross-check already calls.

**Read for real, added 2026-09-07**, after the probe confirmed the
per-socket data exists and `Processors` showed up in `subResources`
alongside `PowerSupplies` on a live appliance. `cpu_threads_from` sums
every socket's `TotalThreads` (`mapping.py`), and `OneViewProvider`
fetches `Processors` the same two-tier way as power supplies: most
servers' `expand=all` response already carries it for free, and only the
rest cost a per-server `GET {uri}/processors`, bounded by a semaphore and
switchable off entirely (`INVENTORY_ONEVIEW_COLLECT_CPU_THREADS`,
`INVENTORY_ONEVIEW_CPU_THREADS_CONCURRENCY` — same shape as
`_COLLECT_PSUS`/`_PSU_CONCURRENCY`). Every other collector (Redfish,
UCS Manager/Central, Intersight) gets a real thread count for free, as a
field on data already being fetched for other reasons; OneView is the
only one where this genuinely costs a second per-server call, which is
why it stayed `None` until it did.

An absent socket (`Status.State == "Absent"`) is not counted, matching
every other absence rule in this mapping. A read with no numeric
`TotalThreads` anywhere reports `cpu_threads: None`, never zero —
**`2 x cores` is the heuristic ADR-0020 deleted from the Dell collector
and must not reappear here as a fallback for a failed read.**

**`/processors` is also the cross-check on the core-count multiplication,
and it is the probe's headline.** Each socket reports its own
`TotalCores`, so `sum(TotalCores)` measures the same quantity
`processorCount * processorCoreCount` computes. If they disagree, the
core count is wrong for the whole fleet, silently — `tools/verify_oneview.py`
prints the verdict twice, once at the top and once in its closing
summary. Confirmed matching on all 5 sampled servers, 2026-09-07.

## Subresources and `collectionState`

`GET /rest/server-hardware` returns subresource *metadata* with an empty
`data` field; `expand=all` populates it. That is the lever this collector
uses — one paginated pass instead of N per-server calls — paged at 25
(`client.EXPANDED_PAGE_SIZE`, deliberately a constant rather than a
setting, since response size is HPE's own reason for `expand` being off
by default).

Documented `SubResourceName` values: `AdvancedMemoryProtection`,
`Devices`, `LocalStorage`, `LocalStorageV2`, `MPSettings`, `Memory`,
`MemoryList`, `Unknown`. The collector reads `Devices`,
`LocalStorageV2` and `LocalStorage`. `Memory` is not read: this
platform's `ProviderServer` has no per-DIMM field, and `memoryMb` already
gives the total.

**Only `Collected` yields data.** Every other state maps to `None` —
"could not read this run" — never to zero or an empty list.

| State | HPE's meaning | Mapped to |
|---|---|---|
| `Collected` | "successfully collected … current at the time of collection" | the data |
| `CollectedStale` | "may be out of date **or missing** due to the server state … typically when the server is powered off or in POST" | `None` |
| `CollectionError` | "An error occurred during the collection" | `None` |
| `InsufficientFirmware` | "The iLO firmware on the server is too low … The minimum version to collect some types of inventory is iLO 5 v1.20." | `None` |
| `NotCollected` | initial state before any collection | `None` |
| `Unknown` | "Unable to determine … or null returned" | `None` |

`CollectedStale` is excluded deliberately, and it is the least obvious
row: "successfully collected" reads like usable data until you reach "or
missing". A powered-off server reporting zero drives is exactly the bug
the `None` contract exists to prevent — a `Storage` collection that
returned zeros once took a machine from CRITICAL to HEALTHY and logged
that the drive had recovered.

**Every subresource on an iLO-4 server returns `InsufficientFirmware`.**
The skips are logged once per appliance, aggregated by state and
generation (`oneview.subresources_unreadable`, e.g.
`{"InsufficientFirmware/iLO4": 112}`), not once per host: on a mixed
estate a per-host line would bury the run's real output.

**Confirmed on iLO 5/6, 2026-09-07 — still open for iLO 4** (this estate
has none to test against): the top-level fields populate near-universally
once collected — `memoryMb`/`processorCount`/`processorCoreCount`/
`processorType` were 797/797 on iLO 5 and 24/24 on iLO 6;
`portMap` was 781/797 and 23/24. The mechanism (per-subresource
`collectionState` deciding `None` vs. real) works as designed on real
data; whether iLO 4 specifically also returns `InsufficientFirmware` for
the top-level fields (not just the named subresources) is still
unverified without an iLO-4 host.

**Confirmed 2026-09-07, against a live appliance: `subResources` is a
JSON object keyed by name**, not an array — probe section 9 listed
subresource names directly as dict keys. The array-shaped branch in
`subresource()` is confirmed dead code for this appliance's API version
(6000); kept rather than deleted, since a different appliance version
could still use it and nothing rules that out.

## Storage — two schemas, one of them dangerous

> "Starting with Gen 10 Plus, certain storage adapters will provide
> `/localStorageV2` instead of (or in addition to) `/localStorage`."

So both are read, and **V2 wins only when it actually reports at least
one drive.** Confirmed against a live appliance 2026-09-07: a real server
(`ocp4-five-compute-08`, four SATA SSDs behind a Smart Array P408i-p) had
`LocalStorageV2` collected and genuinely empty (`data: {"Drives": []}`)
while its real drives were under `LocalStorage`. Falling back only on an
*unreadable* V2 (the original design) silently reported such a server as
having zero drives; the collector now falls back whenever V2 yields no
rows, whether that's because it's unreadable or because it read real and
empty.

**v1 is not a legacy path you can skip.** The split is at Gen10 Plus,
which means this estate's iLO-4/Gen9 half answers `/localStorage` only.
Both are read, and the schema is chosen by what the server actually
offers rather than guessed from its model string.

**`LocalStorage.data` is a list of `HpeSmartStorageArrayController`
objects, never a flat drive list.** Also confirmed against a live
appliance 2026-09-07, and the more consequential of the two storage bugs
that run found: each controller object carries its own `PhysicalDrives[]`
— mapping a controller object directly as if it were one drive produces a
`capacity_bytes: None` "drive" per *controller*, not per disk, silently
undercounting (or, for a single-controller server, reporting zero real
drives entirely). `_physical_drives_v1()` walks every controller in the
list and flattens their `PhysicalDrives[]` into one list before the
per-drive mapper runs; a server with two controllers gets both
controllers' drives concatenated, in controller order.

`LocalStorageV2` is stock Redfish `Storage`: `Drives[]` with
`CapacityBytes` documented in bytes, `MediaType`, `Protocol`,
`RotationSpeedRPM`, `FailurePredicted`,
`PredictedMediaLifeLeftPercent`, `Status`.

**One trap in it: `MediaType`'s enum is only `HDD` and `SSD` — NVMe
lives in `Protocol`.** Reading `MediaType` alone reports every NVMe drive
in the fleet as an SSD. This is the same class of mistake ADR-0020 had to
resolve on Dell by inferring media from three fields at once; here the
shared `..redfish.mapping.media_type_of` already checks `Protocol` first,
which is exactly why it is reused rather than reimplemented.

`LocalStorage` is HPE's SmartStorage schema with **three overlapping
capacity fields**:

| Field | HPE's description |
|---|---|
| `CapacityGB` | "Total capacity of the drive in GB. **This denotes the marketing capacity (base 10)**" |
| `CapacityMiB` | "Total capacity of the drive in MiB" |
| `CapacityLogicalBlocks` × `BlockSizeBytes` | exact |

The collector uses `CapacityMiB * 1048576`, falling back to
`CapacityLogicalBlocks * BlockSizeBytes`. **Never `CapacityGB`** — HPE
says outright it is the marketing number.

V1's `MediaType` also carries `SMR` (shingled magnetic recording), one
value the Redfish enum has no member for. It is a hard disk, and the
shared `..redfish.mapping.media_type_of` already maps it onto HDD rather
than losing it to UNKNOWN.

## NICs — `portMap`

`portMap.deviceSlots[].physicalPorts[]` supplies `nic_macs` and
`ProviderServer.nics`.

**`virtualPorts` are deliberately not included.** They are the
FlexNIC/partition MACs carved out of a physical port — the same
PHYSICAL-vs-VNIC distinction `ProviderAttachment.interface_kind` models
for UCS. Feeding both levels into `nic_macs` flat would inflate a set
ingest correlates identity on.

**No link speed and no link state exist anywhere in `portMap`.** There is
no `speedMbps` and no up/down field. `ProviderNic.speed_mbps` is `None`
and `link_state` is `"UNKNOWN"`; neither is synthesised.

`interconnectUri` / `interconnectPort` would be the natural
`ProviderAttachment` source for blades in an enclosure, mirroring UCS
fabric paths. Out of scope, noted so it is not rediscovered.

## The management-processor address

`mpHostInfo.mpIpAddresses[]` is a **list** mixing IPv4 and IPv6, each
entry carrying a `type`: `DHCP`, `Static`, `SLAAC`, `LinkLocal`,
`LinkLocal_Required`, `Lookup`, `Undefined`.

The collector discards `LinkLocal`, `LinkLocal_Required` and `SLAAC`
outright — an IPv6 link-local address is unroutable without a zone index,
which nothing downstream carries — then prefers `Static` → `DHCP` →
`Lookup` → `Undefined`, and falls back to `mpHostName`. The result is
stored as `https://<address>`.

**Confirmed 2026-09-07, against a live appliance (821 servers): every
server has a usable address.** 0 of 821 had no usable
`mpIpAddresses` entry, and the `Static` preference resolved correctly on
the sampled server (a `LinkLocal` IPv6 entry present alongside a `Static`
IPv4 one, correctly skipped in favor of the static address). Neither the
full ordering across all `type` values nor the guarantee that an entry is
*always* present is fully proven by one estate's data, but the design
held up against real, mixed IPv4/IPv6 `mpIpAddresses` lists.

## iLO identity

`mpModel` — "The model type of the iLO, **such as `iLO4`**." That is the
only documented example. There is no enum, no pattern, and nothing at all
about iLO 5/6/7.

So `mapping.ilo_generation` parses the trailing integer
(`re.search(r"(\d+)\s*$", ...)`) and reports a non-match as `None`
("unknown"), never as a guessed generation. Nothing branches on the
result — it is used only to make the aggregated unreadable-subresource
log say *which* hardware could not be read.

`mpFirmwareVersion`'s format is undocumented (conventionally
`2.78 Mar 15 2023`), so it is never parsed for a minimum-version gate.

**Confirmed 2026-09-07, against a live appliance (821 servers, iLO 5 and
iLO 6 both present):** `mpModel` is exactly the bare string the docs'
one example suggested (`"iLO5"`, `"iLO6"`), with `mpFirmwareVersion`
carrying the date-stamped version separately, matching the assumed
format. The trailing-integer parse correctly derived generation 10/11
(`shortModel` "Gen10"/"Gen11") for all 821 servers — no unrecognized
`mpModel` value was seen.

`mpLicenseType` is worth knowing about even though it is unused: `null`
means "OneView encountered a problem while fetching the license type",
which is not the same as unlicensed.

## GPUs

OneView reports GPUs as `Devices` subresource entries with
`DeviceType: "GPU"`, alongside NICs, storage controllers and empty slots.
HPE's own example shows an empty bay as
`{"DeviceType": "Unknown", "Name": "Empty slot 2", "Status": {"State": "Absent"}}`,
so `Status.State == "Absent"` is filtered before anything is counted.

Available per GPU: `Name` (the model string), `Manufacturer`, `Location`,
`SerialNumber`, `PartNumber`, `FirmwareVersion.Current.VersionString`,
`Status`.

**There is no GPU memory field. Anywhere.** Not in `HpeServerDevice`, not
on `server-hardware`, not in the `Processor` schema. So `memory_bytes` is
always `None` from this collector, and VRAM comes entirely from
`GpuCatalog` (ADR-0021) keyed on the model string.

### HPE product names and the catalog

HPE rebrands NVIDIA cards, so `Name` is HPE's *product name*, not the
chip's model string: `"HPE NVIDIA L40S 48GB PCIe Accelerator"` where a
BMC reports `"NVIDIA L40S"`. Those did not match the catalog as it stood.

Matching was extended (ADR-0022, "GPU matching"): a leading `HPE`/`HP`
and a trailing marketing noun are dropped in normalization, and lookup
falls back through a trailing bus word (`PCIe`, `SXM4`) and then a
`<model> <N>GB` spelling against a bare-model row — **accepted only when
N GB equals that row's own VRAM**.

That capacity check is the safety property: `"HPE NVIDIA A16 64GB PCIe
Accelerator"` matches nothing, because this table models the A16 as the
four 16GB GPUs it carries. A disagreeing capacity produces an honest miss
that an operator fixes with `INVENTORY_GPU_MODELS`, never a confident
wrong number.

**Still UNVERIFIED — run against a live appliance 2026-09-07, but that
estate has no GPU-bearing HPE server.** Probe section 7 reported zero
GPUs across all 821 servers, so neither the real product-name strings nor
the catalog HIT/MISS verdict could be observed. The rules above remain
built against realistic spellings, not observed ones. Re-run
`tools/verify_oneview.py` against a GPU-equipped HPE server when one is
available — that is still the outstanding action.

**Still UNVERIFIED, same reason:** whether GPUs also appear under
`/rest/server-hardware/{id}/processors` as `ProcessorType: "GPU"`. If they
do, a future Redfish pass would get GPUs for free on iLO 5+. No
GPU-bearing host to test against yet.

## Power supplies

**The one per-server call this collector makes, and the richest PSU
data any collector here reports.**

**Finding, 2026-09-07, against a live appliance: `expand=all` already
carries `PowerSupplies` for most servers.** 688 of 821 servers' bulk
sweep responses included a `PowerSupplies` subresource without the
per-server `/powerSupplies` call being made at all — meaning
`INVENTORY_ONEVIEW_COLLECT_PSUS` could default to `False` and still cover
84% of this estate for free, falling back to the per-server call only for
the remaining 133. Not yet acted on: the 688/821 split is one estate's
snapshot, not a guarantee every appliance version includes
`PowerSupplies` in its `expand=all` response, and changing the default
is a real behavior change worth its own decision rather than a side
effect of a field-test writeup. Left as a documented, actionable finding
for whoever picks it up next.

The history is worth keeping straight, because a first draft of this
section got it wrong. `ProviderServer.psus` was added on 2026-09-01 and
was genuinely dead for a while — `power.psu_count` and
`power.failed_psu_count` had nothing to read, so a server with a dead PSU
reported HEALTHY on power exactly like one with two good ones. That gap
was closed before OneView shipped, not by it: Intersight and UCS
Manager/Central populate `psus` for rack units (a blade's supplies belong
to its shared chassis), and `..redfish.mapping.psus_from_supplies` covers
Dell and every standalone BMC. **OneView is the fourth source, not the
first.**

What is still specific to OneView is the *quality* of the answer. Every
other collector reduces a vendor rollup to UP/DOWN/DISABLED/UNKNOWN;
HPE's `Oem.Hpe.PowerSupplyStatus.State` distinguishes `Failed` from
`Degraded` from `ACPowerLost` from `OverTemperature`, and
`PowerCapacityWatts` is documented in Watts rather than inferred.

`GET /rest/server-hardware/{id}/powerSupplies`, HPE's
`HpeServerPowerSupply` schema. What the mapper uses:

| Field | HPE's description |
|---|---|
| `PowerCapacityWatts` | "The maximum amount of power, in **Watts**, that the associated power supply is rated to deliver" |
| `Oem.Hpe.PowerSupplyStatus.State` | `Ok`, `Degraded`, `Failed`, `OverVoltage`, `OverCurrent`, `OverTemperature`, `ACPowerLost`, `FanFailure`, `WarningHighInputVoltage`, `GoodInStandby`, `Unknown`, … |
| `Status.Health` | the generic Redfish rollup |
| `Model`, `SerialNumber`, `MemberId` | free text |

**The HPE state decides health, not a boolean, and it reports in
`UP`/`DOWN`/`DISABLED`/`UNKNOWN` — never `HealthSeverity`.** It is the
more specific answer — a PSU that lost AC input is a different
operational fact from one that is degraded from one that failed — so
`Failed`, `ACPowerLost`, `OverVoltage`, `OverCurrent`, `OverTemperature`
and `FanFailure` map to `DOWN`, `Degraded` and the voltage warnings to
`UNKNOWN` (a degraded supply still delivering power has not lost
redundancy — the same rule `..redfish.mapping.psu_health` applies to
Redfish's own `Warning`), `Ok` and `GoodInStandby` to `UP`. A state this
platform has no mapping for falls back to `psu_health` itself, since a
OneView PSU row is "in JSON format based on RedFish schema" — the same
`Status.Health`/`Status.State` reading `psus_from_supplies` uses for
Dell and standalone BMCs. `Status.State == "Absent"` is an empty bay and
contributes no PSU at all.

**This was a real bug from 2026-09-01 to 2026-09-07: the mapping used to
report `HealthSeverity` values (`HEALTHY`/`WARNING`/`CRITICAL`)
instead.** `power.failed_psu_count` (`facts.py`) counts a PSU as failed
by checking `health == "DOWN"` — a string this mapping never produced,
so every HPE server's failed-PSU count was silently `0` regardless of
real state, for as long as OneView had ever run. No shipped default
health policy reads `power.failed_psu_count` yet, so this never produced
a visibly wrong health verdict — but the metric itself was wrong for
anyone querying it directly or writing a policy against it. Found while
investigating a live-hardware run's own output (not by inspection alone)
and fixed the same day; see `docs/adr/0022`'s "Results, 2026-09-07"
section. This is the *third* time this exact confusion — a vendor rollup
mapped to `HealthSeverity` where `power.failed_psu_count` expects
`UP`/`DOWN`/`DISABLED`/`UNKNOWN` — has shipped in this codebase (the
first was `facts.py`'s own original `"OK"` literal check; the second was
Intersight's `psu()`, corrected before it ever shipped un-fixed — see
that function's own docstring). Worth a shared guard if a fourth
collector is ever added.

Two documentation notes worth keeping: `LineInputVoltage`'s own
description says "in Watts", **which is an error in HPE's doc** — it is
volts, and this collector does not read it; and
`Oem.Hpe.AveragePowerOutputWatts` is "usually updated every 10 seconds
but the period can vary".

### The undocumented gap, and why it is handled rather than assumed

`/powerSupplies` returns a `SubResourceV10` envelope but has **no
matching `SubResourceName` enum value** — the enum has exactly eight
(`AdvancedMemoryProtection`, `Devices`, `LocalStorage`,
`LocalStorageV2`, `MPSettings`, `Memory`, `MemoryList`, `Unknown`) and
`/powerSupplies`, `/thermal`, `/processors` and `/networkAdapters` are
none of them. So whether `expand=all` already returns power supplies is
undetermined in HPE's own documentation.

The collector reads the expanded payload first and only calls per server
for what is missing, then logs `oneview.power_supply_source` with
`from_expand` and `per_server_calls`. That difference is ~15 requests
versus one per server, and an undocumented gap must not turn one into the
other silently. `tools/verify_oneview.py` section 10 settles it.

A `/powerSupplies` call that fails, or reports a `collectionState` other
than `Collected`, yields `None` for that server — unread, carried
forward — and is counted into one aggregated
`oneview.power_supplies_unreadable` warning. It never aborts the run.

Two details of that containment are load-bearing (moved here from the
code, 2026-09-13; `_processors` is the same shape). The per-server
`fetch` wraps the *parse* (`body.get(...)`, the `collectionState` check)
inside its `try` as well as the call: its whole job is to turn one
server's failure into `None`, and leaving the shape-reading outside held
only because `OneViewClient._request_json` happens to coerce a non-object
body to `{}` — a coercion two files away, not a guarantee this function's
contract should rest on. And the fetches are gathered with
`return_exceptions=True`: without it one escaping exception aborted
`gather` immediately and abandoned every other still-in-flight fetch
mid-request, so one bad PSU failed the whole appliance.

## What OneView does not have at all

Checked against the full `ServerHardwareV12` field list and every
documented subresource schema. Each of these is reported as `None` /
`UNKNOWN` and **never derived, inferred or defaulted**:

- **Thread / logical-processor count** at server level — only
  `/processors`, per server.
- **NIC link speed and link state** — absent from `portMap` entirely.
- **CPU or component temperature.** `/thermal` is **fans only**: the
  documented schema has five properties (`Name`, `Reading`,
  `ReadingUnits`, `Status`, `MemberId`) and no `Temperatures[]`, and
  `ReadingUnits`' only value is `Percent`, so there is no fan RPM either.
  The only temperature OneView has is `AmbientTemperature` in
  `/utilization` — **inlet air, in °C, and historical sampled data, not a
  current reading**. That is a different physical quantity from the
  per-component temperatures the Cisco collectors report, so it is not
  collected: putting an inlet sample into a component-temperature field
  would make two vendors' numbers look comparable when they are not. If
  inlet temperature is wanted later it needs its own field.
- **GPU memory** — see below.
- **Per-DIMM health** — only the aggregate `AmpModeStatus`.
- **Total storage capacity as a single field** — summed from the drives.

## Health fields, and one gotcha

`status` values are `OK` / `Disabled` / `Warning` / `Critical` /
`Unknown`, and **`Disabled` is also what an unassigned server reports**
("indicates that a resource is not operational *or that a server profile
has not been assigned*"). Mapping it straight onto a health state would
mark every spare server unhealthy. This collector does not map OneView's
`status` onto health at all — the health-policy engine works from
collected metrics — but the trap is recorded here because it is the
obvious thing for a future change to reach for.

`stateReason == "CommunicationError"` ("appliance cannot communicate with
iLO or OA") is the honest "OneView can't reach the iLO" signal and belongs
in `collection_errors`, not in a health verdict.

## One appliance, and the ceiling that would change that

`INVENTORY_ONEVIEW_IP` is a **single endpoint**, resolved through
`EnvConnectionResolver` like every other vendor's. There is no appliance
list, no per-appliance concurrency and no partial-success accounting:
one appliance failing is the run failing.

**The documented ceiling, recorded so nobody rediscovers it from a
truncated fleet.** HPE OneView 10.0 Support Matrix, docId
`sd00006056en_us`, "Configuration maximums":

| Resource | Maximum |
|---|---|
| Total servers per appliance | **2500** |
| …on a non-ESXi hypervisor | **1024** |
| Assigned server profiles | 2500 |
| **Unassigned** server profiles | **100** |
| Volumes per server profile | 512 |

> "The total number of servers in an HPE OneView **VM appliance cannot
> exceed 2500 servers if the VM OVA is deployed using a VMware vSphere
> ESXi hypervisor**. … **For hypervisors other than ESXi, the HPE OneView
> appliance can manage and monitor up to a maximum of 1024 servers.**"

This estate is one appliance and well inside that. **An estate that
outgrows it needs a second endpoint** — `INVENTORY_ONEVIEW_IP` becoming a
list, or a CronJob per appliance — and that is deliberately not built.
The symptom, if it is ever reached, is not a crash: it is
`oneview.collection_truncated` at ERROR, or a fleet that quietly stops
growing.

**Rate limits: not documented.** No requests-per-second, no concurrency
cap, no 429 in the response-code table (400, 401, 403, 404, 409, 410,
412, 415, 500, 503). The only hints are indirect — "OneView may limit the
number of resources returned" and 503 for "currently unable to handle the
request". `INVENTORY_ONEVIEW_PSU_CONCURRENCY` defaults to 8 for that
reason: one appliance is a single point of failure for up to 2500
servers, and ADR-0016's "embedded management hardware degrades when
polled" warning applies to it too.

## Why not the `hpeOneView` SDK

It is maintained (11.4.0, 2026-08-13) and small, so the Intersight reason
does not apply. It is rejected because the protocol is trivially small
while the SDK is synchronous (`http.client` directly — every call would
need `asyncio.to_thread`), depends on `future`, and pins `docutils<0.18`
across the whole air-gapped wheel mirror.

Four behaviours were taken from reading its source anyway, because they
are what a hand-rolled client learns the hard way:

| Behaviour | SDK evidence |
|---|---|
| `loginMsgAck` force-set on every login | `connection.py:468` |
| Default version = `GET /rest/version` → `currentVersion` | `connection.py:78-82` |
| Version validated against `[minimumVersion, currentVersion]` | `connection.py:85-93` |
| Page loop guards `nextPageUri == uri` | `resource.py:778` |

**Not** taken: its TLS default, which trusts any certificate unless a
bundle is passed. `INVENTORY_ONEVIEW_VERIFY_TLS` defaults off for the
stated reason that an air-gapped appliance ships a self-signed
certificate, and turning it on with a real chain is the scalable answer.
