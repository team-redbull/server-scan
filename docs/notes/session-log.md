# Session log — what each unit of work shipped and surfaced

CLAUDE.md's "Where to continue right now" keeps only the single most
recent unit of work. When you finish one, move the entry that is there
down to the top of this file and write yours in its place — newest first.
`git log` and the ADR each entry names are the authoritative record; this
is the narrative a session reads to pick up where the last one stopped.

---

**2026-09-16 — a blank Redfish `SerialNumber` falls back to
`Chassis.SerialNumber`, closing the real duplicate-document bug the
Model fallback's sibling investigation found.** The operator's own live
testing found a DGX host reporting `SerialNumber: ""` (unprogrammed
SMBIOS, not a transient failure) — `serial_normalized` empty means
correlation never finds `existing`, so the collector's two runs at
investigation time each minted a fresh document (`ocp4-five-
bpod-compute-06`, two `_id`s, same BMC). Six other duplicate-name pairs
in the same report were confirmed as **not** bugs — genuinely distinct
machines sharing a name across vendors/domains, correct
`(vendor, serial_normalized)` correlation behavior.

**Researched against DMTF's schema before fixing** (the operator asked
for this explicitly): `ComputerSystem.SerialNumber` and
`Chassis.SerialNumber` are documented as two different things that can
legitimately disagree; `Chassis.Links.ComputerSystems` can name more
than one system, in which case that chassis's own serial cannot be
safely attributed to just one of them. `mapping._chassis_serial` checks
that reverse link and refuses the fallback above one system. `_dell_serial(
system) or _clean_serial(system.get("SerialNumber")) or _chassis_serial(
chassis)` — same precedence as before, one source appended.
`_chassis_fallback()` (was `_chassis_product_name()`) now fetches the
chassis for either a blank `Model` or `SerialNumber`; a record still
serial-less after all three logs `redfish.no_serial` (previously
silent). ADR-0016 has a second 2026-09-16 update ("continued") with the
full citations.

**Deliberately not done, pending an operator decision**: a serial-less
ingest correlation guard (`(source_provider, network.bmc.host)` as a
fallback key) and cleanup of the two documents this host had already
accumulated — both bigger, riskier changes than what was asked for.

Full backend gate clean, 1407 tests passing. New/renamed
`test_redfish_chassis_fallback.py` (pure `_model`/`_chassis_serial`
unit tests) plus an expanded `TestChassisFallback` in
`test_redfish_collector.py` (absent/blank/whitespace/real-value for
both fields, the multi-system collision guard, and the no-extra-request
guarantee).
**Open:** the ingest correlation guard and Mongo cleanup above; unchanged
from prior entries — whether any real BMC populates `InputPowerWatts`,
and whether `$expand` is actually honored beyond what's advertised.

---

**2026-09-16 — the server detail page shows a GPU-derived Model hint when
the chassis never reported one, then a real fix landed for the more
common case.** A DGX/HGX-class host's BMC often omits
`ComputerSystem.Model` entirely, so the detail page showed "—" even
though the same server's GPUs were already correctly enriched via
`GpuCatalog`. First shipped a UI-only fallback (`frontend/src/lib/
gpuModel.ts`'s `inferredGpuModel`, labeled "(from GPU)", `Server.model`
itself untouched — ADR-0021's second 2026-09-16 update).

**Then the operator found the real fix**, testing against the air-gapped
fleet: some of these hosts report `Model` as empty/whitespace but their
`Chassis` resource carries the true value in `ProductName` (confirmed
via `curl .../Chassis/Self | jq '.ProductName'`). Built
`mapping._model(system, chassis_product_name)`: a real, non-blank
`Model` always wins; the chassis fetch happens only when `Model` is
already unusable, via a `_chassis()` helper extracted from `_psus`/
`_pcie_gpus`'s existing duplicated `Links.Chassis` walk. This actually
fills `Server.model` — no fabrication, the chassis genuinely reports the
model on a different resource — so the frontend hint above now only
matters for a host where even `Chassis.ProductName` is blank. ADR-0016's
2026-09-16 update (a second one, after the PCI-table entry) has the
detail. Full gate clean; new `test_redfish_model_fallback.py` plus a
`TestModelFallback` class in `test_redfish_collector.py` cover the
absent/blank/whitespace/real-model cases and the no-extra-request
guarantee.

**Open:** unchanged — whether any real BMC populates `InputPowerWatts`,
and whether `$expand` is actually honored beyond what's advertised, both
need further live runs to settle.

---

**2026-09-16 — the PCIeDevice GPU-model table is now operator-extensible,
same as `INVENTORY_GPU_MODELS` already is for `GpuCatalog`.** The
operator asked, reasonably, why the PCI ID table
(`_NVIDIA_PCI_DEVICE_MODELS` at the time) was hardcoded when
`GpuCatalog`'s own equivalent is not.

**Built:** `INVENTORY_REDFISH_PCIE_GPU_MODELS` (`"vendor_id:device_id:Model
Name"`, comma-separated), parsed by `parse_pcie_gpu_models` and merged
over the built-in table by `factory._pcie_gpu_models` — mirroring
`gpu_catalog(settings.gpu_models)`'s own merge over `GpuCatalog`'s
built-in table exactly. Renamed the built-in table
`_BUILTIN_PCI_DEVICE_MODELS` and rekeyed it `(vendor_id, device_id) ->
model` (was NVIDIA-only, keyed by device ID alone) so an operator entry
can name **any** vendor — directly closing the "AMD/Intel unresearched"
gap without this codebase researching them itself.

**Also expanded the built-in table** with every plausible device ID
from the original research pass: Tesla P100 12GB/16GB (already
catalog-matchable), A800 40GB/80GB, H800, and A10G. The latter three
needed new `gpu_models.py` rows/aliases — A800 40GB sourced from
NVIDIA's own datasheet, A800/H800 80GB from Lenovo's OEM product guide
(neither has a public NVIDIA page — confirmed), A10G as a new alias on
the existing A10 row per AWS's own datasheet confirming the identical
24GB. Pre-Pascal Tesla IDs (Fermi/Kepler/Maxwell) deliberately excluded
— out of scope for the DGX/HGX-class fleet this feature targets, and
`gpu_models.py`'s own sourcing standard forbids guessing VRAM for
hardware this platform has no evidence any real estate still runs.

Helm/`.env.example` wired: `INVENTORY_REDFISH_PCIE_GPU_MODELS` in both
`values.yaml` (`collectors.redfishStandalone.pcieGpuModels`) and both
CronJobs, and a real, sourced example in `.env.example` (an AMD ID, the
one class the built-in table still can't resolve on its own).

Full gate clean; `test_redfish_pci_ids.py` covers the parser, the
operator-override-wins-over-built-in case, and an end-to-end
`GpuCatalog.enrich()` proof (the real catalog, not a stub) that a
resolved model string actually enriches VRAM.

Separately shipped the same day (d25cdf4, no CLAUDE.md entry at the
time): `values.yaml`'s PCIe GPU defaults flipped to fleet-appropriate —
`pcieGpuDetection: true`, `pcieGpuMaxDevices: 250` (real hosts measured
133-214), `hostBudgetSeconds: 600` (a slow-path host measured ~6.5 min).

**Open at the time:** whether any real BMC populates `InputPowerWatts`,
and whether `$expand` is actually honored beyond what's advertised —
both needed further live runs to settle.

---

**2026-09-15/16 — PSU draw and PCIeDevice GPU model both went from
"unknown" to real data; the PCI ID table then made operator-extensible.**
Continuing the same day's GPU-baseboard-tray and PCIeDevice-fallback
work (ADR-0016's 2026-09-15 updates carry that narrative). Helm gap
closed: `INVENTORY_REDFISH_PCIE_GPU_DETECTION`/`_MAX_DEVICES`/
`_MAX_GPUS` had no chart wiring — added to `values.yaml` and both the
`REDFISH_STANDALONE` and OpenManage CronJobs (OpenManage
cross-references `redfishStandalone`'s own values, matching the
existing `caBundle` precedent).

**PSU `power_watts`** was `None` "by construction, not yet researched" —
DMTF's `PowerSupply.v1_5_1`/`PowerSupplyMetrics.v1_1_2` schemas confirm
real-time input power lives on a separate linked `PowerSupplyMetrics`
resource (`InputPowerWatts`), never on `PowerSupply` itself — the same
"telemetry one link away" pattern GPUs already used. Built
`_psu_telemetry` (mirrors `_gpu_telemetry`).

**Every PCIeDevice-sourced GPU showed the generic `"10DE VGA"`/`VRAM
unknown`.** `Manufacturer` (`"10DE20B2"`) packs the PCI-SIG vendor ID and
device ID as 8 raw hex digits. The operator asked for "an official Cisco
list" — there isn't one; PCI vendor/device IDs are PCI-SIG's registry,
unrelated to Cisco. The real authority is the **PCI ID Repository**
(pci-ids.ucw.cz). `20B2` resolves to `GA100 [A100 SXM4 80GB]`, confirmed
against three independent sources and matching the operator's own EPYC
7742 + 2048GiB fleet exactly. Built `_pci_ids_from_manufacturer` +
a built-in table mapping only to strings `GpuCatalog` already carries as
aliases, so `GpuCatalog.enrich()` fills in VRAM automatically — zero
changes to `GpuCatalog` itself.

**Then made operator-extensible**, at the operator's own suggestion —
the same override relationship `INVENTORY_GPU_MODELS` already has with
`GpuCatalog`'s own table didn't exist for this new, smaller table.
`INVENTORY_REDFISH_PCIE_GPU_MODELS` (`"vendor_id:device_id:Model
Name"`, comma-separated) is parsed by `parse_pcie_gpu_models` and merged
over the built-in table (renamed `_BUILTIN_PCI_DEVICE_MODELS`, rekeyed
`(vendor_id, device_id) -> model` so it is no longer NVIDIA-only) —
directly closing the AMD/Intel gap without this codebase researching
those vendors itself. Also expanded the built-in table itself with
every plausible device ID surfaced during the original research:
Tesla P100 12GB/16GB, A800 40GB/80GB, H800, and A10G — the latter three
needed new `gpu_models.py` rows (A800/H800, sourced from NVIDIA's own
40GB datasheet plus Lenovo's OEM product guide for the 80GB variants
neither has a public NVIDIA page for; A10G as a new alias on the
existing A10 row, per AWS's own datasheet confirming the identical
24GB). Pre-Pascal Tesla IDs (Fermi/Kepler/Maxwell) were deliberately
left out — out of scope for the DGX/HGX-class fleet this feature
targets, and gpu_models.py's own sourcing standard forbids guessing VRAM
for hardware this platform has no evidence any real estate still runs.

Full gate clean throughout; `test_redfish_pci_ids.py` covers the parser,
the operator-override-wins-over-built-in case, and an end-to-end
`GpuCatalog.enrich()` proof (not a stub) that a resolved model string
actually enriches VRAM.
**Open:** whether any real BMC populates `InputPowerWatts`, and whether
`$expand` is actually honored beyond what's advertised — both need
further live runs to settle.

---

**2026-09-15 — two real Redfish standalone defects from the operator's
air-gapped estate, both shipped.** The operator pasted a live finding
write-up (from a separate session against real hardware) for review and
re-implementation here, not a verbatim diff to trust — research caught
one real bug in it before it shipped.

**1. A GPU-baseboard tray with a non-CPU companion (an FPGA) was
rejected as a tray.** `has_only_gpu_processors` required *every*
processor to be a GPU; redefined to "at least one GPU, none a CPU" — a
non-CPU, non-GPU companion (FPGA, NVSwitch, ...) no longer disqualifies
it. Caught in review: the pasted fix used `p.get("ProcessorType", "")`
for the CPU check, silently breaking `cpu_summary`'s own established
convention that a `Processor` with no `ProcessorType` at all is a CPU —
fixed to `p.get("ProcessorType", "CPU")` before shipping, or a normal
host with unmarked CPUs and a GPU add-in card would have misclassified
as an all-GPU tray. `docs/adr/0016`'s 2026-09-15 update.

**2. A GPU reported only as a `PCIeDevice`, never `Processors`, read as
0 GPUs.** Confirmed live: `Manufacturer` carries a PCI-SIG vendor ID
concatenated with a device ID (`"10DE20B2"`, NVIDIA), `Description` is
the closest thing to a model name (`"10DE VGA"`) — real hardware,
despite `DeviceType: "Simulated"` being a genuine, confusing BMC quirk.
Built as an opt-in fallback (`INVENTORY_REDFISH_PCIE_GPU_DETECTION`,
off by default, triggered only when `Processors` reports no GPU):
`is_gpu_pcie_device` requires a known vendor ID *and* a display-shaped
`Description` (NVIDIA/10DE confirmed live; AMD/Intel included but
unresearched). Before implementing the pasted design's brute-force
per-device scan (measured ~6.5 min for 214 devices), researched DSP0266
directly: `$expand=.($levels=1)` is combinable with `$select` and
collapses a whole collection into a handful of requests when advertised
via `ProtocolFeaturesSupported.ExpandQuery`. The operator checked their
fleet live and confirmed most BMCs (Redfish 1.7) advertise
`ExpandQuery.NoLinks: true`. Built `_paged_members` to try `$expand`
first and transparently degrade to the original per-device `$select`
scan when a BMC doesn't honor it.

**The "is `$expand` actually honored" question got answered live, and
the answer was no, for a case this design hadn't covered.** The
operator tested a DGX H100: its `Systems/DGX.PCIeDevices` is a direct
link array on the `ComputerSystem` itself — a second real shape,
distinct from `Chassis.PCIeDevices`'s actual collection resource — and
`$expand=PCIeDevices` (the named-property form) silently returned 0 of
133 real entries. Fixed: `pcie_device_refs` reads the array shape
directly (no collection exists to `$expand`); both shapes are now
checked and merged; `_paged_members` detects the DGX's exact failure
signature (`Members@odata.count > 0` with zero `Members` returned) and
retries without `$expand`.

**Two more fixes from the same fleet, same day:** the Helm chart never
got the three new `INVENTORY_REDFISH_PCIE_GPU_*` settings wired in —
added to `values.yaml` and both the REDFISH_STANDALONE and OpenManage
CronJobs (the latter cross-references `redfishStandalone`'s own values,
matching the existing `caBundle` precedent, rather than a second set of
knobs). Then the operator reported every PSU showing `draw unknown` and
every PCIeDevice-GPU showing the generic `10DE VGA`/`VRAM unknown` —
both researched and fixed: PSU real-time draw lives on a separate linked
`PowerSupplyMetrics.InputPowerWatts`, the same "telemetry one link away"
pattern GPUs already used (`_psu_telemetry` mirrors `_gpu_telemetry`);
a PCIeDevice's `Manufacturer` decodes to a real PCI vendor+device ID
pair (`10DE20B2` → NVIDIA, device `20B2`), confirmed against the PCI ID
Repository (pci-ids.ucw.cz — not a Cisco list) as `GA100 [A100 SXM4
80GB]`, matching the catalog's own `"A100-SXM4-80GB"` alias exactly —
`_NVIDIA_PCI_DEVICE_MODELS` resolves it so `GpuCatalog.enrich()` fills
in VRAM automatically at ingest, no changes to `GpuCatalog` itself.

Full gate clean throughout; new `test_redfish_gpu_baseboard.py`,
`test_redfish_pci_ids.py`, an expanded `TestPcieDeviceGpuFallback`, and
a new `TestPsuTelemetry`/`TestPowerWatts`.
**Open:** AMD/Intel PCIeDevice GPU vendor IDs remain unresearched — only
NVIDIA/10DE is confirmed live. Whether a real BMC populates
`PowerSupplyMetrics.InputPowerWatts` at all is unconfirmed — same
caveat GPU telemetry carried before its own live run settled it.

---

**2026-09-14, later — both UCS Central findings shipped, plus a live
data-quality bug found and fixed along the way.** Continuing the same
day's PSU/vNIC investigation: the operator ran `verify_ucs_central.py`
live and confirmed both open questions. Section 9 (query one domain's
own UCS Manager directly, bypassing Central) got 42 `equipmentPsu` / 2
`equipmentRackUnitPsuStats` back — settling that Central's own API never
proxies statistics classes at all, independent of any domain policy.
**Shipped:** `Psu.power_watts` (real-time input power, alongside — never
replacing — the existing rated `capacity_watts`), read through
`UcsManagerProvider`'s new 14th domain-wide query and DN-joined onto its
owning PSU in `_psus`; Intersight/OneView/Redfish set it `None` with a
one-line pointer, matching `Gpu.power_watts`'s existing per-vendor
pattern; the fake provider populates it ~5% of the time for UCS_CENTRAL
only, echoing the confirmed 2-of-42 live ratio. **Also shipped:** vNIC
`link_state` (both `_nics` and a `VNIC`-kind `_attachments` call) now
reads `operability` instead of `oper_state` via a new
`_vnic_link_state` — this reopens the exact false-CRITICAL case
ADR-0027 fixed on 2026-09-12, but safely: that fix's
`network.links_known_count` gate stays, so a mostly-`operable` fleet
just now has a real non-zero denominator instead of a permanent zero
one. ADR-0027 and `docs/architecture.md` both got dated updates rather
than silent rewrites.

**Caught mid-session, before it shipped:** the fake generator's first
`power_watts` draft used `rng.random()` conditionally on
`collector is UCS_CENTRAL` inside `_build_psus` — exactly the landmine
ADR-0027's own "Seeded data" section already named for `_link_states`
(a vendor-conditional draw on the one shared `rng` stream shifts every
later server's fields for the same seed, corrupting the whole fleet mix).
Caught by actually re-seeding and measuring rather than trusting the
diff — fixed by keying the decision off `index` via `zlib.crc32`
instead, matching `_link_states`'s own pattern; re-verified the site
distribution (211/224/224/220/121) came back byte-identical after the
fix. **The user also asked to make disk `na`/`unknown`, physical
`indeterminate`, and disk `offline`/`self-test-failed` all "known"** —
declined for the first three (ADR-0009/0027's deliberately-unmapped
no-verdict states, pinned by tests; forcing a severity would be exactly
the fabrication `None`-means-unread exists to prevent) and explained why;
the fourth turned out to already be correctly mapped to CRITICAL in
production — only `verify_ucs_central.py`'s own separate hand-copied
`_DISK_HEALTH_MAP` mirror had drifted and was reporting false gaps, fixed
by importing the real `_disk_health` directly so it cannot drift again
(ADR-0009 updated).

**README re-measured live**, not estimated: same seed/count, network
policy counts moved from 22 CRITICAL/17 MAJOR to 25/20 (+3/+3, all from
UCS Central; Intersight still 0/0, unchanged and still exempt — no
equivalent field researched for it). Full gate (backend, frontend, helm
lint, 1337 tests with the dev stack up) clean throughout.

---

**2026-09-14 — two UI reports from the operator's own UCS Central fleet
turned into two previewed-not-wired findings, plus a chart timeout gap
closed.** The operator reported PSU wattage showing `0W` in the UI
despite the PSU's own model naming a wattage (e.g. `UCSC-PSU1-770W`), and
vNIC state showing `UNKNOWN` almost everywhere despite UCS Manager's GUI
showing "Operability: Operable" per vNIC. Neither was fixed blind.
Re-checking the installed `ucsmsdk` source (not assuming last time's
research still holds, per convention 1) found: `Psu.capacity_watts`
already reads `equipmentPsu.psu_wattage` correctly — the GUI's real
number lives on a wholly separate child MO,
`equipmentRackUnitPsuStats.input_power`, fed by the stats poller; and
`AdaptorHostEthIf` (vNICs) has a **second** property, `operability`,
distinct from the `oper_state` ADR-0009 already found mostly-UNKNOWN and
concluded (wrongly, it now looks like) had "no better signal" — same
enum, present since UCS Manager 1.0(1e), and it is `operability` the GUI
actually labels "Operability". Following the exact precedent ADR-0009
set for `fabric_name` ("preview it live before wiring anything in"),
`tools/verify_ucs_central.py` gained sections 7 and 8 to print both
fields against the operator's real domains before any domain-model or
mapping change — both are documented in `docs/cisco-collectors.md` as
open, unconfirmed-live findings, not yet fixes. **Separately fixed:**
`collectors.ucsCentral` had no connect-timeout override in the Helm
chart even though `INVENTORY_COLLECTOR_CONNECT_TIMEOUT_SECONDS` already
governs its UCS Manager logins per-domain (Intersight already exposed
its own); added `collectors.ucsCentral.timeoutSeconds` (240s).
**The operator ran sections 7-8 live** (5586 PSUs / 12583 vNICs, real
fleet): `psu_wattage` reads `0` on 4770 of 5582 equipped PSUs (real,
non-zero on the rest — the field is not universally broken) while
`equipmentRackUnitPsuStats` returned **zero** MOs through Central for any
of them; `operability` reads `operable` on **100%** of vNICs against
`oper_state`'s 99.75% UNKNOWN (12551/12583), confirming it is a real,
populated signal. **A materially bigger finding surfaced checking what
consumes it**: `health_policy_defaults.py`'s `network.all_links_down`
(CRITICAL) and `network.single_link_up` (MAJOR) both gate on
`links_known_count`, and `_nics` returns vNICs whenever a server has
any (host-preferred over physical, `docs/cisco-collectors.md` "Which MAC
the OS actually sees") — so for essentially every *associated* UCS
server, `links_known_count` is silently ~0 today and **neither policy
has ever been able to fire for the UCS fleet**, not a display-only bug.
Added **section 9** (queries one domain's own UCS Manager directly,
bypassing Central) to settle whether Central itself is what blocks
`input_power` — not yet run. Checked `_OPER_STATE_MAP`: an unrecognized
`operability` value falls to UNKNOWN, never a false DOWN, so switching
`_nics`' vNIC `link_state` to it cannot manufacture a false CRITICAL from
an exotic transient state (`config`/`discovery`/...) — the one thing
still genuinely unconfirmed is whether UCS Manager ever actually reports
a non-`operable` value for a vNIC with a real problem, since this fleet
has zero negative examples so far.

---

**Before that, 2026-09-14 — the inventory page moved into the browser (ADR-0033), and
the delivery path got its compression.** The operator corrected the
scale to **2,500 today, 5,000 at most in one to two years** (10k/50k are
test headroom), and asked for a first-principles answer to "why not
filter in the UI?". The research and measurements are in the ADR; the
short version: the fleet as flat rows is 187 KB gzipped, every browser
operation is under a frame, and the API's own list path would cost 618 ms
per fleet-sized request because it validates `Server` per document — so
a new `GET /servers/rows` reads a Mongo projection into a flat
`ServerRow`, is cached as wire bytes under ADR-0028's invalidation, and
carries a weak ETag (body byte-stable: `generated_at` is the newest
`updated_at`, not the build time — the first version got that wrong and
the idle benchmark window caught it). The frontend polls it every 30 s
and does filter/search/sort/facets/paging in `features/inventory/rows.ts`;
`cursor` became `page`; search is substring; sort is natural. Before/after
was measured with a Playwright harness on the same seeded fleet (medians
in the ADR): filter clicks 66–102 ms → 15–30 ms event-to-DOM, 11 API
requests per session → 2, wall-display idle now refreshes on 304s.
Shipped alongside: the API gzips responses over 1 KB (level 6, measured),
nginx gzips the bundle (494 → 143 KB) and serves `index.html` as
`no-cache`. The same harness at 10k and 50k is in the ADR (fine at 10k,
wrong at 50k — 2.4 s first load; the operator's ceiling is 5k). A
pre-commit verification pass (API hammer + UI walk, both in the ADR)
caught a search-parity gap and a reflowing filter row, both fixed. Then
at the operator's request: `GET /servers/facets` deleted (nothing called
it; `feat!:`), every dropdown option counts — `(0)` included, sites too —
the State column sorts by severity, and a pre-existing bug where two
filter changes under ~100 ms apart lost the first was fixed by building
the next URL from the live location.

---

**Before that, 2026-09-13, evening — the stale filter, `INFO` retired, and a layering fix.**
The inventory gained `?stale=true` (a `$$NOW`-based `$expr` so the cursor
binding stays constant — `.claude/rules/mongodb.md`), a `stale` flag on
every server response, a `stale` facet, a `Stale 20h` chip in the State
column and a relative `Last seen` on the detail page (ADR-0029 update).
`HealthSeverity.INFO` is gone: no shipped policy ever produced it; a
stored `INFO` decodes as `HEALTHY`. And the morning's commit had made
`app.api` import `tools.run_collector` — the CLI layer above it — which
worked in the container and CI only because uvicorn's default `--app-dir .`
puts the repo root on `sys.path`, and broke the README's documented
`--app-dir backend` command; provider construction now lives in
`app.infrastructure.providers.factory` and both callers import it from
there. Verified in a real browser (Playwright against the seeded dev
stack): a `Seen` column was built, measured to push Maintenance off a
1440px viewport, and removed. Earlier the same day:

**2026-09-13 — `GET /api/v1/servers/available`** (ADR-0032). A read API for
`BareMetalHostUCS`'s BMH-creation flow to call instead of querying HP
OneView / Cisco UCS Central / Dell OME / Cisco Intersight live itself:
`?name=` for one exact server; `?pattern=` (a real MongoDB regex,
capacity-token-aliased — `5tb` also matches a bare `hypershift` server,
`10tb` a `hypershift-data` one) for a health-tiered, randomly drawn,
`?count=`-bounded set; `?vendor=`/`?source_provider=` to narrow either.
Each item is a purpose-built `AvailableServerItem` carrying only what
`bmh-generator-operator` consumes (second commit, same day, after reading
its generators). Candidates come from Mongo; only the few being returned are live-verified,
via a new sixth abstract method `get_one(ServerIdentity)` on
`ServerInventoryProvider` (implemented in all seven providers) and a new
`IngestService.ingest_one`. The API pod now mounts the
collector-credentials Secret for this; an unconfigured vendor degrades to
trusting Mongo. Shipped with it: `INVENTORY_MAX_AVAILABLE_COUNT` and
`INVENTORY_CAPACITY_ALIASES` (Helm `config.maxAvailableCount`/
`.capacityAliases`), a `flake8-bugbear` allow for FastAPI `Query`/`Depends`
defaults, and this CLAUDE.md restructure — three path-scoped rules under
`.claude/rules/`, the history moved to `docs/notes/`. **Open:** Intersight's
`get_one()` owner-relation `$filter`s have never run against a live tenant
— the next `verify_intersight` pass should exercise one.

**Before that, 2026-09-13, late — the comment sweep.** Eight parallel
agents cleared every one of the 701 comment-density violations and then
deleted every short comment that merely restated the code: 193 files,
−3,600 lines net, the baseline now empty (convention 8). Every displaced
fact went to its topical doc — `docs/cisco-collectors.md`,
`docs/dell-collectors.md` (new "NICs" section), `docs/hpe-collectors.md`,
ADR-0016's dated update (Redfish implementation facts), ADR-0007/0012/0026
updates, `docs/architecture.md` (the fake provider's shape, the provider
contract, the default-policy table, how `run_collector.py` is put
together, the `default_system_rules` ordering history), `deploy/README.md`
and `.env.example`. Six stale statements were corrected on the way. Two
things it surfaced: `Manager` carried five never-written, never-read
fields (`site_id`, `parent_manager_id`, `bmc_credential_ref`, `metadata`,
`ALLOWED_PARENT_TYPES`) plus an index on one of them — removed the same
night, index retired via `RETIRED_INDEXES`, old documents load unchanged
(`tests/integration/test_manager_repository.py`); and keyset paging
on `updated_at`/`last_seen_at` returned an empty second page (a real
`datetime` in `$gt` against ISO strings — ADR-0006's trap), confirmed
live and fixed in the commit after the sweep.

**Before that, 2026-09-13, night — releases deploy themselves** (ADR-0031).
CI gained a `deploy` job after `publish`: it checks out redbull-platform
with `REDBULL_WRITE_TOKEN`, `rsync`s the chart's templates/files into
`gitops/charts/server-scan`, `yq`s the two image tags and `appVersion`,
renders offline, and pushes one `chore(server-scan): pin images to X`
commit with a rebase-retry. The gitops `values.yaml` is never replaced.
Also that evening: the GPU category was never rolled into overall health
(a DOWN GPU read HEALTHY) — fixed in v1.1.3, see "Key technical facts".

**Before that, 2026-09-13, evening — v1.1.0** — health policies are scoped
to a *set* of collectors and the Rules & Policies page groups them by
scope (ADR-0030). `PolicyScope.manager_types` (list, empty = everyone)
replaces `manager_type`; the two UCS fabric-path defaults are scoped to
`vendor=cisco, manager_types=[UCS_CENTRAL, INTERSIGHT]`, everything else
is general; the page shows "General" first, then "Cisco — UCS Central,
Intersight", each sorted CRITICAL → MAJOR → WARNING. The real fix
underneath: **a manager-scoped policy had never matched any server** —
every evaluation call site passed `manager_type=None` — and
`Server.source_provider` is now threaded through as that value. See the
"Key technical facts" entry. Same evening: `/gate` lost its
`disable-model-invocation` flag so a session can run it itself, which
was the point of it.

**Before that, 2026-09-13, later** — **the repository is
`team-redbull/server-scan`**, renamed from `server_scan`. Every `v*` tag
and GitHub Release up to v17.4.3 was deleted at the operator's direction
and versioning restarted: the first release under the new name is
**v1.0.0**, and the images are `ghcr.io/team-redbull/server-scan-api` /
`-frontend` (the old `server_scan-*` packages are deleted from GHCR).
ADR-0010's dated update records it. Two things came out of the same
afternoon: the publish job is now re-runnable after a failed step — a
GitHub API outage left a tag with no release and no images, and the
"version moves forward" guard then refused the re-run until the tag was
deleted by hand — and redbull-platform was pinned to `1.0.0` and
deployed (Synced/Healthy). The operator also stated the real estate:
**~5,000 servers today, up to 10,000** — the scale statements in this
file, `README.md`, `docs/architecture.md` and `docs/arc42.md` now say
that, with the 50k verification kept as the measured headroom.

**Before that, 2026-09-13** — the Claude Code setup itself: `/gate`,
`/docs-sweep`, three enforcing hooks and two review agents, all under
`.claude/` (tracked, per convention 4); MCP servers stay user-level. See convention 7 for what each does. Nothing in the
platform changed.

**Before that, 2026-09-12, later the same day** — staleness
detection (ADR-0029), item 0 of the not-done list: fleet gauges on
`/metrics`, a `ServiceMonitor` + `PrometheusRule` in the chart, and the
frontend's nginx collapsed to three `location` blocks. Also the same day:
maintenance is switched only from the inventory list now (the detail page
is read-only for it), the Name column is left-aligned with everything
else centred, and the Helm chart's fake collector seeds 2,500 servers to
match the operator's real estate.

**Earlier the same day** — eight operator-requested changes, all
shipped. The deployment ones first:

- **The Helm chart is `deploy/helm/server-scan`**, renamed from
  `server-inventory`, along with `app.kubernetes.io/part-of`, the
  `serverScan.*` template helpers, the project name in `pyproject.toml`,
  `INVENTORY_SERVICE_NAME`, `scripts/dev-up.sh`'s pod name, and **the
  Mongo database and user, both now `server-scan`**. The database rename
  was made at the operator's explicit direction after the orphaning risk
  was raised: **an existing deployment's data stays in the old
  `server_inventory` database and must be moved by hand** — `mongodump
  --db server_inventory` then `mongorestore --nsFrom 'server_inventory.*'
  --nsTo 'server-scan.*'`. A hyphen in a Mongo database name is legal and
  was verified against a real server, not assumed; only the `mongosh`
  shell needs `db.getSiblingDB("server-scan")` rather than dotted access,
  since `db.server-scan` parses as subtraction.
- **Exactly one Route, and it does not gain one per endpoint.** The Route
  only gets traffic into the cluster; the frontend's nginx decides which
  paths belong to the API and forwards them to its Service
  (`frontend-api-proxy-configmap.yaml`, mounted at
  `/etc/nginx/api-proxy.d`, which `frontend/nginx.conf` includes). So
  `route.apiPaths` and the five path-scoped API Routes are gone, and
  **exposing another API path is one `location` block there** — no second
  Route, no second hostname. Currently forwarded: `/api/`, `/health/`,
  `/metrics`, `/docs`, `/redoc`, `/openapi.json`. It is a list rather
  than a catch-all because the SPA owns `/` and has its own client-side
  routes (`/servers`, `/rules`, `/health-policies`) that must not be
  proxied.
- **The standalone Redfish TOMLs live in the chart**, at
  `deploy/helm/server-scan/files/redfish/`, read with `.Files.Get`
  (`collectors.redfishStandalone.inventoryFile`/`credentialsFile`). The
  inventory file is the default source; **the credentials file is opt-in
  and empty by default because rendering it puts BMC passwords in git**,
  and `credentialsSecret` still wins.
- **`helm template` passing proves nothing about a missing value.** Helm
  renders an absent `.Values.x` as an empty string with no warning, so a
  mis-nested values file (a block inserted between a map's `enabled` and
  its other keys — done twice on 2026-09-13, in this repo's own
  `values.yaml` and again in redbull-platform's copy) rendered
  `expr: server_scan:collector_silent_seconds >` and sailed through
  `helm lint`, `helm template` and `promtool`. Only OpenShift's
  `prometheusrules.openshift.io` admission webhook rejected it, at Argo
  sync time. Two things now stand in the way: every threshold the
  PrometheusRule reads is wrapped in `required`, so the render fails
  with a message naming the key; and **before pushing a chart change,
  run it against the real cluster** — `helm template ... | oc apply
  --dry-run=server -f -` exercises every admission webhook, which nothing
  offline can.
- **CI has a `helm` job** that lints every chart under `deploy/helm`
  (discovered, not listed) and `helm template`s each one under the value
  combinations the defaults never reach. It uses the runner's
  preinstalled helm rather than `azure/setup-helm`, so ADR-0013's
  SHA-pinning obligation gains nothing new to maintain.

And the four application ones: the Unassigned site card is hidden while empty (a configured
site still shows at zero); the Redfish credential circuit breaker is
deleted so every BMC is attempted every run, with `OPENMANAGE` now
writing a `reachable=False` placeholder for a rejected login as well as a
dead one; the inventory table gained a one-click per-row maintenance
switch; and UNKNOWN stopped counting as a health verdict (ADR-0027 —
this was a real fleet-wide false CRITICAL on Cisco, not a hypothetical).
See the "Key technical facts" entries for the last two.

**Work before that, 2026-09-10** — cluster membership, finished and
documented. `Server.openshift` is now written by two real CronJobs
(`docs/adr/0024-openshift-cluster-membership.md`), `OpenShiftLifecycle`
was trimmed from ten fields to five at the user's direction,
`OpenShiftState` narrowed to AVAILABLE / INSTALLED /
INSTALLED_TO_INVENTORY, and the kustomize tree at `cronjobs/` was replaced
by a Helm chart at `deploy/helm/openshift-membership` (one release per
cluster, for ArgoCD). Three UI changes landed with it: search now finds
mid-name fragments (`docs/adr/0025-...`), the sites landing page gained
Available/Installed cards, and `?site_id=unassigned` works — it never had,
so the site overview's own Unassigned card had always linked to an empty
list.

That commit also fixed the five CI failures the previous one shipped red,
plus a real runtime bug `ty` caught only because CI never got that far:
`AuditService(...)` called positionally against a keyword-only parameter,
which would have raised `TypeError` on every real collector run.

**Convention 8 is now a CI gate**, not the honor system — see the
convention itself. `scripts/check_comment_density.py` with a baseline of
701 pre-existing violations that may only shrink.

The most recent user direction before that was: real vendor collectors
first, deployment/CD gaps and auth deliberately parked. **Every planned vendor
collector now exists, and as of 2026-09-08 every one of them has had a
live field pass against real hardware, every one finding at least one
real defect** — `UCS_MANAGER`/`UCS_CENTRAL` against UCSPE, `ONEVIEW`
against a live appliance (ADR-0022's "Results, 2026-09-07"), `INTERSIGHT`
against the user's on-prem Private Virtual Appliance, both
`verify_intersight` and `--dry-run` itself (ADR-0017's "second field pass"
section — auth, name resolution, `TotalMemory`'s MiB unit, and five real
defects found and fixed: the `ComputeBoard`-only join gap, a GPU catalog
matcher that couldn't recognize Intersight's own product-name spelling,
and `"OK"` reading UNKNOWN instead of UP/HEALTHY across PSU health,
GPU/NIC `oper_state`, and drive health), and, last to close, `OPENMANAGE`
against a live OME appliance and several iDRAC9 servers on 2026-09-08
(ADR-0020's own flagged "highest-consequence unverified assumption" —
that iDRAC's `SerialNumber` is the Service Tag OME correlates on — turned
out wrong; the real one is `Oem.Dell.DellSystem.NodeID`, fixed the same
day). The natural next steps:

1. **UCS's own leftovers — settled 2026-09-07 by a live UCS Central dry
   run**, see ADR-0009's two "Update (2026-09-07)" sections. **Settled:**
   `total_memory`'s MB assumption is correct (confirmed against the UCS
   UI's own figure, and this also backs Intersight's identical
   assumption); `cpu_model` and per-drive storage detail are confirmed
   populated on real hardware, not just present in the mapping code;
   fabric `fabric_model`/`fabric_serial` are confirmed populated too
   (this was already implemented, just never recorded in the ADR until
   now); and the `health`/`oper=UNKNOWN` question is fully settled —
   `_DISK_HEALTH_MAP` was missing two real failure states (`offline`,
   `self-test-failed`, both now CRITICAL) and `_OPER_STATE_MAP` was
   missing five real `AdaptorExtEthIf` values, both closed against the
   installed `ucsmsdk`'s authoritative enums rather than only what this
   fleet happened to show. Three more raw values were confirmed to be
   correct as UNKNOWN, not gaps: disk `NA`/`unknown` genuinely mean
   "doesn't apply"/"no verdict" in Cisco's own terms, and interface
   `indeterminate` (24% of this fleet's physical ports — common) is
   Cisco's own name for "cannot be determined". A fourth finding wasn't a
   bug at all: `AdaptorHostEthIf.oper_state` (vNICs) turned out to be a
   generic equipment-operability enum, not a link-state one, so reading
   `"unknown"` on 99.75% of vNICs is expected given what the field
   actually measures — no fix exists to make there. **`fabric_name` is
   now built and confirmed live** — `topSystem.name` (the domain's shared
   cluster name; UCS Manager has no per-FI hostname), previewed first in
   `verify_ucs_central`'s section 6, then independently confirmed by the
   user running a short `ucsmsdk` script directly against a real
   air-gapped domain before it was wired in. One more domain-singleton
   query per domain (`ucs_manager/provider.py`), threaded through
   `_attachments`'s new `cluster_name` param. See ADR-0009's second
   "Update (2026-09-07)". **Still open:** `fabric_id` (no source exists)
   and a fully *associated* service profile (nothing tested has gone past
   `config-failure` for want of a boot policy, vNICs and a UUID pool).

2. **OpenManage's own remaining narrow item**: `Oem.Dell.DellSystem.
   NodeID` is confirmed only on iDRAC9. Worth a quick check on any iDRAC7/8
   hardware this estate still runs — those generations may not carry the
   OEM block the same way, and `mapping._dell_serial` falling through to
   `SerialNumber` there would silently reintroduce the wrong serial for
   that generation only.

3. **The Dell iDRAC GPU VRAM check** (`docs/field-test-checklist.md`
   part 3) — one `curl`, opportunistic, only if a Dell server with a GPU
   fitted is ever to hand. Settles whether the built-in GPU catalog needs
   to carry Dell's own spellings or Redfish's `MemorySummary` already
   covers it.

4. **Intersight's own remaining narrow items**, none blocking: the
   DOWN/CRITICAL counterpart to Intersight's `"OK"` vocabulary
   (`normalize_oper_state` and `_drive_health` both), unconfirmed because
   nothing on the tested tenant has actually failed; boot-optimized
   storage (`FlexUtil`/`FlexFlash`), confirmed real but not implemented;
   and the smaller ADR-0017 UNVERIFIED-list items (CPU-name field, BMC
   address precedence, clock skew, account region). Worth another
   `verify_intersight` pass opportunistically, not a scheduled action.

5. **Then the deployment/CD and auth gaps above**, which are the rest of
   what "production and really run" means for this platform — staleness
   detection first, since it is item 0 of the not-done list and nothing
   else answers "40 hosts have been failing for two weeks". Ask the user
   before assuming this is the next phase; the ordering above is the
   direction they have been steering toward, not a plan they have signed
   off on.

~~Give the Dell collector a seeded shape~~ — **already done, kept here as
a standing caution rather than deleted.** `_UNSEEDED_COLLECTORS` is
empty, `COLLECTOR_TYPES` shapes all five collectors including
`OPENMANAGE`, and `provider_type_for` discriminates Dell by
`server.vendor` rather than by `external_id` prefix. Verified 2026-09-07
(Phase 11 of `docs/notes/2026-09-refactor-plan.md`) — this exact item was
stale once before a session caught it, so double-check before trusting it
a third time.
