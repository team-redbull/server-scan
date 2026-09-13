# What to run against your vendor managers, and what to bring back

Written 2026-08-29 for the first real run of the Intersight collector,
extended 2026-09-04 for OneView and 2026-09-05 with a one-request Dell
GPU check. Everything here is read-only: no MongoDB write, no ingest, no
`POST` other than the login a probe needs to make and then deletes.
Nothing you run can change anything in Intersight, in OneView, in iDRAC,
or in the inventory database.

| # | Target | Probe | The one answer that matters most |
|---|---|---|---|
| 1 | Intersight | `uv run python -m tools.verify_intersight` | Is `TotalMemory` MiB? If not, every server's memory is 4.86% high, silently. |
| 2 | HPE OneView | `uv run python -m tools.verify_oneview` | **Done 2026-09-07 — CONFIRMED.** See ADR-0022, "Results, 2026-09-07". |
| 3 | A Dell iDRAC with a GPU | one `curl` (part 3) | Does iDRAC populate `TotalMemorySizeMiB` for an add-in GPU? Decides whether the built-in GPU catalog carries Dell or Redfish does. |

Part 1 is the one whole collector that has never seen live hardware.
Part 2 (OneView) is done — kept below for the next appliance this ever
needs running against, and because it's what the ADR's write-up points
back to. Part 3 is much smaller — a single request settling one open
question — so do it opportunistically if a Dell with a GPU is to hand.

---

# Part 1 — Intersight

## The short version

Yes — `verify_intersight` is the one command. Four lines:

```bash
cd /path/to/server_scan

export INVENTORY_INTERSIGHT_IP=<your intersight hostname>   # bare host, no https://
export INVENTORY_INTERSIGHT_API_KEY_ID='<the API Key ID>'
export INVENTORY_INTERSIGHT_API_KEY_PEM="$(cat ~/intersight-key.pem)"

uv run python -m tools.verify_intersight --show-names 15 | tee intersight-verify.txt
```

Send back `intersight-verify.txt`. That is the whole errand, and it is
safe to run repeatedly.

**Output section 7, "OperState VOCABULARY", added 2026-09-07 — the `"ok"`
half is DONE, the DOWN/DISABLED half is still open.** A live run against
this tenant found `equipment.Psu.OperState` split between `"OK"` (36
PSUs) and `"Operable"` (2), a spelling `normalize_oper_state` had no
entry for; `"ok"` is now in `ucs_common._OPER_STATE_MAP`. See ADR-0017,
"A second field pass (2026-09-07)" for the full section 7 output and what
it did and didn't settle. **Still open:** no PSU, GPU or NIC on that
tenant was actually down, so the DOWN/DISABLED spellings Intersight's own
API uses are unconfirmed — the map's UCS-XML failure strings
(`"inoperable"`, `"failed"`, ...) are carried forward as a guess, not a
verified fact. A rerun is only worth another look if this estate ever has
a genuinely failed PSU/GPU/NIC to check section 7 against.

**Output section 8, "DISK HEALTH VOCABULARY", added 2026-09-07 — DONE,
same day.** Same root cause as section 7's PSU finding, one field over:
216 of this tenant's 226 sampled drives reported `Health: "OK"`, the same
generic healthy string Intersight uses for PSUs, which `_drive_health`
had no entry for — every one of those 216 read `health=UNKNOWN` in a
`--dry-run` instead of HEALTHY. `"ok"` is now in `_drive_health`'s
healthy set. See ADR-0017, "The same 'OK' gap, one field over". **Still
open, same shape as the PSU question:** no drive on this tenant reported
anything but a healthy state, so the WARNING/CRITICAL spellings
Intersight's own API uses for a degraded or failed drive are unconfirmed
— worth another look only if this estate ever has one to compare
against.

If the probe passes and you want to see the actual server records it
would ingest — still writing nothing — add:

```bash
uv run python -m tools.run_collector --manager-type INTERSIGHT \
  --dry-run --limit 3 | tee intersight-dryrun.txt
```

---

## Getting the API key (it is not a username and password)

Intersight has no password login for its API at all. The credential is an
**API Key ID** plus that key's **PEM private half**.

1. In the Intersight UI: **Settings → API Keys → Generate API Key**.
2. Use an account with the **Read-Only** role — this collector only reads.
3. **Save the private key when it is shown. It is shown exactly once.**
4. Copy the **API Key ID**: a long `/`-joined string, not a username.

Either key generation works. v2 keys are RSA (`BEGIN RSA PRIVATE KEY`),
v3 are EC (`BEGIN EC PRIVATE KEY`); the collector reads the PEM header
and picks the signing algorithm itself, so there is nothing to configure.
The key must be **unencrypted** — a passphrase-protected PEM is
deliberately not supported and will say so.

`INVENTORY_INTERSIGHT_IP` is a bare hostname: no `https://`, no port, no
path. The collector builds the URL itself and rejects anything else up
front, because the `Host` it signs has to match the one it sends.

**TLS certificate verification is unconditionally disabled** for this
collector — a deliberate, explicit user decision (2026-08-31). There is
no `INVENTORY_INTERSIGHT_CA_BUNDLE` or verify flag to set; the signed
request and its response go to whatever answers at
`INVENTORY_INTERSIGHT_IP`, in every environment including a production
tenant. See `app.infrastructure.providers.intersight.client.
IntersightClient`.

---

## The three things I actually need from the output

### a. The `TotalMemory` unit — the single most important line

Output section **"4. THE TotalMemory UNIT"**.

This is the highest-risk unknown in the collector. Cisco documents no
unit for `TotalMemory` anywhere — not on the summary, not on `Blade`, not
on `RackUnit`. The collector assumes MiB. **If that is wrong, every
server's memory is reported 4.86% too high, silently, forever**, and
nothing else in the platform would ever notice.

The probe settles it by summing one real server's DIMMs, whose capacity
*is* documented, and comparing. You will get one of:

- `SETTLED:` — the assumption is right. Nothing to do.
- `MISMATCH — ... MB-vs-MiB ratio` — the assumption is wrong; I change
  one constant before this is ever scheduled.
- `could not read memory/Arrays: ...` — the relationship filter isn't
  supported on your version. Then please compare by hand in the UI: one
  server's total memory against the sum of its DIMMs.

### b. Do server names come out right

Output section **"3. THE SERVER NAME"**.

The platform parses a server's **site** out of its name (`ocp4-prod-tlv-…`
→ `tlv`) and only collects servers matching `INVENTORY_COLLECTOR_NAME_PATTERN`.
Intersight's own `Name` field is a chassis slot rather than a hostname, so
the collector reads the name off the service profile instead. Set the
pattern to whatever your fleet uses before running:

```bash
export INVENTORY_COLLECTOR_NAME_PATTERN='^ocp'
```

If `names matching ...` comes back `0`, a real run would collect nothing,
and I need to know that before it is ever scheduled. The first ten
resolved names are printed so you can sanity-check them by eye.

### c. What Intersight actually manages there

Output section **"2. WHAT THIS TENANT HOLDS"**, the `ManagementMode`
counts.

- Servers in `Intersight` or `IntersightStandalone` mode are machines
  UCS Central cannot see. Those are what this collector is for.
- Servers in `UCSM` mode are deliberately **not** collected — they belong
  to the UCS Central collector, and collecting both would make one
  document's fields flip on whichever job ran last.

If it turns out to be *all* `UCSM`, this collector correctly collects
nothing at your site, and that is worth knowing plainly.

### d. Whether a "0 drives" dry-run result is real, or boot-optimized storage

Output section **"5. BOOT-OPTIMIZED STORAGE"**, added 2026-09-01 after a
field run reported `storage — not read total across 0 drive(s)` for a
real server. Modern Cisco servers commonly boot from an M.2 RAID module
or legacy SD card, modelled as entirely separate MO classes
(`storage.FlexUtilController`/`FlexFlashController`) this collector does
not query — a server configured that way has genuinely **zero**
`storage.PhysicalDisk` rows, which is correct, not a bug.

- `SETTLED` means at least one 0-drive server has boot-optimized drives
  instead — worth building support for; send me the output and I will.
- `INCONCLUSIVE` means a server reports zero drives everywhere this probe
  checked. Either it genuinely has none (diskless, boot-from-SAN), or
  there is a storage class this probe still doesn't cover — check the
  Intersight UI's own Storage inventory tab for that server by hand.
- `N/A` means every sampled server already reported at least one
  `storage.PhysicalDisk` row — nothing to investigate.

### e. Any drive the mapping cannot size

Output section **"6. DISK CAPACITY"**, prompted by a live report of one
server whose drives all showed correct model/serial/type/health but
`size unknown` while the Intersight UI showed a real size for the same
drives. `NonCoercedSizeBytes` and `Size` are the only two fields the
mapping reads for capacity (`docs/cisco-collectors.md`, "Units"); the
section prints both raw values for every drive it cannot parse rather
than guessing at a third field or a sentinel. Sections 5 and 6 resolve a
disk to its server through its `storage.Controller`, which carries three
owner relationships (`ComputeBlade`/`ComputeRackUnit`/`ComputeBoard`,
confirmed against Cisco's generated Go SDK, `model_storage_controller.go`)
— the tool prints how many resolved which way, because "0 direct, all via
`ComputeBoard`" was the collector's real, live-confirmed join defect
(ADR-0017, "The `ComputeBoard` join gap"). The probe carries its own
copy of that join rather than importing the collector's private helpers:
it queries classes the collector does not read, and should not be
coupled to internals built for a different set of resources.

---

## If the key is rejected

Intersight answers several different problems with the same HTTP 401, but
it distinguishes them internally and the collector now reads that — so
the message tells you which of three situations you are in:

- *"the collector's request signing… not in your API key"* — a bug on my
  side, not yours. Send me the `traceId` from the message.
- *"received no API key credentials at all"* — something in transit
  stripped the header, typically a proxy.
- *"check in that order: API Key ID… private PEM… still listed"* — the
  credential itself. Also check the appliance's clock: a drifted clock
  looks exactly like a bad key, and the message says so when it can tell.

---

## Optional, and genuinely worth it while you are in there

**The `total_memory` MB question is now SETTLED, 2026-09-07** — a live
UCS Central dry run confirmed the collector's number against the UCS
UI's own figure exactly, which also backs Intersight's identical
assumption. See ADR-0009's "Update (2026-09-07)" for the full write-up,
including why the OS-visible (`free`/OpenShift) figure being *lower*
than the collector's number is expected and not a units bug.

**The disk/interface `health`/`oper=UNKNOWN` question is also SETTLED,
same day** — sections 4 and 5 below found two real gaps (now fixed:
`offline`/`self-test-failed` disk states, five `AdaptorExtEthIf` values)
and two non-gaps (`NA`/`unknown`/`indeterminate` genuinely mean "doesn't
apply"/"no verdict"/"can't tell" in Cisco's own terms, and a vNIC's
`oper_state` turned out to be a different, equipment-health field, not a
link-state one — reading `"unknown"` on almost every vNIC is expected).
See ADR-0009's "Update (2026-09-07): the health/oper vocabulary gaps,
settled by sections 4 and 5" for the full reasoning behind each one.

**The fabric interconnect name is also DONE, same day** — section 6
previewed `topSystem.name` without wiring anything in; the user then
independently confirmed it live against a real air-gapped domain with a
short `ucsmsdk` script of their own, which settled it. Every
`[PHYSICAL]` fabric attachment line now carries the domain's cluster
name alongside `fabric A`/`fabric B`. See ADR-0009's "Update
(2026-09-07): `fabric_name` built and confirmed live".

Nothing on this list is open any more, but the commands below still work
as a general health check if you want to run them again:

```bash
uv run python -m tools.verify_ucs_central --show-names 15 | tee ucs-central-verify.txt
uv run python -m tools.run_collector --manager-type UCS_CENTRAL \
  --dry-run --limit 3 | tee ucs-dryrun.txt
```

---

## What to send back, and what is in it

`intersight-verify.txt`, plus either dry-run file if you ran one.

**Skim it before it leaves the secure environment.** It deliberately
contains server names, models, serial numbers and management IP
addresses — those are the point of the exercise. It contains **no**
credential: the API key's private half is never printed, and there is no
debug flag anywhere that would print it.

If your site's rules do not allow hostnames or serials out, redact them
and say so. The memory comparison and the counts are still worth having
on their own.


---

# Part 2 — HPE OneView

Added 2026-09-04, for the first real run of the OneView collector
(`docs/adr/0022-oneview-only-hpe-collector.md`). Same contract as part 1:
read-only, writes nothing, and the session it opens is deleted on the way
out.

## The short version

```bash
cd /path/to/server_scan

export INVENTORY_ONEVIEW_IP=<your OneView appliance hostname>   # bare host, no https://
export INVENTORY_ONEVIEW_USERNAME=<a read-only OneView account>
export INVENTORY_ONEVIEW_PASSWORD='<its password>'

uv run python -m tools.verify_oneview | tee oneview-verify.txt
```

Send back `oneview-verify.txt`. Safe to run repeatedly.

If it passes and you want to see the records a real run would ingest —
still writing nothing:

```bash
uv run python -m tools.run_collector --manager-type ONEVIEW \
  --dry-run --limit 3 | tee oneview-dryrun.txt
```

**A read-only account is enough**, and is what to use: this collector
never writes to OneView. A session is created at login and deleted at the
end; an appliance allows 960 active sessions from one source IP, each
living 24 idle hours, so a leaked one is not free.

## Why there is no emulator for this

Worth saying plainly, because "just test it against a lab appliance"
sounds obvious. **There is no OneView equivalent of Cisco's UCS Platform
Emulator.** HPE's 60-day OneView trial is a *real appliance*, not a
hardware simulator, so with no HPE hardware attached
`GET /rest/server-hardware` returns an empty collection: it would prove
authentication, versioning, pagination and error handling, and **zero**
field mappings — which is where every defect UCSPE found for Cisco
actually lived. The Synergy Data Center Simulator is partner-only and is
Synergy blades, not the DL rack servers this estate runs.

## The four things I actually need from the output

### a. The core-count check — the single most important line

Printed as the `HEADLINE —` block right after the hardware fetch, and
repeated on its own as the last `>>>` line of the run, because it is the
one line worth reading if you read nothing else. HPE documents
`processorCoreCount` as "Number of cores available **per processor**",
while this platform's `cpu_cores` is whole-system, so the mapping
computes `processorCount * processorCoreCount`. The probe checks that
against the sum of each socket's own `TotalCores` from `/processors` on
the sampled servers, and prints `CORE COUNT: CONFIRMED`, `WRONG` or
`INCONCLUSIVE`.

- Agreement confirms the mapping.
- A disagreement means **every server's core count is wrong fleet-wide**,
  and I change the mapping before this is ever scheduled.

### b. Does paging get past the 256-profile ceiling

Output section **2**. HPE documents `/rest/server-profiles` as capped at
256 with "the list is truncated", and does *not* say whether
`nextPageUri` continues past it. This matters more than it sounds: the
server's **name comes from its profile**, so if the cap is per *query*
rather than per request, an estate with more than 256 profiles cannot be
fully enumerated and the collector needs to shard by filter.

The client already detects a short read and logs
`oneview.collection_truncated` at ERROR naming both counts — but
detection is not a fix, and one run answers it.

### c. Does an iLO-4 server report any hardware at all

Output section **4**, printed as a populated-fields table split by iLO
generation. Every *subresource* on an iLO 4 is documented to fail with
`InsufficientFirmware` ("The minimum version to collect some types of
inventory is iLO 5 v1.20") and the collector reports those as `None`
rather than zero. What HPE does **not** document is whether the
*top-level* fields — `memoryMb`, `processorCount`, `processorCoreCount`,
`portMap` — also come back empty.

If they do, iLO-4 machines get identity and nothing else, and the cost of
collecting HPE from OneView alone is much higher than the design assumed.
That is a decision-changing answer, so it is worth the one run.

### d. Do HPE's GPU names match the catalog

Output section **7**. OneView reports a GPU as a `Devices` entry with a
model string and **no memory field anywhere**, so VRAM comes entirely
from the built-in GPU catalog
(`docs/adr/0021-built-in-gpu-catalog-with-model-matching.md`). HPE
rebrands NVIDIA cards — `"HPE NVIDIA L40S 48GB PCIe Accelerator"` — and
the matching rules for those were written against *realistic* spellings,
not observed ones.

The probe prints every GPU string the estate reports with a CATALOG
HIT/MISS verdict. A MISS is not a bug; it is one line of
`INVENTORY_GPU_MODELS`, or a row I add to the table if the card's VRAM is
on a vendor datasheet.

## The rest of what it prints

Lower stakes, all worth having while you are in there: what `mpModel`
really contains per generation (section 3), which `mpIpAddresses` entry
is the reachable one and whether there is always one (section 5), what
the appliance does when `X-Api-Version` is omitted (section 1), whether
`serverName` holds anything without HPE AMS (section 6), the full mapped
`ProviderServer` for a few sampled servers (section 8), whether
`subResources` is an object or an array so the dead branch can be deleted
(section 9), and whether `expand=all` already returns power supplies or
each server costs a `/powerSupplies` call (section 10 — the difference
between a ~15-request sweep and a ~2500-request one).

## If the login is rejected

- **Check the account first.** A read-only OneView user is enough; the
  collector force-sets `loginMsgAck` on every login, so a pending login
  banner is not the cause.
- **`INVENTORY_ONEVIEW_IP` is a bare hostname** — no `https://`, no port,
  no path.
- **TLS verification is off by default** (`INVENTORY_ONEVIEW_VERIFY_TLS`),
  because an appliance in an air-gapped estate ships a self-signed
  certificate. If you have a real chain, turn it on.
- **An API-version complaint** means the appliance's supported range does
  not include what the collector asked for. The collector discovers the
  version from `GET /rest/version` and clamps it to 8000 (OneView 10.20,
  the newest reference these mappings were read against);
  `INVENTORY_ONEVIEW_API_VERSION` pins it explicitly. Send me the
  appliance's `minimumVersion`/`currentVersion` — the probe prints both.

## What to send back, and what is in it

`oneview-verify.txt`, plus `oneview-dryrun.txt` if you ran it.

**Skim it before it leaves the secure environment**, same as part 1. It
deliberately contains server names, models, serial numbers and
management-processor addresses — those are the point. It contains **no**
credential: the password is never printed, and there is no debug flag
that would print it. If your site's rules do not allow hostnames or
serials out, redact them and say so; the core-count check, the paging
answer and the populated-fields table are still worth having on their
own.

---

# Part 3 — Does a Dell iDRAC report GPU VRAM?

Added 2026-09-05. This is one HTTP request, not a probe script, and it
needs **a Dell server with a GPU fitted** — any other Dell tells you
nothing.

## Why it is worth the two minutes

No Cisco or HPE management API has a field for a GPU's memory size at
all, which is why this platform ships a built-in catalog of 30 cards and
looks VRAM up by model (ADR-0021). Redfish is different: it has a
standard field, and the collector already reads it —
`MemorySummary.TotalMemorySizeMiB` on a `ProcessorType == "GPU"` member,
standard since Redfish 1.0. Dell's hardware is collected over Redfish
from each iDRAC, so Dell *may* be reporting real VRAM already, in which
case the catalog is only a fallback there.

What nobody has confirmed is whether iDRAC actually fills that field in
for an ordinary add-in GPU, or leaves it empty. The collector handles
both — a real value always wins, and the catalog fills the gap otherwise
— so nothing is broken either way. But which one happens decides whether
Dell GPU VRAM is measured or inferred, and that is worth writing down
rather than assuming.

## What to run

Against the iDRAC of a Dell that has a GPU, with any read-only account:

```bash
IDRAC=10.0.0.5
USER=readonly-user

# 1. List the processors. GPUs appear here alongside CPUs.
curl -sk -u "$USER" "https://$IDRAC/redfish/v1/Systems/System.Embedded.1/Processors"

# 2. Pick the @odata.id of one whose id looks like a GPU (Dell names them
#    "Video.Embedded.1", "ProcessorGPU.Slot.N" or similar), and fetch it:
curl -sk -u "$USER" "https://$IDRAC/redfish/v1/Systems/System.Embedded.1/Processors/<that-id>"
```

`-k` skips certificate verification, which self-signed iDRACs need.
`curl` will prompt for the password so it stays out of your shell
history.

## What I need back

The **whole second response**, verbatim. Four fields decide it:

| Field | What it tells me |
|---|---|
| `ProcessorType` | Must be `"GPU"` — anything else and the collector never treats it as one. |
| `MemorySummary.TotalMemorySizeMiB` | **The answer.** A number means iDRAC reports real VRAM. Absent or `null` means the catalog is carrying Dell. |
| `ProcessorMemory[].CapacityMiB` | The pre-2020.4 fallback path. If `MemorySummary` is missing but this is present, the collector still gets a real figure. |
| `Model` | The string the catalog matches on — e.g. `NVIDIA A100-PCIE-40GB`. Tells me whether the built-in table would have matched it anyway. |

Please also say the **iDRAC firmware version** (`System > Overview` in
the UI, or `/redfish/v1/Managers/iDRAC.Embedded.1`), since the
`MemorySummary` path only exists from Redfish 2020.4 onward.

## What each outcome means

- **A real `TotalMemorySizeMiB`** — Dell reports measured VRAM and the
  catalog quietly stops mattering for Dell. It stays load-bearing for
  Cisco and HPE, which have no such field.
- **Missing on modern firmware** — the standard path is decorative on
  real hardware and the catalog carries every vendor. Also worth knowing:
  it means `Model` matching is the only thing standing between a Dell GPU
  and a blank VRAM column, so the table's coverage matters more than
  assumed.
- **A `Model` string the table does not have** — send it and I will add a
  row, with the VRAM cited from NVIDIA's or AMD's datasheet.

Record the result in `docs/adr/0021-built-in-gpu-catalog-with-model-
matching.md`, under its 2026-09-05 update.
