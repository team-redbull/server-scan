# ADR-0017: Cisco Intersight collector

**Status:** Accepted and implemented. **First run against the user's own
on-prem Intersight is done** (`tools/verify_intersight`, 2026-09-01):
auth, name resolution and the `TotalMemory` unit are now settled against
real data. That same field test also surfaced two scope-cut
justifications in Decision 5 that turned out to be stale citations of an
earlier ADR-0009 — `cpu_model` has since been added back (see Decision 5,
and `docs/notes/intersight-inventory-model.md`'s "Follow-up 2026-09-01");
fabric-interconnect identity was re-verified and stays cut, for a
different and now-confirmed reason.

**It also found and fixed a real bug in the request plan itself, not
just a scope-cut question.** `storage.Controller` carries a `ComputeBoard`
relationship the collector's join never followed — only `ComputeBlade`/
`ComputeRackUnit`. On this tenant's hardware, **0 of 37** storage
controllers set either of those; all 37 set only `ComputeBoard`. Every
drive on every server was therefore silently unread — not a scope cut,
not a "diskless server," a join gap in code that was already shipped.
`graphics.Card` and `processor.Unit` carry the same `ComputeBoard`
relationship and were fixed the same way pre-emptively, and a `--dry-run`
rerun on the same tenant **confirmed** both drives and `cpu_model` now
populate correctly (`adapter.Unit` and `management.Controller` carry no
`ComputeBoard` relationship at all and were correctly left alone). See
"The request plan" and the "Validation" section's second field-test
entry below.

**It also added the first field the platform's domain model had a slot
for but no provider had ever filled**: PSU identity and health
(`equipment.Psu`, "The request plan"'s "PSU identity and health"
paragraph), which caught and fixed a real, previously dead bug in the
health engine's `power.failed_psu_count` fact along the way — see
`docs/cisco-collectors.md`, "Power supplies (PSUs)".

The transport and auth path have been exercised against the **real
`intersight.com` service**; the field mapping has now seen a real tenant,
though a small (19-server) one. Read "Validation: what this has and has
not been tested against" before trusting any field it produces.

**Date:** 2026-08-29

Research behind every claim here: `docs/notes/intersight-auth.md`,
`docs/notes/intersight-inventory-model.md`,
`docs/notes/intersight-query-and-scale.md`,
`docs/notes/intersight-testability.md`. Facts cited below as
`signing.py:236` are file:line in the installed SDK wheel
(`intersight==1.0.11.2026072720`), which is the contract — the generated
models are the OpenAPI spec rendered as Python, and they beat prose docs
wherever the two disagree.

---

## Context

`INTERSIGHT` has had configuration slots since ADR-0012 —
`INVENTORY_INTERSIGHT_IP`/`_USERNAME`/`_PASSWORD`, a `ManagerType`
member, entries in both `_LOGIN_FIELDS` and `_ENDPOINT_FIELD`, a
`values.yaml` block and a Secret-template block — and no implementation.
`tools/run_collector.py`'s `_PROVIDER_FACTORIES` has no entry, so
`--manager-type INTERSIGHT` raises `NotImplementedError`. This ADR
decides how to close that gap.

Two things make this collector different from every one that came before
it, and both are decided here rather than discovered later.

**It is the first collector that actually reaches the platform's 10,000
server target.** UCS Central costs ~11 round trips for a whole Cisco
fleet but is bounded by Central's domain registry; the Redfish collector
costs ~25 round trips *per BMC* and ADR-0016 states plainly that it does
not reach 10k. Intersight's OData API answers fleet-wide list queries
whose cost is a function of page count, not server count.

**And it is the first collector with a deployment dependency this
platform cannot satisfy on its own.** Every other collector talks to
something the customer already owns. Intersight's SaaS endpoint is on the
public internet, which an air-gapped site by definition cannot reach.

---

## Decision 1 — Thin `httpx` client, not the official SDK

**Decision: write a small signing client on the `httpx` already in
`pyproject.toml`, and add exactly one new dependency (`cryptography`) for
the signature.**

| | Official `intersight` SDK | Thin `httpx` client |
|---|---|---|
| Wheel size | **57.6 MB** | 0 (httpx already present) |
| Unpacked | **328 MB** | — |
| Generated modules | **10,112 under `intersight/model/`** | — |
| New dependencies | `pycryptodome`, `pem`, `urllib3`, `python-dateutil` | `cryptography` |
| Code we would write | ~0 for transport, all of the mapping | ~60 lines signing, ~30 paging |
| 429 handling | **none — `rest.py` raises on any non-2xx** | ours, deliberately |
| `mypy` | generated, dynamically-typed `model_utils` machinery | ours, typed |

Evidence for the SDK column: `Requires-Dist` in the wheel's `METADATA`;
`ls intersight/model | wc -l` → 10112; the scale research confirmed
`rest.py` has no retry/backoff for HTTP status codes and
`Configuration.retries` defaults to `None`, which falls through to
urllib3's connection-level `Retry(3)` — that does **not** cover 429
unless `status_forcelist` is passed explicitly. So the SDK does not save
us the one piece of transport work that actually matters here.

The case for the SDK is that it saves us from hand-rolling HTTP Signature
crypto. That is a real risk and it is why this is a decision rather than
an obvious call. Three things reduce it to acceptable:

1. The scheme is small and fully specified: `hs2019`, sign
   `(request-target)`, `Host`, `Date`, `Digest`, base64 the signature,
   emit an `Authorization: Signature ...` header. This is composing a
   documented string and calling a library's `sign()`; it is not
   inventing a construction.
2. It is directly unit-testable against a fixed key and a frozen clock —
   deterministic output, no network. The SDK's own `signing.py` is the
   reference implementation to test against, and it is on disk.
3. **We would use roughly 8 of the SDK's 10,112 models.** We map raw JSON
   into `ProviderServer` at the port boundary regardless — that is what
   every provider in this repo does. The generated model layer is not
   value we would consume; it is 328 MB of mirror we would carry to throw
   away.

Air-gap weight (`docs/air-gap.md`): `requirements.txt` and `pylock.toml`
are generated exports for mirroring, and ADR-0013's precedent is that the
right answer to a heavy dependency is often deletion rather than a bump.
Pinning `ucsmsdk`/`ucscsdk` to whatever the mirror happens to carry is
already a documented friction; a 57.6 MB wheel is materially more of it.

**Cost accepted:** we own the signing code, including any future change
to Intersight's accepted signature scheme. Mitigated by the unit tests
above and by the fact that `hs2019` is a published draft standard, not a
Cisco-private construction.

**Outcome:** `cryptography==46.0.3` added, and `requirements.txt` /
`pylock.toml` regenerated for the air-gapped mirror. Neither
`cryptography` nor `pycryptodome` was previously in the tree, so *either*
option added a crypto dependency; this one adds the smaller of the two
and no generated model layer. **If the deployment's mirror carries
`pycryptodome` but not `cryptography`, swapping the library is a
contained change to `signing.py` alone** — the decision does not
change.

---

## Decision 2 — The API secret is a PEM in an env var, named for what it is

**Decision: `INVENTORY_INTERSIGHT_API_KEY_ID` and
`INVENTORY_INTERSIGHT_API_KEY_PEM`. No mounted volume.**

> **Revised 2026-08-29, after the user reviewed it.** This decision
> originally kept the `_USERNAME`/`_PASSWORD` names every other vendor
> uses, on the grounds that one uniform shape per vendor is what keeps
> the Secret template uniform, and that the confusion was "a
> documentation problem". That reasoning was wrong in a specific way:
> the *shape* is what the Secret template needs (a pair of values), not
> the *names*, and the pair is still a pair. Calling an API key a
> password made the wrong action — pasting an account password — the
> obvious first guess, and no amount of surrounding documentation fixes a
> variable whose name asserts something false at the point of use. The
> original text is kept below the line for the record.
>
> `_LOGIN_FIELDS` still maps `INTERSIGHT` to a two-field tuple, so
> `ManagerConnection` and the Secret template are untouched; only which
> settings fields that tuple names has changed, which is what makes
> `ManagerNotConfiguredError` print the right variable. `IntersightProvider`
> now takes `api_key_id`/`api_key_pem` explicitly rather than a
> `ManagerConnection`, so the word "username" survives at exactly one
> adaptation point in `run_collector`, with a comment saying why.
>
> Breaking for anyone who had configured it — nobody has, since the
> collector did not exist until this ADR.

Intersight has **no username/password path for the REST API at all**. It
is strictly `(API Key ID, PEM private key)`, and every request is signed;
`Configuration.auth_settings()` only ever builds cookie / http_signature
/ oAuth2-bearer, and Cisco's own example uses only http_signature.

The decisive fact for this platform:
`HttpSigningConfiguration(key_id, signing_scheme, private_key_path=None,
private_key_string=None, private_key_passphrase=None)` — `signing.py:126`
— and `_load_private_key` reads `private_key_string` **first**, touching
the filesystem only when it is `None` (`signing.py:236`). A PEM can
therefore live in an ordinary environment variable. ADR-0012's "no secret
volume to mount" holds, and ADR-0016's deviation is not repeated.

Multi-line values survive the whole chain: Helm renders a block scalar
from `values.yaml` into the Secret's `stringData`, Kubernetes injects it
with `envFrom`, and pydantic-settings reads it verbatim. **This satisfies
the requirement that a deployment be configured entirely from
`values.yaml` with no pre-existing Secret** — `collectors.existingSecret`
stays opt-in and empty, exactly as it is for every other vendor today.

*(Superseded, kept for the record.)* ~~Not renaming the settings fields
is deliberate laziness with a reason: the `username`/`password` shape is
already load-bearing in five files (`settings.py`, `credentials/env.py`,
`.env.example`, `values.yaml`, `collector-credentials-secret.yaml`),
`_LOGIN_FIELDS` is keyed on it, and one uniform shape per vendor is what
makes the Secret template uniform. The confusion is a documentation
problem and gets a documentation fix.~~ See the revision note above.

**We do not support a passphrase-encrypted key in v1.** The SDK supports
one (`private_key_passphrase`), but a passphrase would need a fourth
value with nowhere uniform to put it, and it protects a secret that is
already sitting next to it in the same Secret. Unencrypted PEM only,
stated in `.env.example`.

**Never logged:** the PEM, the signature, the `Authorization` header. One
concrete hazard found in the SDK and worth recording even though we are
not using it: `Configuration.debug = True` sets
`http.client.HTTPConnection.debuglevel = 1`, which `print()`s raw request
headers — `Authorization` included — to stdout, bypassing the logger
entirely. Nothing in this collector may ever wire a flag to that.

---

## Decision 3 — Overlap with UCS Central is resolved by `ManagementMode`

**This is the highest-risk decision here. Decision: collect only
`ManagementMode in ('Intersight', 'IntersightStandalone')` by default,
operator-overridable through
`INVENTORY_INTERSIGHT_MANAGEMENT_MODES`; never collect `UCSM` by
default.**

`IngestService` correlates on `(vendor, serial_normalized)`. A Cisco
server collected by *both* `UCS_CENTRAL` and `INTERSIGHT` is therefore
**one document whose `source_provider`, `external_id`, `manager_id` and
every mapped field flip on every run**, depending on which CronJob fired
last. That is not a cosmetic race: it churns the audit trail, and two
collectors disagreeing about a field (say memory, if the unit question
below lands wrong) would look like real hardware change.

The clean discriminator comes from the contract itself:

- `compute.PhysicalSummary.ManagementMode` enumerates exactly
  `IntersightStandalone` | `UCSM` | `Intersight`
  (`compute_physical_summary.py:471`).
- `ServiceProfile` is documented "The distinguished name of the service
  profile to which the server is associated to. **It is applicable only
  for servers which are managed via UCSM**" (`:496`).

So `ManagementMode == 'UCSM'` is *precisely* the set UCS Central already
owns, by Cisco's own definition. Excluding it makes the two Cisco
collectors partition the fleet rather than race over it.

Rejected alternatives:

- **Scope by Organization.** Does not work: no inventory MO
  (`compute.PhysicalSummary`/`Blade`/`RackUnit`) has an `Organization`
  field — only policy and profile MOs do. Verified in the SDK models.
- **Accept the overlap.** Rejected: silent, continuous field churn on
  shared servers, with no operator-visible symptom until someone reads
  the audit trail and asks why a server keeps changing collector.
- **Retire the UCS Central collector.** Not on the table: it is validated
  against a live Central with 152 domains, and Intersight cannot see a
  UCS domain that was never claimed into it.

**Cost accepted:** a server that Intersight genuinely manages in `UCSM`
mode and that is *not* registered with UCS Central falls through the gap
and is collected by nobody. The override exists for exactly that estate.

---

## Decision 4 — `external_id` is `intersight/<Moid>`

`Moid` is a stable 24-hex identifier, unique within the tenant, and is
the join key the whole request plan already depends on. Prefixed rather
than bare so it is self-describing next to a UCS Central
`compute/sys-1009/...` DN and a Redfish `redfish://.../Systems/1`.

**What happens to history when a machine changes collector** — the
question worth answering explicitly, because Decision 3 does not make it
impossible, only unlikely. Correlation is on `(vendor,
serial_normalized)`, and Intersight servers are `cisco` like UCS
Central's. So the *document survives*: the same server is found, and its
`external_id` and `source_provider` are overwritten in place. No
duplicate, no lost history, one audit event recording the change. That is
the correct outcome; it is only pathological if it happens on *every*
run, which is what Decision 3 prevents.

---

## Decision 5 — Scope cuts, stated honestly

What v1 deliberately will not collect, and why:

- **GPU memory, temperature, power draw, ECC state and error counts.**
  Not a cut — **a capability ceiling of the API**. `pci.Device`,
  `graphics.Card` and `graphics.Controller` carry model/vendor/serial/PCI
  address and *no* memory, thermal, power or ECC field anywhere in this
  SDK version. These report `None`, never `0`. The Redfish collector gets
  this data because it reads `ProcessorMetrics`/`EnvironmentMetrics` off
  the BMC directly; Intersight does not expose an equivalent.
  **Reconfirmed 2026-09-01** against Cisco's own current official metrics
  documentation, independent of the pinned SDK: every documented hardware
  telemetry category (`hw.current`/`fan`/`host`/`memory`/`network`/
  `power_supply`/`signal_power`/`temperature`/`voltage`,
  `system.cpu`/`memory`) has no GPU/graphics/PCIe entry at all. Two
  independent sources now agree; see `docs/cisco-collectors.md`'s "GPUs"
  section.
- **`speed_mbps` on attachments.** Neither `adapter.ExtEthInterface` nor
  `adapter.HostEthInterface` has a numeric speed field. Only the
  switch-side `ether.PhysicalPort`/`ether.HostPort` do, as *free-form
  strings* whose format is unverified. `None`, not a guess.
- ~~`cpu_model`~~ — **REVERSED 2026-09-01.** This cut was based on a
  stale citation: it claimed to be "the same reason ADR-0009 cut it," but
  ADR-0009's own 2026-08-16 update had already reversed that cut before
  this ADR was written — `docs/cisco-collectors.md`'s "CPU model" section
  and `ucs_manager/mapping.py:_cpu_model` show UCS Manager/Central
  collecting it today, fleet-cheap. Re-researched against Intersight's
  own model (`docs/notes/intersight-inventory-model.md`, "Follow-up
  2026-09-01", §11): `processor.Unit` exists, lists fleet-wide at
  `/api/v1/processor/Units`, and carries a direct `ComputeBlade`/
  `ComputeRackUnit` relationship — the identical cost class as
  `storage.Controller`/`graphics.Card`, already in the request plan.
  Implemented; see "The request plan" table above.
- **Fabric interconnect identity** (`fabric_model`, `fabric_serial`).
  **Still cut, but the "same cut, same reason" framing was also stale**
  (same follow-up research, §14) — re-verified rather than assumed this
  time. Two different answers depending on management mode: for
  `IntersightStandalone` servers (all of this platform's field-tested
  tenant so far) it is **structurally not applicable** — a standalone
  server has no Fabric Interconnect in its topology, so `None` is
  correct, not a gap. For `Intersight`-mode (IMM) servers, which *do* sit
  behind a real FI, the MO exists (`network.Element`) but the per-server
  join costs three more fleet-wide queries chained through a `PeerDn`
  **string** match rather than a `Moid` relationship — genuinely pricier
  than every other join in this table, and pricier than UCS Central's
  equivalent (which gets disambiguation for free from being
  domain-scoped to one FI pair at a time). Worth adding only once this
  platform actually collects IMM-mode servers with something reading
  these fields — not implemented.
- **Per-drive detail is *kept*.** Unlike ADR-0009, this one is cheap
  here: `storage.PhysicalDisk` is one more fleet-wide list call, and
  `docs/adr/0016`'s `storage.failed_drive` policy depends on it.

---

## Decision 6 — No concurrency knob; a 429 policy and a memory bound instead

`INVENTORY_UCS_CENTRAL_DOMAIN_CONCURRENCY` exists because domains are
independent endpoints. Intersight is **one** endpoint answering
sequential paged list queries, so a concurrency knob would only add
throttling risk against a rate limit Cisco does not publish. Not added.

What is needed instead:

1. **Our own 429 backoff.** The SDK has none, and Cisco publishes no
   limit numbers — which means the limit will be discovered in
   production. Honour `Retry-After` when present, exponential backoff
   with jitter otherwise, bounded retries, and a run budget so a
   throttled run ends with a summary rather than hanging.
2. **`$orderby=Moid` on every paged query.** `$top`/`$skip` is the only
   paging mechanism (no next-page token), and no vendor documentation
   states the result set is stable across pages. Ordering on an immutable
   key is our own prudence, recorded as such and not as a documented
   fact.
3. **A stated memory ceiling.** This is the honest cost of the fleet-wide
   join: the collector holds join tables (adapter units, interfaces,
   controllers, disks, profiles) in memory while it streams servers out.
   `list_servers()` still streams `ProviderServer`s — it never
   materialises 10k of them — but the *join tables* are proportional to
   fleet size, which no previous collector's memory was. `$select` is
   used on every query to keep only mapped fields, and the CronJob gets a
   memory limit sized for it.

---

## The request plan (the thing that makes this scale)

Every child MO carries an **inverse** reference back to its owner, so
sub-resources are listed once fleet-wide and joined client-side. Verified
directly in the SDK models:

| Query | Joins to owner via |
|---|---|
| `compute/PhysicalSummaries` | (the anchor; `Moid`) |
| `server/Profiles` | `assigned_server` → `ComputePhysical` |
| `adapter/Units` | `compute_blade` / `compute_rack_unit` |
| `adapter/ExtEthInterfaces` → `PHYSICAL` | `adapter_unit` → `adapter.Unit` |
| `adapter/HostEthInterfaces` → `VNIC` | `adapter_unit` → `adapter.Unit` |
| `storage/Controllers` | `compute_blade` / `compute_rack_unit`, **or `compute_board` → `compute/Boards`** |
| `storage/PhysicalDisks` | `parent` → `storage.Controller` |
| `management/Controllers` | `compute_blade` / `compute_rack_unit` |
| `processor/Units` | `compute_blade` / `compute_rack_unit`, **or `compute_board`** (added 2026-09-01, see below) |
| `graphics/Cards` | `compute_blade` / `compute_rack_unit`, **or `compute_board`** |
| `compute/Boards` | `compute_blade` / `compute_rack_unit` (added 2026-09-01, see below) |
| `equipment/Psus` | `compute_rack_unit` **only** — no `compute_blade`, no `compute_board` (added 2026-09-01) |

**The `compute_board` fallback, added 2026-09-01, is not optional on
some hardware.** A live tenant showed **0 of 37** `storage.Controller`
rows setting `ComputeBlade`/`ComputeRackUnit` at all — every one of them
set only `ComputeBoard`, which the collector did not previously read.
Every drive on every server was silently unread as a result.
`graphics.Card` and `processor.Unit` carry the identical relationship
and got the same fallback pre-emptively; `adapter.Unit` and
`management.Controller` do **not** carry a `ComputeBoard` relationship at
all (confirmed against Cisco's own generated Go SDK, independent of the
Python wheel this ADR otherwise cites) and must never have it added to
their `$select` — an unsupported field risks failing the whole query.
`compute/Boards` is one more fleet-wide query, itself joined via a direct
`ComputeBlade`/`ComputeRackUnit` relationship.

**PSU identity and health, added 2026-09-01, is the first field this
collector reports that the platform's domain model already had a slot
for but no provider had ever filled.** `app.domain.models.hardware.Psu`/
`Power` and the health engine's `power.psu_count`/`power.failed_psu_count`
metrics predate this collector; `IngestService` hardcoded
`Power(psus=[])` until this shipped. `equipment.Psu` is unique among
every MO here: it carries `ComputeRackUnit` but no `ComputeBlade` and no
`ComputeBoard` relationship at all, confirmed against Cisco's own
generated Go SDK — a blade's PSUs belong to its shared chassis
(`EquipmentChassis`), not to one blade, so **this collector reports no
PSUs for a blade server**, a real capability gap rather than a join to
fix. Fixing this also caught a real, if previously dead, bug: the health
engine's `power.failed_psu_count` fact compared `Psu.health` against the
literal string `"OK"`, a vocabulary no provider (this one included)
actually emits — `Psu.health` uses `normalize_oper_state`'s
UP/DOWN/DISABLED/UNKNOWN, the same pattern `gpu()`'s own `health` field
already used. That comparison would have counted every healthy PSU as
failed the moment real data arrived; it is now `health == "DOWN"`. See
`docs/cisco-collectors.md`, "Power supplies (PSUs)".

At `$top=1000` (the documented maximum) that is on the order of **~120
requests for 10,000 servers**, flat in fleet size — against the Redfish
collector's ~250,000 at the same scale. This is the first collector in
the repo that meets the platform's stated target without qualification.

`adapter.ExtEthInterface` is `PHYSICAL` and `adapter.HostEthInterface` is
`VNIC` — the latter's own docstring uses the word "vNIC". Getting this
backwards is exactly the ADR-0009 defect that made physical port counts
always zero. `vnic.EthIf` is a *design-time policy* object and must not
be used for live state.

Server-side `startswith(Name,'ocp')` is available and will be used as a
**bandwidth optimisation only** — `_NameFilteredProvider` remains the
correctness boundary, unchanged, exactly as for UCS Central.

`INTERSIGHT` belongs in **neither** `_ENDPOINTLESS_TYPES` (it has a real
configured endpoint) **nor** `_UNFILTERED_TYPES` (its servers carry the
`ocp4-...` names the pattern exists to filter). Stated here so it is a
decision rather than an inference.

---

## The name trap — worse than UCS Manager's, and not fully settled

ADR-0009's most expensive defect was naming every server after its
chassis slot because a UCS server's name lives on its *service profile*,
not on `computeBlade.name`. Intersight has the same trap, and the
contract is explicit about it: `compute.PhysicalSummary.Name` is
documented as **never** an operator hostname — it is
FI-cluster-name + chassis/slot when UCSM-attached, the CIMC's own name in
standalone mode, or model + chassis/server id in IMM mode.

Since the platform parses the **site** out of this name and matches it
against `^ocp`, sourcing it wrong does not fail loudly — it collects
nothing, or labels the whole fleet "Unassigned".

Planned resolution, by mode:

- **`Intersight` (IMM):** `server.Profile.name`, joined via
  `assigned_server`. Also yields `profile_template_*` through
  `src_template`.
- **`IntersightStandalone`:** no profile exists. Fall back to
  `compute.PhysicalSummary.Name` (the CIMC's own name), then
  `UserLabel`.
- **`UCSM`:** excluded by Decision 3, so moot by default. If an operator
  overrides that, parse the `ServiceProfile` DN the way
  `ucs_manager/mapping.py` already does.

**UNVERIFIED, highest priority to settle:** `server.Profile.ManagementMode`
enumerates only two values, omitting `UCSM`, while
`compute.PhysicalSummary.ManagementMode` enumerates three. That suggests
UCSM-mode servers may get no `server.Profile` MO at all — consistent with
Decision 3, but it is an inference from an enum mismatch, not a
confirmed fact. One live tenant call settles it.

---

## UNVERIFIED — what only live hardware can settle

Recorded here rather than buried, because ADR-0009's validation found
five defects that were invisible without real hardware, and nothing
reachable today can play that role for Intersight (see below).

1. ~~`TotalMemory`'s unit~~ — **SETTLED 2026-09-01**, see "Validation"
   below and `docs/cisco-collectors.md`'s "Units" section. It is MiB, as
   assumed; no code change needed.
2. Whether UCSM-mode servers have a `server.Profile` at all (above).
3. `assigned_server` vs `associated_server` precedence on `server.Profile`.
4. `mgmt_ip_address` vs `ipv4_address` as the authoritative BMC address,
   and where the BMC MAC actually lives.
5. Clock-skew tolerance and its exact failure signature. The SDK raises a
   generic 401 `UnauthorizedException` for skew, expiry, revocation and a
   wrong key id alike — four causes, one symptom. `health_check()` will
   have to guess between them, so it will proactively sanity-check the
   pod clock rather than pretend it can tell.
6. Cisco publishes no rate-limit numbers anywhere reachable.
7. `ether.PhysicalPort.oper_speed` string format.
8. Whether the Private Virtual Appliance serves an identical MO surface
   to SaaS (see below).
9. **Account region.** Intersight's own 401 message asks the operator to
   "verify the API key and associated account region", which implies a
   tenant can live in a region other than the one `intersight.com`
   resolves to. Nothing in this collector models a region. If a tenant
   turns out to need a regional hostname, it goes in
   `INVENTORY_INTERSIGHT_IP` and needs no code change — but that it
   *works* is unverified.
10. **Which field carries `processor.Unit`'s human-readable CPU name** —
    `Model` (what the collector reads, mirroring `ucs_manager.mapping`'s
    convention) or `Description` (a second candidate the SDK docstrings
    don't distinguish). Settle by comparing both fields on one live row
    against the Intersight UI's own CPU panel for that socket
    (`docs/notes/intersight-inventory-model.md`, "Follow-up 2026-09-01",
    §11).
11. ~~The tenant's 0-drive report~~ — **SETTLED 2026-09-01, and the
    explanation was neither hypothesis.** `pci.Device` was checked and
    ruled out first (it's a GPU-riser identity MO with no storage
    relationship at all). The M.2 boot-optimized storage subsystem
    (`storage.FlexUtilController`/`FlexFlashController`) was checked
    live next and found **real for exactly one server** (0
    `storage.PhysicalDisk`, 1 `FlexUtilPhysicalDrive`, 2
    `FlexFlashPhysicalDrive`) — but the other 18 servers reported zero
    everywhere, and a UI check on one of them showed a RAID controller
    and physical drive Intersight's own console could see. **The actual
    cause was the `ComputeBoard` join gap** described above in "The
    request plan": `storage.Controller` was silently failing to join to
    its server on this hardware at all — 0 of 37 rows resolved the old
    way. Fixed (see "The request plan"); a rerun after the fix is the
    next thing to confirm. The single-server FlexUtil/FlexFlash finding
    is still real and separate — support for it remains **not
    implemented**, tracked as its own item below.
12. **Boot-optimized storage** (`storage.FlexUtilController`/
    `FlexUtilPhysicalDrive` current, `storage.FlexFlashController`/
    `FlexFlashPhysicalDrive` legacy SD-card) — confirmed present on at
    least one real server (previous item), unrelated to the
    `ComputeBoard` join bug that explained the rest of the fleet. **Not
    implemented** — costs two more fleet-wide queries beyond what the
    `ComputeBoard` fix already added (`storage/FlexUtilControllers`/
    `FlexFlashControllers` and their physical-drive classes) through a
    two-hop join via `compute.Board`, and needs both a current and a
    legacy generation to be complete. Worth building once the
    `ComputeBoard` fix's rerun shows whether any *other* servers turn out
    to need it too, rather than the one already found.

---

## The three questions that gated this, and their answers

All three were put to the user before any code was written, because each
one could have made the collector not worth building. Their answers are
recorded here as the premises the design rests on — if any of them stops
being true, this ADR needs revisiting rather than the code needing a
patch.

**1. Air-gap → there is a reachable Intersight in the target site.**
Refined twice on 2026-08-29. The build was gated on "a Private Virtual
Appliance exists or is planned"; the user then clarified they have **no
PVA**, and then that they do have an Intersight they can point this at
from the air-gapped environment. Both statements are compatible: Cisco
ships the on-prem product under several names, and "PVA" is only one of
them.

What matters for this ADR is the operational fact, and it holds:
`INVENTORY_INTERSIGHT_IP` points at an on-prem FQDN rather than
`intersight.com`. **The collector is deployable and, more importantly,
testable there** — which is what turns this ADR's UNVERIFIED list from a
standing risk into a one-command errand
(`docs/field-test-checklist.md`). See the dated update below: TLS
certificate verification was later made unconditionally off, so an
internal CA is no longer something this collector needs at all.

The standing dependency is unchanged and still worth stating: an on-prem
Intersight is a licensed Cisco product this platform does not control,
which no other collector here requires. Exactly which flavour is deployed
is a detail the first `verify_intersight` run will surface.

**2. What it manages → IMM and standalone-claimed servers.**
Which is precisely the set UCS Central cannot see, so Decision 3's
default (`Intersight,IntersightStandalone`) collects real inventory
rather than nothing, and the two Cisco collectors partition the fleet.

**3. Validation → build it, and document what is unproven.**
Hence the "Validation" section below, and
`tools/verify_intersight.py`, which exists so the first real tenant
settles the open items in minutes rather than over a debugging session.

---

## Validation: what this has and has not been tested against

**No server inventory has ever been mapped from real data.** The DevNet
Intersight sandbox — the closest equivalent to the UCS Platform Emulator
that found five real defects in ADR-0009 — went offline on 2026-08-01 for
a rebuild with no committed return date before ~Q1 2027, and Cisco's API
reference publishes response *schemas* without example *values*.
Schema-shaped fixtures cannot catch the class of bug UCSPE caught: a
field that is empty in practice, a unit that is not what the name
implies, a parent relationship that is null on real hardware.

### What a live probe against `intersight.com` did prove (2026-08-29)

The collector was pointed at the real service with a locally-generated
RSA key and a syntactically valid but unregistered API Key ID. That is
the one live test available without a tenant, and it settled more than
expected:

- **The request reaches Intersight's IAM and is processed**, not rejected
  at the edge. The response is `HTTP 401` with
  `code: "UnauthorizedOperation"`,
  `messageId: "iam_apikey_authheader_invalid"` — a key-lookup failure, in
  the shape the collector's 401 handling expects. TLS, the request line,
  the query encoding and the `Authorization` header shape all survive
  the round trip.
- **The error body schema is now verified**, where the research had it
  marked UNVERIFIED: a JSON object with `code`, `message`, `messageId`
  and `traceId`. The client was changed as a result — it now surfaces
  Intersight's own `message` and the `traceId` alongside our guidance,
  because the `traceId` is the only handle Cisco can use to find that
  exact request and discarding it costs an operator their support case.
- **The clock-skew check works against the real service.** Measured skew
  was 0.9s against Intersight's own `Date` header, so the message
  correctly omitted the clock note rather than firing spuriously.
- **A hint worth keeping:** Intersight's own message says "Verify the API
  key and associated **account region**." Region is not currently modelled
  anywhere in this collector. For SaaS tenants outside the default region
  the endpoint may need to be a regional hostname rather than
  `intersight.com` — untested, and listed under UNVERIFIED below.

### A second probe settled the "is our header even parsed" question

Six deliberately-broken requests, same day, same unregistered key. All
answered HTTP 401, but **`messageId` distinguishes them**:

| What was sent | `messageId` |
|---|---|
| No `Authorization` header at all | `iam_cookie_invalid` |
| `Authorization: total garbage` | `iam_apikey_signature_invalid` |
| `Signature keyId="..."` and nothing else | `iam_apikey_signature_invalid` |
| **Our header**, unknown key | `iam_apikey_authheader_invalid` |
| **Our header**, signature bytes zeroed | `iam_apikey_authheader_invalid` |
| **Our header**, `Date` back-dated 2 hours | `iam_apikey_authheader_invalid` |
| **Our header**, signed for a different path | `iam_apikey_authheader_invalid` |

A structurally malformed header is rejected with a *different* code than
ours. **Intersight therefore parses our `Authorization` header
successfully** and fails at key lookup — which is live evidence that the
`hs2019` construction is structurally correct, independent of the offline
SDK comparison.

The client was changed again as a result: those three `messageId` values
mean three different things to an operator, and it now says so. A
malformed header is a bug in *this collector* and the message says the
key is probably fine; `iam_cookie_invalid` means something in transit
stripped the header; only `iam_apikey_authheader_invalid` warrants the
check-your-credentials list.

What this still does **not** prove: that any signature is *accepted*. A
registered key is required for that — with an unknown key id, a wrong
signature and an unknown key are indistinguishable, as the zeroed-signature
and back-dated rows above show. The offline byte-identical comparison
against Cisco's own SDK is what carries that weight.

**Also learned, and a limit on what any keyless probe can do:
authentication is checked before routing.** A request to
`/api/v1/compute/NotARealThing` returns exactly the same 401 as a real
path, so **resource paths cannot be validated against the live service
without a tenant**. They are verified against the SDK's own
`endpoint_path` definitions instead, and remain unproven until a real
run.

What *has* been proved offline:

- **The signature is byte-identical to Cisco's own SDK.** The `hs2019`
  construction was run side by side against `intersight==1.0.11`'s
  `signing.py` with a fixed key and a frozen clock: for an RSA (v2) key
  the `Authorization` header matches character for character. For an EC
  (v3) key it necessarily differs — the SDK derives its nonce per
  RFC 6979 and `cryptography` draws one randomly — so that half was
  proved instead by verifying the signature against its own public key.
  Both properties are locked into
  `tests/unit/infrastructure/providers/test_intersight_signing.py`.
- **Every attribute name, type and relationship** used by the mapping was
  read out of the installed SDK's generated models, which are the OpenAPI
  contract rendered as Python — not from prose documentation, and not
  from recall.
- **The join topology** — that each sub-resource can reach its owning
  server — was verified field by field in those models, and is what
  Decision "The request plan" rests on.

What remains unproven is listed under UNVERIFIED below. The first item
is a live-data risk, not a theoretical one.

### The first real tenant run (2026-09-01)

`tools/verify_intersight --show-names 15` against the user's on-prem
Private Virtual Appliance. The tenant is small — 19 servers, all
`IntersightStandalone` — and the bulk of the user's fleet lives on UCS
Central and OneView, so this is a narrow sample, not a fleet-scale one.

- **Auth works end to end.** The API key was accepted and inventory was
  readable — the byte-identical signature construction (proved offline
  against Cisco's own SDK) is now also proved against a real appliance,
  not just `intersight.com`.
- **Name resolution: GOOD, 19/19.** Every server resolved a
  `server.Profile` name and matched `INVENTORY_COLLECTOR_NAME_PATTERN`.
- **`TotalMemory`'s unit is SETTLED: MiB, as assumed.** A sampled
  server reported `TotalMemory = AvailableMemory = 786432`. The
  `memory/Arrays` relationship filter did not resolve on this tenant
  (see the UNVERIFIED note below on relationship-filter support), so the
  authoritative DIMM-sum check in `verify_intersight` could not run —
  the fallback was a hand comparison against the Intersight UI's own
  "Memory Capacity" figure, which read **768.0 GiB**.
  `786432 ÷ 1024 = 768.0` exactly, matching the MiB assumption to the
  decimal. This is now moved to `docs/cisco-collectors.md`'s "Units"
  section as a confirmed fact; no change to `_BYTES_PER_MB` is needed.
- **Not yet run:** `--manager-type INTERSIGHT --dry-run` against a full
  ingest, and the `memory/Arrays` relationship filter remains unverified
  on this tenant — worth another look with `--sample` large enough to
  land on a server whose filter does resolve, since the UI comparison is
  a fallback the tool itself only reaches for when that filter fails.

### The `ComputeBoard` join gap (2026-09-01, same tenant)

A follow-up dry-run on the same 19-server tenant reported `cpu_model`
unknown, fabric identity empty, and every server's storage as `not read
total across 0 drive(s)`. `cpu_model` and fabric identity turned out to
be the (then-still-unimplemented / correctly-N/A) scope cuts described
above, but storage did not fit either. Chasing it down:

1. Added `verify_intersight.py` section 5, checking whether Cisco's M.2
   boot-optimized storage subsystem explained the 0-drive report. Result:
   **real for exactly one server** (`FlexUtil=1`, `FlexFlash=2`,
   `PhysicalDisk=0`), but the other 18 servers reported zero across every
   storage class the probe checked — a different signal than "found the
   explanation."
2. A UI check on one of those 18 showed a RAID controller and a physical
   drive that the API-based probe could not see at all — proof the
   controller existed in Intersight's own inventory, not proof of
   diskless hardware.
3. Extended section 5 to report how many `storage.Controller` rows
   resolve via `ComputeBlade`/`ComputeRackUnit` versus only via
   `ComputeBoard`. Result: **0 of 37 resolved the direct way; all 37
   resolved only through `ComputeBoard`** — a relationship the collector
   never read. Confirmed the same relationship exists on `graphics.Card`
   and `processor.Unit` (not on `adapter.Unit` or
   `management.Controller`) against Cisco's own generated Go SDK before
   touching any `$select` field, since an unsupported field risks failing
   the whole query.

Fixed in `IntersightProvider._owning_server`/`_build_joins`: one more
fleet-wide `compute/Boards` query, and a fallback through it for the
three affected classes. See "The request plan" above for the mechanism.

**CONFIRMED 2026-09-01, `--dry-run` rerun on the same tenant: drives and
`cpu_model` now populate correctly.** `processor.Unit` carries the
identical `ComputeBoard`-only pattern `storage.Controller` was directly
confirmed to have — inferred at the time the fix shipped, now verified
rather than assumed. GPUs were not separately confirmed (this tenant
appears to have no GPU hardware at all — `0` there is expected, not
evidence either way for `graphics.Card`'s own fallback).

### A second field pass (2026-09-07, same tenant): GPU catalog matching, and an OperState vocabulary gap

This run did have GPU hardware — a T4 — which the earlier pass's tenant
did not, and turned up two more real findings.

**Fixed: the GPU catalog never matched Intersight's own product name.**
`graphics.Card.Model` on this tenant reads, confirmed against the
Intersight UI's own "Model" field: `"NVIDIA T4 PCIe 16GB 70W"` — NVIDIA's
real marketing name for the part, TDP included. `GpuCatalog._for_identifier`
matched neither the full string nor its capacity-fallback path, for two
independent reasons layered on top of each other:

1. Every existing example of a bus/form-factor word (`PCIe`, `SXM`, ...)
   in `gpu_catalog.py`'s test suite and its own docstring trails the
   capacity (HPE's `"H100 80GB PCIe"`) — `_FORM_FACTOR_WORDS` was only
   ever stripped from the *end* of the word list. Intersight puts it
   *before* the capacity instead (`T4 PCIe 16GB`), which the trailing-only
   strip never reached.
2. `70W` is a marketing suffix `_TRAILING_NOISE` had no entry for — no
   word in that set is a *number*, since every existing case is a fixed
   noun. Left in place, it broke the base+capacity fallback too: that
   path requires the *last* word to be the capacity, and `70W` sat after
   `16GB`.

Both are fixed in `gpu_catalog.py`: `_FORM_FACTOR_WORDS` is now stripped
positionally rather than only trailing, and a new `_TRAILING_WATTAGE`
pattern (`\d+W`) is stripped alongside `_TRAILING_NOISE` in `_words()`.
`"NVIDIA T4 PCIe 16GB 70W"` now resolves to the built-in `NVIDIA T4 16GB`
row. See `tests/unit/domain/test_gpu_catalog.py`'s
`test_a_form_factor_word_before_the_capacity_still_matches` and
`test_a_trailing_wattage_figure_is_stripped_like_marketing_noise` — both
confirmed to fail on the pre-fix code.

**Found and fixed: `normalize_oper_state` did not recognize Intersight's
own `OperState` vocabulary.** The PSU section above (2026-09-01) asserted
`psu()`'s `health` uses "the same vocabulary `gpu()` already uses for the
identical OperState-sourced pattern" — true as a statement about the
code, but that assumption was never checked against a live tenant. A UI
check on this tenant's server showed `OperState: OK` for both PSUs.
`ucs_common._OPER_STATE_MAP` was written for UCS Manager's XML vocabulary
(`"operable"`, `"inoperable"`, `"link-up"`, ...) and had no `"ok"` entry,
so `normalize_oper_state("OK")` fell through to `UNKNOWN` — silently, the
same failure mode this ADR's own PSU section describes for the *health
engine's* now-fixed literal-`"OK"` bug, just one layer lower, in the
field this collector reports rather than in the fact that reads it. This
affects three call sites identically: `psu()`, `gpu()`, and
`attachment()`'s `oper_state` (`intersight/mapping.py`).

Cisco's Intersight API reference and its generated Python/Go SDKs do not
enumerate `OperState`'s allowed values at all (confirmed: the field is a
plain `string`, `OperReason` has a documented enum, `OperState` does
not) — there is no contract to read this from, only a live tenant.
`tools/verify_intersight.py` gained a new **section 7, "OperState
VOCABULARY"**, which reads `equipment/Psus`, `graphics/Cards`,
`adapter/HostEthInterfaces` and `adapter/ExtEthInterfaces` directly and
prints every distinct raw `OperState` value each reports, alongside what
`normalize_oper_state` currently does with it.

**Section 7's output against this tenant, same day:**

```
equipment/Psus  (PSU health):
    'OK'                x36      -> UNKNOWN   <- not recognized
    'Operable'          x2       -> UP

graphics/Cards  (GPU health):
    ''                  x1       -> UNKNOWN   <- not recognized

adapter/HostEthInterfaces  (vNIC oper_state):
    'Down'              x33      -> DOWN
    ''                  x10      -> UNKNOWN   <- not recognized

adapter/ExtEthInterfaces  (physical port oper_state):
    'Up'                x75      -> UP
    'Down'              x37      -> DOWN
```

Two things this settles, and one it does not:

- **`equipment.Psu.OperState` genuinely splits between `"OK"` (36) and
  `"Operable"` (2) for the identical healthy state on one tenant** —
  Intersight does not even use one consistent spelling with itself,
  let alone with UCS Manager/Central's XML vocabulary. `"ok"` is now
  added to `_OPER_STATE_MAP` (mapped to `UP`); `"operable"` already
  worked. See `tests/unit/infrastructure/providers/test_intersight_mapping.py`'s
  `test_psu_health_recognizes_the_live_intersight_ok_spelling`, confirmed
  failing pre-fix.
- **The empty-string rows are not the same bug and were not changed.**
  `graphics/Cards` and `adapter/HostEthInterfaces` both report `''` for
  some rows — a field genuinely not populated, not a spelling this
  collector fails to recognize. `normalize_oper_state("")` correctly
  answers `UNKNOWN` for "no data", the same as it would for `None`; there
  is nothing to add a map entry for.
- **Still not settled: the DOWN/DISABLED counterpart to Intersight's own
  `OperState` vocabulary.** `adapter/ExtEthInterfaces` reports `"Up"`/
  `"Down"` (already recognized, case-insensitively) but nothing on this
  tenant showed a failed PSU or GPU, so whether a genuinely down one
  reports `"inoperable"` (carried forward from UCS's vocabulary, on the
  guess that Intersight's backend partly shares it) or something else
  entirely — `"Critical"`, `"Failed"`, `"NotOk"` — is unconfirmed. The
  existing DOWN/DISABLED entries are kept as a best guess, not as
  something this run actually observed failing. Worth another
  `verify_intersight` run if this estate ever has a failed PSU/GPU/NIC to
  compare against.

### The same "OK" gap, one field over: `storage.PhysicalDisk.Health` (2026-09-07)

The `--dry-run` this field pass was validating against showed most drives
reading `health=UNKNOWN`. New `verify_intersight` **section 8, "DISK
HEALTH VOCABULARY"** (added the same day as section 7, same shape of
check) grouped every raw `Health`/`DriveState`/`FailurePredicted`
combination this tenant's drives report:

```
Health       DriveState       FailurePredicted     count     mapped
'OK'         'ENABLED'        'False'              180       -> UNKNOWN  <- not recognized
'OK'         'Online'         'False'               36       -> UNKNOWN  <- not recognized
''           'JBOD'           'False'                8       -> HEALTHY
''           'Online'         'False'                2       -> HEALTHY

216 of 226 sampled drive(s) read health=UNKNOWN.
```

Same root cause as the PSU finding above, on a sibling field:
`equipment.Psu.OperState` and `storage.PhysicalDisk.Health` both use
`"OK"` as Intersight's generic healthy-status string, and `_drive_health`
(`intersight/mapping.py`) had no `"ok"` entry in its healthy set (`good`,
`healthy`, `online`, `optimal`, `jbod`, `unconfigured good`) — 216 of 226
drives, silently. `Health` is read before `DriveState`, and was truthy
for all 216, so `DriveState` — which *did* already recognize
`"Online"`/`"JBOD"` on the 10 drives it was actually consulted for
(rows 3 and 4 above, where `Health` was blank) — never got a chance to
save them.

Fixed: `"ok"` added to `_drive_health`'s healthy set. See
`test_drive_health_recognizes_the_live_intersight_ok_spelling`, confirmed
failing pre-fix. The DOWN/CRITICAL counterpart is exactly as unconfirmed
here as it is for `OperState` above — no drive on this tenant reported
`Health`/`DriveState` as anything but `"OK"`/healthy, so `_drive_health`'s
existing `warning`/`critical` sets are untested against Intersight's own
spellings, only inherited from whatever informed them originally.

---

## Corrections made during implementation

Two claims in this ADR's own first draft turned out to be wrong when
checked against the SDK models. Recorded rather than quietly fixed,
because both were the kind of plausible assumption that ships a defect.

1. **`server.Profile` has no `Dn` field at all.** The draft planned to
   read `profile.Dn` for `profile_dn`, and `_PROFILE_FIELDS` selected it.
   Selecting a field the schema does not define risks failing the whole
   `server/Profiles` query — which would have cost *every server its
   name*, silently, exactly the failure mode ADR-0009 hit with a
   nonexistent MO class. `profile_dn` now comes only from a UCSM-mode
   summary's `ServiceProfile`, and is `None` for an IMM server.
   **Consequence worth stating: an Intersight server whose name carries
   no site token has no org path to fall back to, so it resolves to no
   site.** UCS Central's servers do; these do not.
2. **The `^ocp` name filter cannot be pushed server-side.** The draft
   said `startswith(Name,'ocp')` would be used as a bandwidth
   optimisation. It cannot be: `compute.PhysicalSummary.Name` is *not*
   the operator's hostname (see "The name trap"), so filtering on it
   server-side would discard the whole fleet. The only filter pushed to
   the API is `ManagementMode`, which is a genuine field on the summary.
   `_NameFilteredProvider` remains the sole name filter, client-side.

A third detail, not a correction but a hedge: `server.Profile` carries
**both** `AssignedServer` and `AssociatedServer`, and nothing documents
their precedence. The join consults `AssociatedServer` first — the
machine actually running the configuration — and falls back to
`AssignedServer`.

### Found by an independent review pass

The mapping was reviewed against the Phase-1 research notes by someone
other than its author. Four findings were real and are fixed:

1. **The UCSM fallback this ADR promised was never implemented.** The
   section above says a `UCSM`-mode server would parse its
   `ServiceProfile` DN for a name; the code only ever read
   `server.Profile.Name`. Since `INVENTORY_INTERSIGHT_MANAGEMENT_MODES`
   is operator-editable, adding `UCSM` to it would have named every such
   server after its chassis slot — the ADR-0009 defect, reintroduced by
   an ADR section that described a behaviour the code did not have.
   `mapping.profile_name_from_dn` now reads `ls-<name>` the way
   `ucs_manager.mapping` does.
2. **`nic_macs` could report `()` when a NIC table had failed.** The two
   adapter-interface queries fail independently. If the physical-port
   table failed while the vNIC table succeeded and a server genuinely had
   no vNICs, the merge produced an empty tuple — an assertion that the
   server has no MACs, which `IngestService` would write over the stored
   ones. `()` is now claimed only when *both* tables were read.
3. **The run budget did not cover the join phase.** Every fleet-wide
   sub-resource read happened before the budget was ever consulted, so a
   throttled tenant could burn the whole budget there and be killed by
   `activeDeadlineSeconds` with nothing reported — precisely what the
   budget exists to prevent. It is now checked between tables, and a
   table skipped for time is `None` (unread), not empty.
4. **`bmc_address` preferred the summary over the management
   interface.** Reversed: `bmc_mac` comes from the interface, so the
   address should too, or a server reports an address and a MAC that need
   not describe the same interface.

A fifth finding — that the memory check filtered `memory/Units` on
relationship fields it does not have — had already been found and fixed
independently; `memory.Unit` reaches its server only through
`memory.Array`.

---

## Superseded: the questions as they were asked

**1. Air-gap: is there a Private Virtual Appliance?**
An air-gapped site can reach Intersight **only** via the on-prem Private
Virtual Appliance — never `intersight.com`, and not the *Connected*
Virtual Appliance either, which still calls home to public SaaS by
design. A PVA is a commercial SKU (no free or evaluation tier found),
16–48 vCPU / 32–96 GB / 2 TB. That is a deployment dependency no other
collector in this repo carries. If this platform's air-gapped target site
has no PVA, this collector cannot run there at all, and it is worth
knowing that before it is built rather than after.

**2. Testability: there is no emulator, and there will not be one soon.**
CLAUDE.md records that testability without real hardware was *the
deciding factor* for building UCS first — Cisco's free UCSPE answers real
API calls, and validating against it found five real defects. The
Intersight equivalent, the DevNet Sandbox, is **offline**: Cisco took the
entire sandbox catalog down on 2026-08-01 for a rebuild, targeting "early
2027" with no committed date. The best reachable substitute today is a
free SaaS account with no claimed devices, which proves signing, paging
and empty-result handling — and proves **nothing** about field
population, units or nullability, which is precisely the class of bug
UCSPE caught. Schema-only fixtures from the OpenAPI spec cannot catch it
either; the API reference publishes types, not example values.

So: this collector can be built to a good standard against the contract,
with honest unit tests, and it will be **unvalidated** in a way UCS
Manager's never was — with `TotalMemory`'s unit as a live, silent,
4.86%-wrong-on-every-server risk sitting in the middle of it.

**A third question, which is really the first one:** if this deployment's
Intersight is claiming the *same* UCS domains that UCS Central already
collects, then under Decision 3 this collector correctly collects
**nothing** — every one of those servers is `ManagementMode == 'UCSM'`.
It earns its place only if there are Intersight-managed (IMM) or
standalone-claimed servers that UCS Central cannot see.

All three were answered before implementation; see "The three questions
that gated this" above.

---

## What was built

| | |
|---|---|
| `..providers.intersight.signing` | `hs2019` request signing, pure and clock-injectable |
| `..providers.intersight.client` | Paged OData over `httpx`, 429 backoff, actionable errors |
| `..providers.intersight.mapping` | Managed objects -> `ProviderServer`, pure |
| `..providers.intersight.provider` | The fleet-wide join and the streamed run |
| `tools/verify_intersight.py` | Read-only pre-flight; GOOD / PARTIAL / BAD |
| `deploy/.../intersight-collector-cronjob.yaml` | One CronJob, every 3 hours (operator, 2026-10-04; 6 hours from 2026-09-10, hourly before — see `deploy/README.md`), values-only config |

`INTERSIGHT` is registered in `_PROVIDER_FACTORIES` and is in **neither**
`_ENDPOINTLESS_TYPES` nor `_UNFILTERED_TYPES`.

Two pieces of shared behaviour moved rather than being duplicated: the
Cisco interface-state vocabulary (`operable`/`link-up`/... ->
`UP`/`DOWN`/`DISABLED`) now lives in `..providers.ucs_common` and is used
by both Cisco collectors, so a value a live fleet turns up is mapped once
rather than in one collector only.

The fake generator models all three collectors, splitting Cisco blades
(UCS Central) from Cisco rack units (Intersight) the way the real
`ManagementMode` partition does, and reproducing each collector's
different GPU ceiling.

---

## Update (2026-08-31): TLS certificate verification is unconditionally off

`IntersightClient` no longer verifies the endpoint's TLS certificate,
full stop — not a default, not an opt-out gated on a recorded reason (an
earlier same-day iteration built exactly that, mirroring the Redfish
collector's per-host `verify_tls`/`verify_tls_reason` pair), a hardcoded
`verify=False`. `INVENTORY_INTERSIGHT_CA_BUNDLE`,
`INVENTORY_INTERSIGHT_TLS_VERIFY` and `_TLS_VERIFY_REASON` are all gone
— from `Settings`, from the Helm chart, and from every doc that
mentioned them.

This was an explicit, repeated user instruction against the air-gapped
lab appliance at `intersight.tomer.lab`, made after being told plainly
what it costs: the signed request and its response go to whatever
answers at `INVENTORY_INTERSIGHT_IP`, indistinguishable from a
man-in-the-middle, in **every** environment this code ever runs in —
including a real production SaaS or on-prem tenant, not just this lab.
The user was asked once whether to scope this to their local `.env`
only (keeping the reason-gated opt-out with a secure default) and chose
instead to remove the setting from the codebase entirely, twice, after
that tradeoff was stated in those terms.

If this collector is ever pointed at a real deployment, this is the
first thing to revisit — reintroducing a `verify=True`-by-default path
(with `INVENTORY_INTERSIGHT_CA_BUNDLE` for an on-prem appliance's
internal CA) is a small, self-contained change: see the `IntersightClient`
git history around this date for the shape it had before this update.
