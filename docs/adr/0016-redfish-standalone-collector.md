# 16. Redfish collector for standalone servers

Date: 2026-08-23

## Status

**Accepted and implemented, and now run once against real hardware** —
see "Update (2026-08-23): two defects found on the first real run" at the
end of this file. "What is still unproven" below is otherwise unchanged:
this was one operator's fleet, not a survey.

Implemented as `app.infrastructure.providers.redfish`, with a stdlib
test fixture (`tests/redfish_fixture.py`) serving a Redfish mockup over
`http.server`. It is hand-rolled because neither `sushy-tools` nor DMTF's
own mockup server implements `SessionService` at all, so neither can
exercise login, logout or a rejected credential — the only genuinely
non-trivial part of the client.

**No dependency was added.** The client is built on the `httpx` already
pinned, so `pyproject.toml`, `uv.lock`, `requirements.txt` and
`pylock.toml` are all unchanged — which is the concrete payoff of
rejecting both candidate libraries.

The six open questions it was reviewed against were settled on
2026-08-23 and are folded in below; `docs/notes/redfish-plan.md` §6 has
the reasoning for each.

The three defects in "Pre-existing defects this surfaced" were in shipped
code and are **fixed** — commits `1b2c10b`, `acca277`, `d8cdda2`.

## Context

Every collector this platform has points at an *aggregator*. UCS Central
is asked what exists and answers with 152 registered domains and their
addresses (ADR-0014); the collector fans out from there. `credentials/
env.py` encodes that model as a fact of the type system: one endpoint and
one login per `ManagerType`.

A growing set of machines has no aggregator. They are not registered with
UCS Manager, UCS Central, Intersight, OneView or OpenManage Enterprise,
and the operator's own example is a Cisco server whose CIMC speaks
Redfish but which Intersight cannot yet manage. The requirement, stated
directly: **any BMC that speaks conformant Redfish should be
collectable.**

That is not a smaller version of the existing problem. It inverts three
of its properties:

1. **Nothing enumerates the fleet.** A BMC knows about itself. The host
   list must come from something we own — the central new problem, and
   not a Redfish problem at all, since Redfish begins working only once
   an address is already known.
2. **Cost is per server, not per manager.** ADR-0014 could say "scale was
   not a factor" because a domain costs ~11 round trips whether it holds
   10 servers or 500. Here each host costs ~25 round trips against
   embedded hardware that degrades under polling. Bounded concurrency,
   per-host timeouts and a total-run budget stop being tuning knobs and
   become correctness requirements.
3. **Failure is normal.** With 400 independent devices some are always
   down. A run where 40 fail is a Tuesday, not an incident.

And it introduces a failure mode with no precedent in this codebase:
**account lockout**. Many BMCs lock an account after a few failed logins,
so a retry loop with a wrong password is actively destructive against the
estate's own management plane.

## Evidence

Held to ADR-0009's bar: confirm against the primary artefact, not a
documentation summary. The full research is in `docs/notes/
redfish-domain.md`; the load-bearing items were re-verified first-hand
against the downloaded DMTF schema bundle (2026.1) and DSP0266 1.14.0.

**On the schema.** Every resource's `required` list is only
`@odata.id`, `@odata.type`, `Id`, `Name` (plus `ChassisType` on Chassis).
Everything this collector reads is schema-optional, and §9.6.1
distinguishes *absent* ("not supported by this implementation") from
*null* ("supported, but unknown at the time of the operation") — a
distinction that turns out to decide a correctness bug (below).

Four findings changed the mapping:

- `ProcessorSummary` counts **central** processors and excludes GPUs.
  DMTF's own mockup reports `Members@odata.count: 10` against
  `ProcessorSummary.Count: 8`.
- `ProcessorSummary.CoreCount` is declared in
  `Namespace="ComputerSystem.v1_14_0"`, whose `Redfish.Release` is
  **2020.4** — absent on 2016–2019 firmware, so summing
  `Processor.TotalCores` is mandatory rather than defensive.
- `Drive.MediaType` is exactly `["HDD","SSD","SMR"]`; **NVMe is expressed
  through the separate `Protocol` property.** Reading only `MediaType`
  would report every NVMe drive in the fleet as an SSD.
- GPU memory is `Processor.MemorySummary.TotalMemorySizeMiB` — **MiB**,
  against `ComputerSystem.MemorySummary.TotalSystemMemoryGiB` in **GiB**.

**On real hardware.** iLO 7 before 1.22 *advertises* `$expand` and
returns HTTP 400, so `ProtocolFeaturesSupported` is a hint and never a
contract. ~30 iLOs failed fleet-wide at a 3-second timeout and 20 seconds
fixed it. `LogServices` scrapes have been measured at ~10 minutes.
Lockout defaults differ in kind, not degree: Dell blocks the **source
IP** (3 failures / 60 s → 1 hour), Lenovo XCC locks the **account** and
can be configured to hold it until an admin intervenes, HPE merely
delays.

**On the client library.** `DMTF/python-redfish-library` hardcodes
`verify = False` inside its request loop, overridable only by supplying a
CA file, and calls `disable_warnings()` process-wide at import; it
defaults to no timeout and 10 retries, and logs response headers
unredacted — where `X-Auth-Token` lives. `sushy` is better engineered
(`verify=True`, split timeouts, never retries `SSLError`) but is
sync-only and untyped. Both were read from source, not documentation.

**On testing without hardware.** `sushy-tools` was installed and run:
**it has no `SessionService` at all** — zero matches for
`sessionservice`/`x-auth-token` in the package, and its ServiceRoot
template has no `Links` block. DMTF's own mockup server is not on PyPI,
is stale, and also has no auth. Neither can exercise session login,
the only non-trivial part of the client.

## Decision

Build `app.infrastructure.providers.redfish` as a peer of `ucs_manager`,
registered in `_PROVIDER_FACTORIES` with its own CronJob.

**The runtime client is ours, on the already-pinned `httpx==0.28.1`.**
No new dependency, native async, `py.typed` under `mypy --strict`, and
zero new air-gap mirror entries. The knowledge is carried over rather
than the code: `Connection: close` (sushy's field studies), split
connect/read timeouts, never retrying an `SSLError`, and redacting
`X-Auth-Token` in *response* logging — the precise place the DMTF library
leaks it. Revisit sushy if vendor-specific branches pass ~5.

**Configuration is a login with no endpoint** — the same carve-out
`UCS_MANAGER` uses, and the reason `credentials/env.py` has two maps
rather than one. The fleet comes from a **TOML** inventory file mounted
from a ConfigMap, parsed with stdlib `tomllib` so it adds no dependency
and no air-gap mirror entry; per-host credential overrides are referenced
**by name** from a Secret. A homogeneous fleet therefore needs two
environment variables and a list of hosts.

TOML over YAML because `pyyaml` reaches this project only transitively
via `uvicorn[standard]`, so using it properly would mean a direct pin and
regenerated lock files. TOML over a bare list because the file needs
**comments** — `verify_tls = false  # INC-1234` is how a relaxed security
control stays reviewable — and **grouping**, so a per-site credential is
expressible without inventing syntax.

The credential resolution chain (host → group → defaults → the
fleet-wide login) is built in the first slice rather than deferred. Its
value is not exotic hardware support: it is **load-time validation**. A
typo'd group name fails the run naming the known groups, instead of
silently falling through to the default service account and presenting it
to a machine it was never meant for.

**This deviates from ADR-0012's "no secret volume to mount", and the
deviation is named rather than slipped in.** That statement was about
per-manager credential *directories* alongside `Manager` documents, whose
harm was a second source of truth. A host list is neither secret nor a
second source of truth — it is data, hundreds of lines, changing when the
estate changes rather than when the deployment does.

**Authentication is Redfish session auth**, with `logout()` guaranteed by
`async with` *and* shielded against cancellation. Basic is
spec-guaranteed and stateless, but makes every one of ~25 GETs a login
event — 25× the lockout pressure when a credential is wrong, and on an
LDAP-backed account 25 directory round trips per server.

**Two safety invariants bound the blast radius:**

> Never authenticate to a host that has not already answered
> `/redfish/v1` with a valid Redfish ServiceRoot over a verified TLS
> connection.

> A credential is only ever presented to a host listed in the inventory.
> Never to an address found by scanning.

The first costs one GET (ServiceRoot is unauthenticated by
specification) and means a typo'd address that is not a BMC fails the
probe while one that is not ours fails certificate verification —
neither ever receives a password. The second is enforced structurally:
the collector contains no discovery code path, and discovery, if ever
built, is a separate program that cannot present a credential.

**Vendor is the manufacturer, mapped.** `ComputerSystem.Manufacturer` →
`dell`/`cisco`/`hp` where recognized, `Vendor.STANDALONE` where present
but unrecognized **or absent/null** (reversed 2026-08-23 — see the dated
update below). "Has no manager" is carried by `source_provider`, not by
`vendor`, because `IngestService` correlates on
`(vendor, serial_normalized)` — putting management state into the vendor
field means a machine splits into two documents the day it gains a
manager.

**`INVENTORY_COLLECTOR_NAME_PATTERN` does not apply.** The pattern exists
because a vendor manager holds the whole datacenter and the name is the
only discriminator. Here the inventory list *is* the filter, and a more
precise one. Applying `^ocp` over a name a BMC does not know would
discard every host the operator deliberately listed.

**`attachments` is empty and `gpus` is best-effort.** A standalone server
has no fabric interconnect; emitting NIC link state as pseudo-attachments
would make the seeded `connectivity.fabric_paths_down` policies evaluate
against fiction. An empty `gpus` tuple means "not discoverable here",
never "none installed".

**The CI fixture is ours too** — ~100 lines built on `sushy-static`'s
118-line Apache-2.0 stdlib skeleton, plus session auth, expiry, and a
`path → (status, delay)` fault-injection table. That combination tests
what no off-the-shelf option can: a 401 without a token, a session
expiring mid-run, a 500 on one collection member, and a deliberately hung
request.

## Pre-existing defects this surfaced

Found while designing, verified first-hand, not caused by this feature.
Fixed as separate commits ahead of the collector.

**`uniq_system_uuid` rejects the second server with no UUID.**
`partialFilterExpression={"identity.system_uuid": {"$exists": True}}`
matches a field present *with a null value*, and `model_dump(mode="json")`
always emits the key. Reproduced against live MongoDB with the repo's own
`SERVER_INDEXES`: the second null-UUID insert raises `DuplicateKeyError`,
`_ingest_one`'s recovery finds nothing by serial and re-raises, and the
server fails ingest on every run forever. `ComputerSystem.UUID` is
schema-optional, so this blocks OpenBMC whiteboxes and older firmware
outright. It has never fired because UCS always reports a UUID and no
test covers two servers without one. Fix: `{"$type": "string"}`. **"UCS
always reports a UUID" turned out to be true but not the reassurance it
reads as here — a live domain reported the same one twice; see the dated
update below.**

**`run_collector` never writes the `Manager` document.**
`IngestService.ingest` upserts managers only from its `managers=`
argument; `seed_inventory.py` passes it and `run_collector.py` does not.
So `manager_for()`'s docstring and `CLAUDE.md` both describe behaviour
that does not happen — every UCS-collected server's `manager_id` points
at a document that does not exist.

**A partial read silently heals a real fault.** `_build_server` carries
forward only `classification`, `health`, `maintenance` and `openshift`,
and rebuilds `hardware` from the `ProviderServer` every run, into a
`replace_one(upsert=True)`. A host whose `Storage` collection 404s —
which sushy 5.10.0 had to handle because HGX boards do exactly this —
yields zeros that overwrite good data. `storage.failed_drive_count`
becomes 0, the seeded `storage.failed_drive` policy stops firing, and
**a server with a genuinely failed disk flips from CRITICAL to
HEALTHY** — with a `HEALTH_STATUS_CHANGED` audit event asserting the
recovery, and `last_seen_at` stamped fresh so staleness monitoring reads
the hollowed-out document as healthy.

This is the same class as ADR-0009's fabric-path defect: the run
succeeds, the numbers are plausible, and a health signal silently
switches off. The fix is at the port — `ProviderServer`'s `int = 0`
defaults cannot express "unknown", so the schema's own absent-vs-null
distinction dies at the DTO boundary. The numeric fields become
`int | None` and `_build_server` carries the previous value forward,
exactly as it already does for classification and health.

## What is still unproven

Held to ADR-0009's standard: what a live run has *not* settled.

**One BMC has been touched, not a survey.** Before 2026-08-23 this rested
entirely on the DMTF schema, DMTF's own mockups, and vendor documentation.
The first real run already found two defects — see the dated update at
the end of this file — matching ADR-0009's experience closely enough to
repeat its warning rather than retire it: that ADR found five real
defects invisible without hardware — a nonexistent MO class, a BMC filter
matching nothing, a whole class of adapter interface never collected,
fabric counts always zero, and servers named after their chassis slot.
**Assume this design still has more of its own**, on hardware and vendors
not yet run against.

Specifically unsettled:

- **Whether Dell iDRAC or HPE iLO populate `Processors` with
  `ProcessorType: "GPU"`** for arbitrary add-in GPUs. The path is
  standard and Lenovo XCC implements it; no evidence was found for the
  two vendors actually in scope. `gpus` therefore ships best-effort.
- **Whether the fleet's firmware honours `$expand`.** Probed and verified
  per host at runtime, so being wrong costs latency and never
  correctness — but the ratio is unknown.
- **Whether `Accept-Encoding: identity` is safe.** Proposed as a
  decompression-bomb defence; the research found some BMCs reject it.
- **Whether the string `"N/A"` ever appears in a numerically-typed
  field.** Widely assumed, including in this project's own earlier
  notes; **no primary citation was found.** What *is* confirmed is `null`
  where a number is expected, and keys absent entirely. Coded for
  defensively, but not claimed as established.
- **The per-host request count (~25) is derived from the mapping, not
  measured.** Every concurrency and budget number scales linearly with
  it.
- **The circuit breaker cannot prevent Dell's IP block.** With 16 hosts
  in flight, 16 logins are dispatched before the first 401 returns, so
  the effective threshold is concurrency rather than N — and N=3 is
  already past Dell's documented 3-failures-in-60 s default. **The
  breaker bounds damage; it does not prevent lockout.** Only the
  pre-flight prevents.
- **`Connection: close` may be miscited.** sushy's comment concerns
  long-running persistent connections; a 25-request burst may not be that
  case, and forcing ~12,500 TLS handshakes per run is a real cost.
- **Supermicro is out of scope and untested**, and its Redfish may be
  licence-gated behind `SFT-OOB-LIC` with an undocumented free/licensed
  boundary. Recorded because the expected symptom — a server ingesting
  with a name and serial and almost nothing else — reads as a collector
  bug. Check licensing first. Lenovo XCC and OpenBMC are equally
  untested; they map to `standalone` and nothing more is claimed.
- **HPE iLO 4 is excluded on conformance grounds**, not vendor grounds:
  `MacAddress` rather than `MACAddress`, `Power` rather than
  `PowerState`, the pre-Redfish dotted `@odata.type`, and no standard
  `Storage` at all. Any equally divergent BMC of any brand fails the same
  gate.

## Settled review decisions

The four remaining questions this ADR was reviewed against, and what was
decided.

**The credential breaker trips at three distinct BMCs, and says so
loudly.** Three *different* hosts rejecting the same credential disables
it for the rest of the run: remaining hosts on that credential are
skipped without a connection attempt, hosts on other credentials
continue, and one aggregate error names the credential and the hosts that
rejected it. Three attempts against a *single* BMC cannot arise — a 401
is never retried, so one host produces at most one authentication failure
per run.

**Stated plainly because it is easy to over-claim: this bounds damage, it
does not prevent lockout.** With 16 hosts contacted concurrently, ~16
logins are already in flight before the first rejection returns, so the
effective threshold is concurrency rather than three — and Dell blocks
the source IP at three failures, which is reached before the breaker can
act. The pre-flight against a single host is the mechanism that
prevents; the breaker is the backstop behind it.

**Exit 3 keeps its meaning, and nothing alerts on Job status.** A partial
run must never report success — that is how a bad credential stays
invisible for weeks. But with PARTIAL as the *normal* outcome for
hundreds of independent BMCs, the Job is routinely red, and an alert
nobody can act on gets muted before the day it matters. Alerting is on
staleness instead. `backoffLimit` is **1**, not 0: a ConfigMap mount can
lag at pod start and read as "inventory absent", and with no retry that
loses an entire collection cycle. The retry-sprays-credentials risk that
argued for 0 is already covered by the pre-flight and the breaker.

**`Accept-Encoding: identity` is not sent.** It was proposed as a
one-line decompression-bomb defence, but some BMCs reject the header
outright — turning a theoretical attack into a real, silent zero-data
failure on those hosts, with nothing pointing at the cause. A streaming
byte cap gives the same protection with no compatibility risk.

## Consequences

**A machine no aggregator owns becomes collectable**, including machines
from vendors this platform already has a manager-based collector for.
This does **not** restore a UCS Manager entry point: `--manager-type
UCS_MANAGER` remains deleted (ADR-0014), and reaching a Cisco CIMC over
Redfish is a different protocol to a different endpoint that knows
nothing about domains or service profiles.

**The inventory file is a production-critical, review-gated artefact.**
With `^ocp` correctly not applied, it is the *only* collection filter —
there is no second line of defence. Write access to it is equivalent to
write access to the credential Secret, because it decides where the
credential is sent. It belongs in git and reaches the cluster through
GitOps.

**Supported scale is ~400–1000 hosts per CronJob, extended by sharding
the inventory across CronJobs.** This does **not** reach the platform's
10,000-server target in this shape; at that size the per-server cost
model requires a continuously-running worker pool with a persistent
queue, which is a different program rather than a bigger CronJob. The
10,000 figure is about inventory size, most of which arrives through
aggregators — standalone is the long tail.

**Collector-side Prometheus metrics are impossible.** `/metrics` is
served by the API process; a CronJob pod lives minutes and is never
scraped. So "40 hosts have been failing for two weeks" — a failure of
*absence* — can only be answered by staleness gauges derived from
MongoDB's `last_seen_at`, exported by the API. Until that lands,
staleness detection is a documented manual query, and the ADR says so
rather than implying coverage that does not exist.

**PARTIAL is the normal outcome.** Exit 3 will be common and must not be
paged on; alerting moves to staleness. A run that authenticated nowhere
exits 1, not 3.

**Nothing tombstones a server.** A host removed from the inventory keeps
its document with a frozen `last_seen_at`, and no delete path exists
anywhere in this codebase. Deleting an inventory line makes a server
invisible-but-present.

**A machine listed here *and* registered with an aggregator is collected
by both.** Correctness converges — they share `(vendor, serial)` and
therefore one document — but `manager_id` and `source_provider` are
single-valued and flip each cycle. Documented, not detected.

## Update (2026-08-23): two defects found on the first real run

Reported against the operator's own hardware, the day this ADR was
written. Both fixed; neither had been caught by the CI fixture, and both
are the exact class this ADR's "What is still unproven" warned about —
untested assumptions the schema alone could not rule out.

**The client forced a trailing slash onto the session-creation URI, and
real hardware rejected it.** `_login()` unconditionally appended `/` to
whatever the service root advertised at `Links.Sessions` or
`SessionService.@odata.id` before POSTing to it — a rewrite of a
server-supplied `@odata.id` that DSP0266 gives no basis for and that this
ADR's own Evidence section never claimed to have researched. A live BMC
answers its Sessions collection at an exact path and 404s the same URI
with a trailing slash appended, so every login failed against it. The CI
fixture never caught this because its own routing matches by
`str.startswith`, tolerating exactly the mistake real hardware does not
— a reminder that a hand-rolled test fixture can be *more* permissive
than the thing it stands in for, not just less capable. Fixed by posting
the advertised URI unmodified
(`app.infrastructure.providers.redfish.client._login`).

**`MemorySummary.TotalSystemMemoryGiB` is absent on real hardware**,
despite `system_to_provider_server` treating it as the only source of a
server's memory. The property is schema-optional — confirmed already in
this ADR's Evidence section for `ProcessorSummary`/`CoreCount`, just not
carried over to memory at the time. The reported BMC populates `Memory`
(the `ComputerSystem` navigation link to a `MemoryCollection`, one member
per installed DIMM, confirmed present since `ComputerSystem` v1_1_0)
instead, each member's own `CapacityMiB` — schema-optional too, but
populated on this hardware. `mapping.memory_bytes` now sums
`Memory[].CapacityMiB` across every non-absent DIMM
(`Status.State != "Absent"`, the same empty-bay signal already relied on
for `Drive`) whenever `MemorySummary` is absent or unparseable — the
identical "required fallback, not a defensive one" shape `cpu_summary`
already used for `CoreCount`. `provider.py` fetches `Memory` through the
same `_optional()` used for `Processors`/`EthernetInterfaces`, so a BMC
that cannot serve it degrades to `None` rather than failing the host.

Neither defect changed anything about the account-lockout safety design
— both are read-path mapping/transport bugs, not authentication-retry
behaviour.

## Update (2026-08-23): GPU telemetry for DGX/HGX-class fleets

Requested for an operator's A100/H100/H200/B200/B300 fleet. Verified
against DMTF's `Processor.v1_22_0`, `ProcessorMetrics.v1_6_0`,
`EnvironmentMetrics.v1_5_0`, `PCIeDevice.v1_16_0` and
`PCIeFunction.v1_5_0` schemas — not yet against real DGX/HGX hardware.

**Added to `Gpu`:** `memory_type` (`ProcessorMemory[].MemoryType`, e.g.
`"HBM3"`/`"HBM3e"` — distinguishes an H100 from an H200 by memory
generation rather than model string alone), `ecc_mode_enabled`
(`MemorySummary.ECCModeEnabled`), `correctable_error_count` /
`uncorrectable_error_count`, and `temperature_celsius` /
`power_watts`.

**The error counts are a real ambiguity, not a confirmed mapping.**
`ProcessorMetrics` scopes `Correctable`/`UncorrectableErrorCount` to
"Core" and "Other" components without specifying which bucket a GPU's
own HBM stacks report under — the schema is shared between CPU and GPU
`Processor` entries and was clearly written CPU-first. Both buckets are
summed into one correctable/one uncorrectable total rather than guessed
apart, since neither vendor documentation nor a live run has settled it.
If a real run shows one bucket always zero for a GPU, or shows vendor
`Oem` extensions carrying more precise HBM-specific counts DMTF does not
model, that is the next thing to fold in.

**`pci_address` stays unpopulated, deliberately.** Went looking for a
source and found none: DMTF's schema has no bus:device.function
addressing field anywhere in `PCIeDevice` or `PCIeFunction` — only
`VendorId`/`DeviceId`/`FunctionId`/slot location. Some vendors set a
`PCIeDevice`'s own `Id` to something BDF-shaped as a convention, but
that is not spec-guaranteed, so it was not read as one. Revisit with a
real BMC's `PCIeDevice.Id` in hand rather than assuming the convention
holds.

**Cost is no longer flat per GPU discovery — it now scales with GPU
count.** Each GPU's `Metrics` and `EnvironmentMetrics` are separate
per-processor resources, not part of the `Processors` collection fetch,
so reading them costs two more requests per GPU: +2 for a single add-in
GPU, +16 for an 8-GPU DGX-class baseboard. This is exactly the kind of
cost this ADR already treats as a correctness requirement rather than a
tuning knob (see "Cost is per server, not per manager" in Context) —
the per-host budget and timeouts already in place bound it, but a
fleet's `host_budget_seconds` should be re-checked against a real
8-GPU host's response time rather than assumed adequate. Each fetch is
independently tolerant of failure (`_optional_link`, the single-resource
analogue of `_optional`): one GPU's metrics 500ing degrades that GPU's
telemetry to `None`, never the host.

**The frontend was fixed alongside this**, not held back for a separate
pass: `HardwareTab.tsx`'s GPU section only ever rendered `model`, so
`memory_bytes` (VRAM) was already being collected and returned by the
API on every server without ever being visible — the reason the
operator could not see it was a display gap, not a collection gap. Now
renders every field as a table, matching how storage drives already
render.

## Update (2026-08-23): a missing Manufacturer no longer fails the system

Reversed at the operator's explicit request. Through this point, an
absent or null `ComputerSystem.Manufacturer` raised `ValueError` in
`system_to_provider_server`, which `provider.py` caught to skip that one
system and record a collection error — reasoned about at length in the
Decision section above as the mirror image of mapping an *unrecognized*
manufacturer string to `Vendor.STANDALONE`: guessing a vendor for a
genuinely unreadable property risks a machine splitting into two
documents the day the property starts reporting.

`vendor_from_manufacturer` now returns `Vendor.STANDALONE` for that case
too — a missing/null Manufacturer and a present-but-unrecognized one are
treated identically. `system_to_provider_server` can no longer raise at
all, so the `try/except ValueError` around it in `provider.py`'s
collection loop was removed as dead code rather than left in place.

**The correlation-key risk this reopens is real, not hypothetical, and
is now accepted rather than avoided.** A server ingested once under
`STANDALONE` because its BMC did not report `Manufacturer` on that run,
then later reporting a real manufacturer (firmware update, transient
read fixed, licensing unlocked) is `(vendor, serial)`-keyed — a vendor
change is a new document, not an update to the old one, and the old
`STANDALONE` document is orphaned rather than corrected. The prior
design accepted a *skip* to avoid this; the current one accepts the
*split* to keep every host with a listed BMC actually ingested, on the
view that a whitebox/OEM system permanently reporting no `Manufacturer`
is common enough in the field that failing it outright cost more than
this risk does. If the split is observed in practice, the fix is
correlation-time reconciliation (detecting a `STANDALONE` document and a
newer real-vendor document that plausibly describe the same host), not
reverting this change.

## Update (2026-08-23): DGX/HGX GPU-baseboard systems are merged into their host

The operator's earlier question ("what's the difference between
`/redfish/v1/Systems/DGX` and `/redfish/v1/Systems/HGX_Baseboard_0`?")
surfaced a real bug rather than a curiosity. Confirmed against NVIDIA's
own docs (the DGX B300 user guide and the DGX GB200 rack-scale guide):
NVIDIA's DGX/HGX platforms model one physical machine as **two**
`ComputerSystem` resources — a host system (`DGX`, or `System_0` on
rack-scale) carrying BIOS/CPU/host memory/host storage, and a separate
GPU-baseboard system (`HGX_Baseboard_0`) carrying only the GPUs
(`Processors/GPU_SXM_<id>`) and their telemetry. On rack-scale GB200
systems this goes further still — separate management controllers
(`BMC_0` vs `HGX_BMC_0`) for the two trays.

`_collect_systems` already anticipated "one BMC, several systems," but
only for genuinely independent hosts sharing a BMC (OpenBMC multi-node).
It had no way to recognize that a DGX's two systems are one physical
machine, so each would ingest as its own server: the host with CPU/
memory/storage but zero GPUs, and the baseboard with 8 GPUs but no CPU
and — until the Manufacturer change above — no vendor either, failing
collection outright.

**`mapping.has_only_gpu_processors`** recognizes a GPU-baseboard tray by
shape, not by name (`HGX_Baseboard_0` is NVIDIA's current convention,
not a guaranteed one to match against forever): every non-absent
`Processor` reports `ProcessorType == "GPU"` and none looks like a CPU
(reusing `cpu_summary`'s own convention that an unmarked processor
defaults to `"CPU"`). `_collect_systems` reads every system's
`Processors` up front, partitions them into trays and hosts by that
test, and — **only when there is exactly one host** — folds every
tray's already-mapped GPUs into that host's `ProviderServer` via a new
`extra_gpus` parameter on `system_to_provider_server`, and does not
ingest the tray as its own server at all.

**The one-host requirement is a deliberate safety rail, not an
oversight.** Zero hosts, or more than one, means there is no way to
know which host a tray's GPUs belong to — merging would be a guess.
In that case nothing is merged: every system, tray included, ingests
independently exactly as before (a tray now succeeds as
`vendor: standalone` rather than failing, per the Manufacturer change
above, rather than being silently dropped), and a
`redfish.gpu_baseboard_ambiguous` warning names what was found. The
common one-host-one-or-more-trays case logs
`redfish.gpu_baseboard_merged` per tray instead.

Not yet run against real DGX/HGX hardware — verified against NVIDIA's
documentation and a hand-built fixture shaped to match it
(`_with_hgx_baseboard` in the test file), not a live BMC. The one thing
most worth confirming on a real run: whether `HGX_Baseboard_0` genuinely
reports zero CPU-type `Processors` entries (rather than, say, omitting
`Processors` from its service root link entirely, in which case
`has_only_gpu_processors` correctly returns `False` for it and it would
be — wrongly — treated as an unmergeable second host).

## Update (2026-09-15): a real tray has a non-CPU companion, not just GPUs

Run against real DGX/HGX-class hardware for the first time (operator's
air-gapped estate): a GPU-baseboard tray reported 8 GPUs *and* one FPGA
`Processor` entry. `has_only_gpu_processors`'s "every processor is a
GPU" rule rejected it as a tray outright, so it would have ingested as
its own zero-CPU, zero-vendor server instead of merging into its host.

**Redefined** from "every non-absent processor is a GPU" to "at least
one is a GPU and none is a CPU" — a non-CPU, non-GPU companion (this
FPGA, an NVSwitch, ...) no longer disqualifies the tray. The CPU check
keeps `cpu_summary`'s own default: a `Processor` entry with no
`ProcessorType` at all is still a CPU, not a companion device, so a
normal host with unmarked CPUs and a GPU add-in card cannot misclassify
as an all-GPU tray under the loosened rule.

## Update (2026-09-15): a GPU reported only as a `PCIeDevice` is invisible to `gpus_from_processors`

A real host in the operator's air-gapped estate reported 0 GPUs despite
having 8 NVIDIA cards installed. Its `Processors` collection held only
its two CPUs — no `ProcessorType == "GPU"` entries at all — because this
BMC enumerates GPUs as `ComputerSystem.PCIeDevices` entries instead,
confirmed live:

```json
{
  "@odata.id": "/redfish/v1/Chassis/Self/PCIeDevices/00_4E_00",
  "Description": "10DE VGA",
  "Manufacturer": "10DE20B2",
  "DeviceType": "Simulated",
  "PCIeInterface": { "LanesInUse": 16, "MaxPCIeType": "Gen4" },
  "Status": { "Health": "Ok", "State": "Enabled" }
}
```

Confirmed against real hardware, not a mockup, despite `DeviceType:
"Simulated"` — a real, if confusing, BMC quirk worth recording as-is
rather than assumed to mean the host itself is fake. `Manufacturer`
carries the PCI-SIG vendor ID concatenated with a device ID
(`10DE20B2`: `10DE` is NVIDIA's registered ID), not a human-readable
name — `Description` is the closest thing to a model string this
resource offers, and it is exactly that generic (`"10DE VGA"`, not a
real product name).

**Built as an opt-in, bounded fallback** — `INVENTORY_REDFISH_PCIE_GPU_DETECTION`
(default off), `_MAX_DEVICES`, `_MAX_GPUS` — triggered only when
`Processors` reported zero GPUs, never replacing that path.
`is_gpu_pcie_device` requires **both** a known GPU vendor's PCI-SIG ID
(`10DE`/NVIDIA confirmed live; `1002`/AMD and `8086`/Intel included on
the same convention but unresearched) **and** a display/GPU-shaped
`Description` — vendor ID alone is not trusted, since Intel in
particular ships far more non-GPU PCIe silicon than GPU.

**A real, documented capability ceiling, not a bug**: `PCIeDevice` has
no VRAM, ECC, error-count, temperature or power property of any kind —
every one of `Gpu`'s telemetry fields is `None` by construction for a
device found this way. This fallback answers "does this server have N
GPUs", never "what state are they in" — a DGX/HGX box whose GPUs come
through `Processors` still gets the fuller shape from
`gpus_from_processors`, unaffected.

**Cost, settled**: screening every `PCIeDevice` individually (this BMC
has 214 of them) measured ~6.5 minutes for one host — the operator then
checked `ProtocolFeaturesSupported` across their fleet and confirmed
most BMCs (Redfish 1.7) advertise `ExpandQuery.NoLinks: true` with
`MaxLevels: 5` (a smaller, newer 1.11 subset adds `DeepOperations` on
top, unrelated here). `_pcie_scan_query` requests
`$expand=.($levels=1)&$select=Manufacturer,Description,Status` on the
`PCIeDevices` collection whenever `NoLinks` is advertised — DSP0266
§7.3.2 confirms `$expand` and `$select` are combinable — collapsing what
would be 214 individual requests into a handful of paginated ones.
`_paged_members` mirrors `RedfishClient.get_collection`'s own
already-expanded-vs-reference-only per-member handling, so a BMC that
does *not* honor the query degrades to exactly the original per-device
`$select`-only scan, automatically, with no separate code path. Either
way, paging stops at `pcie_gpu_max_devices` — a cap `pcie_gpu_max_gpus`
alone cannot give on a BMC that ignores `$expand`, since without it
`get_collection`'s own unbounded resolve-every-member behavior would
still cost one request per device regardless of how few GPUs are found.

## Update (2026-09-15, continued): a second real shape, and `$expand` confirmed unreliable on it

Tested live against a DGX H100 (150.3.20.83): `Systems/DGX.PCIeDevices`
is a **direct array of links on the `ComputerSystem` itself**
(`"PCIeDevices": [{"@odata.id": ...}, ...]`, `"PCIeDevices@odata.count":
133`) — confirmed against DMTF's own Resource and Schema Guide as a
real, distinct property from `Chassis.PCIeDevices`, which links to an
actual `PCIeDeviceCollection` resource instead (the 150.3.20.19 shape
above). `GET Systems/DGX?$expand=PCIeDevices` (the named-property form,
not `.`/`*`) returned successfully but with **zero** entries despite the
133 real references — a silent, dangerous failure mode: nothing errors,
so a naive reader would conclude the host has no GPUs at all.

**Built:** `pcie_device_refs` reads the array directly off the system —
no collection to `$expand` at all, so `_pcie_gpus` never attempts it for
this shape, only ever fetching each reference with `$select`. Both
shapes are now checked and merged (deduplicated by `@odata.id`, capped
at `pcie_gpu_max_devices` combined) since a system could plausibly carry
either or, in principle, both. **Also added, for the collection shape**:
`_paged_members` now checks its first `$expand`ed page for exactly the
DGX's failure signature — an advertised `Members@odata.count > 0` with
zero returned `Members` — and retries the whole collection without
`$expand` when it sees that, rather than trusting an empty result.

## Update (2026-09-15, continued again): PSU real-time draw lives on a separate `PowerSupplyMetrics`, not `PowerSupply` itself

Operator report: every PSU line printed `draw unknown` — `power_watts`
had been left `None` "by construction" pending research (this ADR's
GPU telemetry update did the equivalent research for GPUs but PSUs were
never followed up). Checked DMTF's current `PowerSupply.v1_5_1` and
`PowerSupplyMetrics.v1_1_2` schemas directly: `PowerCapacityWatts`
(rated capacity, already read correctly) **is** on `PowerSupply` itself,
but real-time draw is not — it lives on a linked `PowerSupplyMetrics`
resource (`PowerSupply.Metrics` → `{"@odata.id": ".../Metrics"}`),
`InputPowerWatts` a `SensorPowerExcerpt` (a `.Reading` field), the exact
same shape `_sensor_reading` already parses for GPU
`EnvironmentMetrics`. The same "telemetry lives one link away from
identity" pattern as GPUs, just never chased down for PSUs.

**Built:** `RedfishStandaloneProvider._psu_telemetry` fetches each
supply's `Metrics` link (mirroring `_gpu_telemetry`'s per-processor
fetch) and `psus_from_supplies` takes the result as `metrics_by_supply`,
reading `_sensor_reading(metrics, "InputPowerWatts")` for `power_watts`.
One more request per PSU — the same per-server-scaling cost this
module's own docstring already accounts for. `PowerSupplyMetrics` is
only reachable through the modern `PowerSubsystem/PowerSupplies` path;
a PSU reached through the deprecated `Power` resource has no `Metrics`
link at all and stays `power_watts: None`, honestly.

**Whether a real BMC actually populates `InputPowerWatts` is
unconfirmed** — same caveat this ADR's GPU telemetry update carried
before its own live run settled it. If it turns out unpopulated too,
that is itself the answer (a hardware/firmware gap, not a mapping bug),
the same way `psu_wattage` reading `0` on UCS turned out to be
(`docs/cisco-collectors.md`).

## Update (2026-09-15, continued a third time): a PCIeDevice's `Manufacturer` decodes to a real GPU model

Operator report, on the same fleet: every PCIeDevice-sourced GPU printed
`10DE VGA` with `VRAM unknown`, all 8 GPUs on one host looking identical
and telling the operator nothing beyond "there are 8 of them." Traced:
`Manufacturer` (`"10DE20B2"`) is not a vendor name — it is the PCI-SIG
vendor ID and device ID concatenated as 8 hex digits, a firmware quirk
this schema gives no way to distinguish from a real name up front.
`10DE` is NVIDIA's registered vendor ID; `20B2` is looked up against the
**PCI ID Repository** (`pci-ids.ucw.cz`) — the community-maintained
database the Linux kernel and `lspci`/`hwdata` ship, not a Cisco-owned
or Cisco-specific list; PCI-SIG assigns vendor IDs, individual vendors
publish their own device IDs, and this repository is the de facto
aggregation point. `20B2` resolves to `GA100 [A100 SXM4 80GB]` —
confirmed against three independent sources (DeviceHunt, FreeBSD's
`pci_vendors` port, and the repository itself) — matching the operator's
own EPYC 7742 + 2048GiB fleet exactly (a DGX/HGX A100 80GB-class system).

**Built:** `_pci_ids_from_manufacturer` splits the 8-hex-digit string;
`_NVIDIA_PCI_DEVICE_MODELS` maps a handful of neighboring Ampere/Hopper
device IDs (only `20b2` confirmed live, the rest the same silicon
family's IDs on the sourcing bar ADR-0021 already sets) to a string
`GpuCatalog` already carries as an alias (`"A100-SXM4-80GB"`, `"A10"`,
`"H100-SXM5-80GB"`, ...) — never a fabricated new one. `pcie_device_to_gpu`
resolves `model` through this table before returning, so the platform's
existing ingest-time `GpuCatalog.enrich()` fills in VRAM automatically,
the same way it already does for every other vendor's GPU — no changes
to `GpuCatalog` or `gpu_models.py` at all. An unlisted device ID (AMD's
`1002`, Intel's `8086`, or an NVIDIA ID not yet in the table) falls back
to the raw `Description` exactly as before.

## Update (2026-09-16): the PCI ID table is operator-extensible, like `INVENTORY_GPU_MODELS`

The operator asked, reasonably: why is `_NVIDIA_PCI_DEVICE_MODELS` a
hardcoded table at all, when `GpuCatalog`'s own equivalent
(`INVENTORY_GPU_MODELS`) is already operator-extensible? No good reason
— the same override relationship applies here.

**Built:** `INVENTORY_REDFISH_PCIE_GPU_MODELS`, parsed by
`parse_pcie_gpu_models` as `"vendor_id:device_id:Model Name"` triples,
comma-separated (`"10de:20f3:A800-SXM4-80GB,1002:74a1:Instinct MI300X"`).
Renamed the built-in table `_BUILTIN_PCI_DEVICE_MODELS` and rekeyed it
`(vendor_id, device_id) -> model` (was NVIDIA-only, keyed by device ID
alone) so an operator entry can name **any** vendor, not just NVIDIA —
directly closing the "AMD/Intel remain unresearched" gap from the
2026-09-15 update without this codebase having to research them itself.
`_pcie_gpu_models` in `factory.py` merges the two (operator wins on a
shared ID) once per collector construction and threads the result
through `RedfishStandaloneProvider` into `pcie_device_to_gpu`, mirroring
exactly how `gpu_catalog(settings.gpu_models)` already merges
`INVENTORY_GPU_MODELS` over `GpuCatalog`'s own built-in table.

## Update (2026-09-09): `uniq_system_uuid` gave up its uniqueness too

The fix above (`{"$exists": True}` → `{"$type": "string"}`) settled the
*null*-collision case. A live Cisco UCS domain then found the other half
of the same field's problem: a real, non-null collision, between two
genuinely different physical servers.

`computeBlade`/`computeRackUnit.uuid` — what `ucs_manager/mapping.py`
reads into `system_uuid` — turned out to reflect the *associated service
profile's* UUID, drawn from an admin-managed UUID Suffix Pool, not an
immutable hardware id. Two profiles cloned with a fixed UUID instead of a
pool-derived one, or two domains with overlapping pool ranges, can
legitimately hand two different real servers the same UUID — confirmed
directly in the UCS UI, not inferred. Enforcing uniqueness on it made
that vendor-side misconfiguration a platform outage: the second server's
every ingest run raised `DuplicateKeyError` on `uniq_system_uuid`,
`_ingest_one`'s recovery path found nothing to reuse (it looks up by
`vendor`+`serial`, which the two servers do not share), and the server
was permanently dropped from inventory — the same failure shape as the
null-UUID defect above, just for a live, non-null value this fix's
`{"$type": "string"}` does nothing to catch, because there was never
anything wrong with the value's *type*.

**Fix: dropped `system_uuid`'s uniqueness entirely, kept the field and
its index — renamed to plain `system_uuid`.** Correlation was never
based on it — `IngestService._find_by_vendor_serial` is the only lookup
ingestion does, keyed on `(vendor, serial_normalized)` — so `system_uuid`
had no correlation role to lose. What it keeps: real, useful,
vendor-reported data, still indexed (now purely for an
equality/aggregation query like "which documents share this UUID",
exactly what diagnosing this needed), no longer able to reject a write.
See `app.infrastructure.mongodb.indexes`'s module docstring for the full
reasoning.

**The rename does not self-migrate, on purpose.** `_create_indexes`
detects a changed specification by matching *name* — a straight rename
does not conflict with the old index at all, it creates `system_uuid`
alongside the still-present, still-unique `uniq_system_uuid` rather than
replacing it. Keeping the old name was the safe default (tried first);
the operator explicitly chose the honest name instead and accepted
owning the migration on any already-deployed database — drop
`uniq_system_uuid` by hand (`db.servers.dropIndex("uniq_system_uuid")`)
or recreate the database, either before or right after deploying this
change. A future rename of any *other* index should not assume the same
call without asking, since it trades the self-healing property every
other index here still has.

## Update (2026-09-10): a plain unreachable host no longer makes the run PARTIAL

At the operator's request, matching the same-day change to `OPENMANAGE`
(ADR-0020's dated update): `_collect_host`'s `RedfishUnreachableError`
branch now logs at ERROR (was WARNING — with fewer collector-side signals
than OpenManage gets, the log is the only one left) and writes its
`collection_errors` entry with a new, exported marker,
`UNREACHABLE_MARKER`. `tools.run_collector._is_benign_collection_error`
reads it at the exit-code decision: a run whose only `collection_errors`
are plain-unreachable hosts now exits 0, not 3. The benign hosts are
still printed in the run's own output, just not under a `PARTIAL` banner.

**Widened the same day, still 2026-09-10, after a real OME run hit auth
failures too**: a rejected BMC credential, a credential the auth guard
disabled after repeated rejections, and the run-wide auth-failure budget
being spent (`AUTH_REJECTED_MARKER`/`AUTH_CREDENTIAL_DISABLED_MARKER`/
`AUTH_BUDGET_EXHAUSTED_MARKER`, all exported from this module) are now
just as benign as a plain unreachable host — none of the four marks the
pod failed. This reverses the original reasoning in the paragraph above,
which treated auth failures as a fleet-wide signal worth failing loudly
over; in practice, on a real fleet, they show up often enough (one bad
password, one locked-out account) that PARTIAL had stopped meaning
anything unusual, exactly the operator's complaint. **What's left as
genuinely PARTIAL-worthy**: a TLS failure, a per-host time budget
exceeded, and any error `RedfishError`/`ValueError` doesn't recognize —
none of those are collector-side auth/reachability outcomes the same way,
and none has an established real-world "happens on a normal Tuesday"
rate the way the first four do.

**Deliberately does NOT extend `OPENMANAGE`'s other half — writing a
`reachable=False` server document.** OME can name a profile (service tag,
name, template) before ever touching its iDRAC, which is what makes a
stable `(vendor, serial_normalized)` correlation key possible without
contacting the dead host. A standalone target has no such source: its
inventory file carries only `host`, an optional `name`, and login —
never a serial. Writing a placeholder for a host this collector has
*never* reached would have no stable key to correlate future collections
against, and would create a new document every single run rather than
one durable record. A host that *was* previously collected does have a
real serial, but it lives only in its own already-stored document, which
this collector has no path to read — providers deliberately have no
database access; only `IngestService` does. Closing that gap means either
teaching this provider to look servers up by BMC host (a new dependency
this layer has never had) or teaching `IngestService` a second,
address-based correlation path alongside `(vendor, serial_normalized)` —
both real design changes, not implemented here pending direction.

## Update (2026-09-12): the credential circuit breaker is gone

`_AuthGuard` is removed, with both of its settings
(`INVENTORY_REDFISH_AUTH_FAILURE_THRESHOLD`, `_BUDGET`), its two Helm
values (`collectors.redfish.authFailureThreshold`, `authFailureBudget`)
and the two `collection_errors` shapes it wrote
(`AUTH_CREDENTIAL_DISABLED_MARKER`, `AUTH_BUDGET_EXHAUSTED_MARKER`). Both
collectors that share this provider — `REDFISH_STANDALONE` and
`OPENMANAGE`'s iDRAC pass — now attempt **every** host in the inventory,
however many earlier hosts rejected the same credential.

At the operator's explicit request, after the guard fired on a real
estate. It was built to bound lockout damage; what it actually did was
turn one wrong password into a run that silently skipped most of the
fleet, so the servers behind the skipped hosts went stale with nothing in
the inventory saying why. The guard's own docstring already conceded it
"bounds damage, it does not prevent lockout" — with hosts contacted
concurrently, a number of logins equal to `fleet_concurrency` are in
flight before the first rejection returns.

**The risk it covered is real and is now the operator's to carry.** The
`redfish.credential_circuit_open` hint said it plainly and it is still
true: re-running with a wrong credential locks the account on Lenovo XCC
and IP-blocks this collector from every iDRAC for an hour. What replaces
the breaker is visibility, not protection — every rejection logs
`redfish.bmc_login_failed` at ERROR with the host and the credential's
*name*, and the run summary carries `auth_failures`.

What is unchanged: a rejected login is still recorded in
`collection_errors` under `AUTH_REJECTED_MARKER`, and is still benign at
the exit-code decision, so the CronJob pod still exits 0 for it. TLS
failures and per-host budget overruns are still PARTIAL.

`OPENMANAGE` goes one step further the same day, and this is the one
behaviour change beyond "try everything": a rejected login now yields the
same `reachable=False` placeholder document a plain unreachable host
does (`..openmanage.provider._is_uncollected`). The reasoning in the
2026-09-10 entry above is unchanged for `REDFISH_STANDALONE` — a
standalone target still has no serial to correlate a placeholder against,
so it still writes none.

## Update (2026-09-13): implementation facts moved out of the code

CLAUDE.md convention 8 became a CI gate on 2026-09-10; the facts below
lived as inline comments and long docstrings in
`app.infrastructure.providers.redfish` and are recorded here so the code
can carry a one-line pointer. Provenance per item: "DSP0266" is the
Redfish specification 1.14.0 read for this ADR; "live BMC" is the
operator's own hardware (2026-08-23 run); "measured" is this codebase.

### Client (`client.py`)

- **A 403 after a successful login is `RedfishForbiddenError`, never
  `RedfishAuthError`.** A ReadOnly BMC account — which this collector
  asks operators to use — legitimately gets 403 on some vendors'
  resources. Treating those as authentication failures would have made
  a correctly configured estate read as a bad credential on every run
  (and, while the breaker existed, aborted the run). Only a 401, or a
  403 on the session-creation POST itself, is an auth failure.
- **`build_ssl_context` returning `False` is the one TLS opt-out in the
  codebase**, per host, reason-gated at inventory load, logged every
  run. ruff's `S501` does **not** flag it — the rule matches only a
  literal `verify=False` keyword argument — so once the escape hatch
  exists the linter stops helping;
  `test_verification_is_on_unless_a_host_opts_out` is the control.
- **The Sessions URI is never hardcoded.** DSP0266 §13.3.4.1: find it at
  `ServiceRoot.Links.Sessions` or `SessionService.@odata.id`, and it is
  posted exactly as advertised (the trailing-slash defect above).
- **A non-401/403 error on session creation is `RedfishProtocolError`,
  logged with its status**: some BMCs are reported to answer a bad
  password with 400, and only real hardware settles that.
- **`Members@odata.count` is deliberately ignored** when paging a
  collection. DSP0266 defines it as the total across *all* pages, so
  comparing it to one page's length is meaningless, and trusting it in
  place of following `Members@odata.nextLink` would silently truncate
  the fleet. A member that arrives already expanded is used as-is and a
  bare link is fetched — one branch makes `$expand` on and off the same
  code path.
- **Redirects are never followed** (`follow_redirects=False`) and a 3xx
  is an error: it is the other way an untrusted device retargets the
  next request, and httpx cannot know a custom `X-Auth-Token` is
  sensitive the way it knows `Authorization` is. Same reasoning as the
  `@odata.id` validation in the Decision.
- **`Connection: close` plus a zero-keepalive pool**
  (`max_connections=1, max_keepalive_connections=0`): the header is sent
  for the BMC's benefit and the pool setting is what makes our side
  actually honour it rather than merely ask.
- **Logout is shielded and `CancelledError` is re-raised.** Without
  `asyncio.shield`, a cancelled task's `await` on the logout raises
  immediately, the DELETE is never sent, and the leaked session counts
  against a BMC session cap that is often as low as 16. Any other logout
  failure is logged and swallowed so it never masks the error that caused
  teardown — but `CancelledError` must propagate: `__aexit__` runs inside
  every caller's own task, including one `provider.py` is about to cancel
  and drain, and swallowing it there would make that task appear to
  finish normally instead of stopping.
- **The response cap is 32 MiB, checked after the body is read.** httpx
  has decompressed by then, which is what makes the cap meaningful
  rather than advisory — a `Content-Length` check alone is defeated by
  compression. Redfish payloads are small and this runs on a LAN, so
  nothing is lost; an unbounded `.json()` on a wedged or hostile BMC
  takes the whole run's pod with it.
- **`--debug-http` logs method, path and status only, and excludes the
  session exchange outright** rather than redacting it: that one request
  carries the password and its response the token, and a redactor that
  must be perfect is a worse design than never formatting the value.
- **Only transport failures are retried** (3 attempts, exponential
  backoff capped at 8 s with jitter, `Retry-After` honoured up to 30 s
  on 429/503). Never a 4xx — a rejected credential retried across an
  estate is what locks accounts — and never an `SSLError`, which is a
  configuration problem, not a transient one (sushy's rule).

### Mapping (`mapping.py`)

- **`Manufacturer` is matched on a normalized prefix** (`dell`, `cisco`,
  `hpe`, `hewlett`, `hp `) rather than equality, because the strings vary
  (`"Dell Inc."`, `"Hewlett Packard Enterprise"`), and only against that
  closed set; anything else is `STANDALONE`, a correct-but-less-specific
  answer rather than a wrong one.
- **SMBIOS placeholder serials are treated as no serial at all**
  (`""`, `0123456789`, `Default string`, `To be filled by O.E.M.`, `Not
  Specified`, `None`, `N/A`, `Unknown`, `System Serial Number`). These
  reach `SerialNumber` on whitebox hardware, and since `IngestService`
  correlates on `(vendor, serial_normalized)`, letting them through would
  collapse every such machine into one document, silently, reporting
  success.
- **`_server_name` prefers `HostName` but falls through to `Name`/`Id`
  when it is absent**: `HostName` is OS-populated and goes null when the
  host is powered off, which would flip a server's name — and with it
  its parsed site and classification — every shutdown.
- **`LinkStatus` is exactly four schema values**, unlike OME's link
  status there is nothing to guess at. `NoLink` (no carrier) and
  `LinkDown` (carrier but down) are distinct in Redfish and both map to
  `DOWN`, the only distinction `LinkState` models.
- **`Status.State == "Absent"` is the empty-bay signal** — the direct
  analogue of `ucs_common.is_equipped` — and applies to drives, DIMMs,
  processors and PSUs alike. `Drive.MediaType == "SMR"` is shingled
  magnetic recording, a hard disk, and maps to HDD rather than UNKNOWN.
- **PSU state vocabulary**: `Enabled`/`StandbyOffline`/`StandbySpare`
  -> `UP`, `Disabled` -> `DISABLED`, `UnavailableOffline` -> `DOWN`;
  `Status.Health` decides first when present (`Critical` -> `DOWN`,
  `Warning` -> `UNKNOWN`, `OK` with an unlisted state -> `UP`). The
  raw `Health/State` pair rides along as `redfish_status` for the
  dry-run print, unpersisted, so a live run can settle the `Warning`
  question on evidence. `psu_health` is public because
  `..oneview.mapping.psus_from` reuses it as its fallback — a OneView
  `/powerSupplies` row is "in JSON format based on RedFish schema".
- **`memory_modules_from_dimms` reads the same `Memory` members
  `memory_bytes` already sums** — no extra request, only the health, slot
  and speed that were being thrown away. A DIMM's `health` is a
  `HealthSeverity`, the vocabulary drives use.
- **`TotalSystemMemoryGiB` is typed `number`, not `integer`**: a real
  768 GB machine has been observed reporting `715.256064`, so the
  rounding is ours to do.
- **`EthernetInterface.Id` is carried through as `ProviderNic.location`**
  rather than dropped: on iDRAC it is the FQDD, the only thing
  distinguishing one partition from another, while `Name` is the same
  generic string on every one. `..openmanage.mapping.dell_port_nics`
  rewrites it into a readable form (`docs/dell-collectors.md`, "NICs").
- **`bmc_address_raw` is composed from the host we connected to plus the
  system's own `@odata.id`**, never from the operator's raw inventory
  string, so a credential accidentally written into an address can never
  reach MongoDB. `attachments` is always `()` — see the Decision.
- **`EnvironmentMetrics.TemperatureCelsius`/`.PowerWatts` are
  `SensorExcerpt` objects with a nested `Reading`**, not bare numbers —
  the replacement for `ProcessorMetrics.TemperatureCelsius`/
  `.ConsumedPowerWatt`, deprecated since Redfish 1.2. A GPU's
  `memory_type` is the first `ProcessorMemory[].MemoryType`; HBM stacks
  are uniform, so the first is representative.
- **`is_gpu_processor` is the single filter** both `gpus_from_processors`
  and `provider.py`'s telemetry follow-up apply, so the two cannot drift.

### Provider (`provider.py`)

- **The run budget lives on `asyncio.as_completed(timeout=...)`, not on
  an `asyncio.timeout()` around the loop, and the reason is that
  `_list_servers` is a generator.** `asyncio.timeout()` captures
  `current_task()` once, at `__aenter__` — whichever task is driving the
  generator via `asend`/`athrow`. When the deadline lands while the
  generator is suspended at its `yield` (the normal case: the real
  consumer awaits a Mongo upsert per server), the cancellation is raised
  in the *consumer's* frame, and the generator's own `except
  TimeoutError` never runs. `as_completed(timeout=)` raises from `await
  finished` itself, always inside the generator's frame. That alone only
  bounds waiting on unfinished host tasks: if every host finishes quickly
  but a slow consumer keeps the generator waiting between yields,
  `as_completed` cancels its own timeout the moment the last task
  completes. The explicit `loop.time() >= deadline` check on every
  resumption is what still bounds the whole run — the most a generator
  can do without an external supervisor. Both the timeout and a
  `hosts_unfinished` count are logged before the CronJob's
  `activeDeadlineSeconds` could kill the pod with no summary.
- **Cancelled host tasks are drained, not abandoned** (`gather(...,
  return_exceptions=True)` in the `finally`), so each still runs its
  session teardown.
- **The semaphore is acquired *before* the per-host budget starts**, so
  time spent queueing for a slot is not charged to the host. Reversed,
  every host past the first few "times out" without a packet sent.
- **Hosts are shuffled each run** because completion order is not
  arrival order: without it the run budget truncates the same slow hosts
  every run, leaving them permanently stale and invisible.
- **`health_check` makes no network call.** There is no single endpoint
  whose reachability means the fleet can be collected, and probing a
  canary host would reintroduce the single point of failure this
  collector exists to remove — one host being reimaged would kill every
  other host's run. (It used to say a pre-flight in `_list_servers`
  checked the credential; that went with the breaker on 2026-09-12.)
- **The run summary is logged even when everything succeeded**: "0
  collected, 0 failed" is the signature of an empty inventory, "0
  collected, 400 failed" a credential or network fault, and without the
  line they look identical.
- **A BMC that authenticates but exposes no `ComputerSystem` is recorded
  as an error**, not skipped silently: the inventory is the operator's
  own assertion that a server is at this address, so nothing there is a
  wrong address, an enclosure manager mistaken for a node, or a
  licensing limitation.
- **`Storage.Drives` is an inline array of links, not a sub-collection**,
  so each drive is fetched by following its own `@odata.id` — the
  normative URI is served under `Chassis` on some vendors and under
  `Systems` on others, and constructing either would be wrong somewhere.
  Drives are deduplicated by `@odata.id`.
- **PSUs hang off `Chassis`, reached through `Links.Chassis`, and two
  schema generations are both live in the field**:
  `PowerSubsystem/PowerSupplies` (Redfish 2020.4+, a real collection) is
  tried first, then the deprecated `Power` resource with its inline
  `PowerSupplies` array, which is still what most fielded BMCs serve. An
  iLO or iDRAC too old for the former still answers the latter. `None`
  (not `[]`) when neither could be read, so `_carry_forward` keeps
  stored PSUs.
- **The BMC's own MAC** is read once per host through
  `Links.ManagedBy[0]`'s `EthernetInterfaces`, preferring
  `PermanentMACAddress`.
- **Every system's `Processors` and GPU telemetry are fetched before
  any system is mapped**, so a GPU-baseboard tray can be recognized and
  its metrics already in hand before deciding whether to merge it (the
  DGX/HGX update above).
