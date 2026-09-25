# ADR-0032: `GET /servers/available` — a live-verified server lookup for BMH-creation callers

Date: 2026-09-13
Status: Accepted

## Context

`BareMetalHostUCS` (the `bmh-generator-operator`) creates a `BareMetalHost`
+ BMC `Secret` + `NMStateConfig` per server by querying HP OneView / Cisco
UCS Central / Dell OME / Cisco Intersight **live**, on every reconcile
(`src/*_server_strategy.py`). `server-scan` already collects and stores
that same inventory in MongoDB on a 6-hour cron
(`tools/run_collector.py`). Querying the vendor managers a second time,
per BMH, from a second codebase, is redundant, and it means two systems
each holding their own partial view of which servers are "available."

This ADR adds a read API that a BMH-creation flow calls instead: it
resolves candidates from Mongo (fast, fleet-wide, filterable), then
live-verifies only the one-to-few servers it is about to hand back before
returning them — never the whole matching set, and never on the write
path a normal `GET /servers` request takes.

It returns **data only** — never a `BareMetalHost`/`Secret`/
`NMStateConfig`, and never BMC credentials (server-scan doesn't hold
those; the caller still supplies `{VENDOR}_BMC_USERNAME`/`_PASSWORD` from
its own config, exactly as `BareMetalHostUCS/src/yaml_generators.py` does
today).

## Decision

### 1. One endpoint, two lookup modes

`GET /api/v1/servers/available`

- `?name=<exact server name>` — case-insensitive exact match on
  `Server.name`. Exactly one server must match, or 404. Declared in
  `servers.py` **before** `/servers/{server_id}` (same ordering trap the
  existing `# Before /servers/{server_id}: FastAPI matches in declaration
  order` comment already documents for `/servers/facets`).
- `?pattern=<regex>` — a real MongoDB `$regex` against `Server.name`, not
  the word-boundary token search `?search=` on `GET /servers` uses
  (ADR-0025). Deliberately unanchored-capable: a caller passes a spec
  prefix like `ocp-dell-r650-five-128c-1024gb-5tb-` and gets every
  matching server, ranked and drawn from (below). This *is* markedly more
  expensive than an anchored token-index search at fleet scale
  (ADR-0025's measured ~650ms/query for an unanchored regex over 52k
  servers, against 1–26ms anchored) — accepted here because this endpoint
  is called per BMH-creation event, not per page load, and because
  `count` (below) bounds how many candidates the live-recheck phase then
  costs on top of the query itself.
- Exactly one of `name`/`pattern` — 400 otherwise.
- `?count=<int>` — pattern mode only (400 combined with `name`). Default
  `1`. A new `max_available_count` setting, enforced the same way
  `PageSizeTooLargeError` bounds `page_size` today — this is what stops a
  caller from accidentally paying for a live recheck of the whole
  matching fleet in one call.
- `?vendor=<Vendor>` / `?source_provider=<ManagerType>` — optional,
  combinable with either mode and each other. `vendor=cisco` alone can't
  distinguish `UCS_CENTRAL` from `INTERSIGHT`; `source_provider` is what
  does. With `name=`, a mismatch is treated as not-found (404), not
  silently ignored.
- Response is **always** `{"items": [...], ...}` — the same list idiom
  `ServerListResponse` already uses — regardless of whether `count` was
  given, defaulted, or the mode is `name` (a one-item list on success).
  Each item is `AvailableServerItem` — **only what a BMH/NMState generator
  consumes**, see the 2026-09-13 update below for why it is not
  `ServerDetail`. The envelope around the list — which mode matched,
  `count` requested vs. returned, and per item whether its state reflects
  a fresh live recheck or a trust-Mongo fallback (Decision 5) — is
  additive metadata.

  **Update (2026-09-13, same day): the item is a purpose-built projection,
  not `ServerDetail`.** The first cut reused `ServerDetail` as the spec
  suggested. The operator then asked what `bmh-generator-operator` actually
  consumes, and a read of its `yaml_generators.py`, `server_profile_config.py`
  and `operator_bmh_gen.py` (branch `feature/multi-vendor-bond-nmstate-
  intersight`) gives a short, closed list: the server `name`; a BMC-driver
  vendor in *its* vocabulary — `HP` / `DELL` / `CISCO` / **`INTERSIGHT`**,
  because a UCS-managed and an Intersight-managed Cisco server take
  different `bmc.address` schemes (`ipmi://…:623` vs
  `redfish-virtualmedia://…`), a distinction `identity.vendor` alone cannot
  express; the **bare BMC host** (it runs `ipaddress.IPv4Address` on it and
  wraps the scheme itself); the **ordered NIC MAC list** its `select_macs`
  indexes with `first`/`last`/`N`; the **per-interface OS name** for the
  NMState bond members; and, for Dell, the FQDD each MAC belongs to. Nothing
  in `hardware`, `classification`, `connectivity`, `maintenance`,
  `openshift`, `unread_fields` or the audit/revision fields is read. So the
  item is now: `id`, `name`, `vendor`, `source_provider`, **`bmc_vendor`**
  (`schemas.bmc_vendor_for`, `None` for `STANDALONE` — the caller decides
  that driver), `bmc` (`BmcInfo`: `host`, `host_is_ip`, `address_raw`, …),
  `nic_macs`, `interfaces[]` (`name`, `mac`, `location`, **`os_name`** —
  folded in per interface so bond members are one parallel list, the exact
  shape `generate_nmstate_config(mac_addresses, nic_names)` takes),
  `site_id`, `health_overall`, `live_recheck_performed`. `site_id` and
  `health_overall` stay because they are one field each and a caller will
  want to log or label them; everything else is reachable via
  `GET /servers/{id}` if ever needed. `ServerDetail` is untouched.

  **What "live" means for a caller, stated once:** in `name` mode every
  field above comes from the `get_one()` read made during *this* request
  and persisted through `IngestService` before the response is built — not
  from the last CronJob run. The single exception is Decision 5's degrade,
  and `live_recheck_performed: false` names it; a caller that requires
  freshness should treat that value as a failure, not a warning.

### 2. Capacity-token aliasing is a config-driven catalog, not an `if`

Same shape as `SiteCatalog`/`GpuCatalog`/`NicNameCatalog`
(`<token>:<name-pattern-alias>`, comma-separated, parsed once via
`from_spec`, cached with `lru_cache`, keyed on the spec string so tests
pass literals): a new `INVENTORY_CAPACITY_ALIASES` setting, default
`"5tb:hypershift(?!-data),10tb:hypershift-data"`.

The alias only fires when `pattern` is **exactly** (case-insensitively)
equal to a configured alias key — `pattern=5tb` expands to `{$or:
[{name: /5tb/i}, {name: /hypershift(?!-data)/i}]}`. A compound pattern
that merely contains `5tb` as a substring (`...-128gb-10tb`) is used
verbatim, no expansion — ADR-0018's dated updates already record what an
over-eager substring rule cost this platform once; this table does not
repeat it. The negative lookahead is plain PCRE, which MongoDB's regex
engine (PCRE-based, same engine ADR-0025 already relies on) supports
natively — no new regex-safety code needed beyond what `pattern` already
goes through (existing pattern-length/ReDoS caution stays app-level, not
newly invented here).

### 3. Candidate ranking is Mongo-side filter + tier + random draw

For `pattern` mode, after `vendor`/`source_provider`/alias-expanded name
filters:

1. Filter to `openshift.lifecycle_state == AVAILABLE`,
   `maintenance.enabled == false`, `reachable == true` — a single Mongo
   query, no application-side post-filtering.
2. Group by `health.overall`, restricted to `{HEALTHY, INFO, WARNING,
   MAJOR}` (never `CRITICAL`; `UNKNOWN` — never evaluated — is also
   excluded, consistent with ADR-0027's "UNKNOWN is not a verdict").
   `HEALTH_SEVERITY_RANK` gives the fill order.
3. Within a tier, sample `count` (or the tier's full remainder) via
   Mongo's `$sample` aggregation stage rather than fetching every
   candidate and drawing in Python — the whole point of ranking is to
   avoid materializing a possibly-large matching set. Move to the next
   tier only once a tier's own draw is exhausted.
4. Each drawn candidate is live-rechecked (Decision 4). A candidate a
   recheck downgrades below the cutoff is dropped and replaced by another
   `$sample` draw from the same or next tier — bounded by a retry cap
   (`max_available_count`-sized worst case: never draw more replacement
   rounds than there are tiers × configured count, so a small fleet with
   every candidate flapping cannot loop indefinitely).
5. Fewer than `count` qualifying after every tier and every replacement
   round is honest partial fulfillment (200, with metadata saying how
   many were requested vs. returned), not an error. 404 (RFC 9457) is
   reserved for the genuinely-empty case: no name matched the pattern at
   all, or every match was `CRITICAL`/`UNKNOWN`/unassignable even before
   any live recheck ran — the response's `detail` says explicitly which.

### 4. `get_one()` — a new abstract method, one per-provider single-object fetch

```python
@dataclass(frozen=True, slots=True)
class ServerIdentity:
    """Whatever a provider needs to re-locate one already-ingested server."""
    serial: str | None = None
    external_id: str | None = None  # this manager's own DN/Moid/URI, from Server.identity.external_ids
    host: str | None = None          # BMC host, for Redfish-shaped lookups
    name: str | None = None          # fallback, e.g. UCS Central's lsServer name match


class ServerInventoryProvider(ABC):
    ...
    @abstractmethod
    async def get_one(self, identity: ServerIdentity) -> ProviderServer | None:
        """Fetch one server's current state, or None if it can no longer be found."""
```

Each of the six providers implements it by mirroring `BareMetalHostUCS`'s
own single-host/single-domain lookups rather than re-running
`_list_servers()` and filtering:

- **UCS Central → UCS Manager.** `UcsCentralProvider.get_one` queries
  Central's `lsServer` (already a small, directory-sized query — the
  precedent `_plan()` already runs) and matches by `identity.name`,
  exactly as `BareMetalHostUCS/src/ucs_server_strategy.py`'s
  `get_server_info` does. The matched profile's `pn_dn` (its physical-node
  DN, domain-local) is handed to a per-domain `UcsManagerProvider.get_one`
  (built via the existing `_new_domain_provider`), which calls
  `client.query_dn(pn_dn, hierarchy=True)` — one round trip that returns
  the compute unit **and every descendant MO** (`ucsmsdk` confirmed
  behaviour: `hierarchy=True` returns a flat MO list, not just the root).
  The flat list is bucketed by each MO's own `_class_id` (the same wire
  class-id string `query_classid("mgmtIf")` etc. already key on
  throughout this codebase — verified against the installed `ucsmsdk`
  0.9.18 source, not assumed) and fed through the *existing*
  `compute_unit_to_provider_server` mapping unchanged. No new bulk query,
  no whole-domain re-list — one Central query plus one hierarchical `query_dn`.
- **Intersight.** An OData `$filter` on `Serial` (or `Moid` when the
  serial is absent) against `compute/PhysicalSummaries`, then the same
  per-server sub-resource joins `_build_joins` already performs, scoped
  with a `$filter` on the owning relation (`ComputeBlade eq '<moid>' or
  ComputeRackUnit eq '<moid>'`, and for the three board-owned classes, a
  `compute/Boards` lookup first to resolve the owning board's `Moid`) —
  the same relation shape `_build_joins`/`_owning_server` already encode,
  just filtered per-server instead of fleet-wide.
- **OneView.** `GET /rest/server-hardware/{external_id}` for the hardware
  member, then `GET {member.serverProfileUri}` for its profile (and, if
  present, `GET {profile.serverProfileTemplateUri}` for the template
  name) — three direct-by-URI fetches, no bulk `get_all`. Power supplies
  and processors reuse the exact per-URI fetch OneView's bulk path already
  falls back to for a server the expanded payload didn't cover.
- **OpenManage.** OME's OData `$filter` on `DeviceServiceTag` against
  `/DeviceService/Devices` for the one device, then a single-target
  `RedfishStandaloneProvider` (via the same `redfish_provider_factory`
  seam `_list_servers` already uses) against just that one iDRAC.
- **Redfish standalone.** Already single-host in shape: `get_one` looks up
  `identity.host` in `self._targets` and reuses `_collect_systems` for
  that one target only.
- **Fake.** Regenerates the deterministic fleet (`generate_servers`,
  cheap — it's an in-memory generator, not I/O) and returns the one
  entry matching `identity`. A `get_one_overrides: dict[str,
  ProviderServer] | None` constructor parameter (default `None`, never
  read in production wiring) lets a test inject a downgraded verdict for
  one identity — the fake fleet is otherwise deterministic and could
  never on its own produce the "live recheck downgrades a candidate"
  scenario the test suite has to prove.

### 5. Live recheck is a real write, through `IngestService`, degrading to Mongo-trust when unconfigured

Before returning any candidate (the one `name` match, or each of the
`count` pattern-mode draws), its own manager is queried fresh via
`get_one()`, run through `IngestService`'s existing
correlate→classify→health-evaluate→persist pipeline (a new
`IngestService.ingest_one(provider_server, provider_type=...)` public
method — an extraction of what `_ingest_one` already builds, returning
the persisted `Server` rather than the create/update boolean
`ingest()`'s summary needs), and the list/detail cache is invalidated the
same way the two maintenance endpoints do (ADR-0028 — a live recheck is
an operator-triggered write, same class as maintenance, not the
5-CronJobs-write-continuously case ADR-0028 deliberately excludes from
invalidation).

**Decided, explicitly, per the spec's own instruction not to leave this
ambiguous: when a manager type's credentials are not configured on the
API pod, the endpoint skips live recheck for that candidate and trusts
the stored Mongo document.** Not a hard error. Reasoning:

- This mirrors every other "verify freshness, degrade to the stored
  value on failure" pattern already in this codebase — Redis cache-aside
  degrading to Mongo on any failure, and `Server.reachable=False`
  carrying the last-known hardware forward rather than blanking it.
- A hard error would make the endpoint unusable in any environment that
  hasn't (yet, or ever, for one vendor) wired every manager credential
  onto the API pod — including local dev, which by design runs the
  `fake` provider only and has no real vendor credentials at all.
- The alternative (hard error) would turn a missing `INTERSIGHT` key,
  say, into total unavailability of the endpoint for *every* vendor's
  servers whenever pattern-mode candidates happen to span providers,
  which is a worse failure mode than "this one candidate's health is
  whatever the last collection run saw, same as it always was before
  this endpoint existed."
- `ManagerNotConfiguredError` already exists and already means exactly
  "this manager type has no connection configured on this process" —
  reusing it here rather than inventing a second error type keeps one
  vocabulary for the same fact across the CLI and the API.

The response's per-item metadata says whether a live recheck actually ran
for that item, so a caller that *needs* freshness (rather than merely
preferring it) can tell the difference and decide for itself — this
endpoint does not silently claim freshness it didn't get.

**`REDFISH_STANDALONE` is the one collector this degrades for by
construction, not by configuration gap**, and that is a deliberate,
narrower decision than the general one above: its per-host credentials
live in files (`INVENTORY_REDFISH_INVENTORY_FILE`/
`_CREDENTIALS_FILE`) mounted only into its own CronJob pod. Mounting the
same files onto the API Deployment would put every standalone BMC's
password within reach of the pod exposed through the Route — a real
increase in blast radius for one collector out of five, to support a
freshness guarantee this decision already treats as best-effort
everywhere else. `OPENMANAGE`'s inner Redfish pass is unaffected: its BMC
login is the single shared `INVENTORY_OME_BMC_USERNAME`/`_PASSWORD`
account already in the collector-credentials Secret, not a per-host file.

### 6. Deployment: the API pod gets the same collector-credentials Secret the CronJobs already have

`deploy/helm/server-scan`'s `backend-deployment.yaml` gains one
`envFrom.secretRef` onto the same Secret every collector CronJob already
mounts (`{{ .Values.collectors.existingSecret | default (printf "%s-collector-credentials" .Release.Name) }}`)
— no new Secret, no new values toggle. Two reasons this isn't gated
behind a flag: `get_one()`'s degrade path (Decision 5) is safe on an
unconfigured value (empty string reads as "not configured", exactly as
`EnvConnectionResolver` already treats it for the collectors), and the
credentials this mounts are the same ones already living in the cluster
for the CronJobs — this widens who can *read* them, not what exists.
`UCS_MANAGER`'s login is included because `UCS_CENTRAL`'s `get_one` needs
it (same as the collector does); no `UCS_MANAGER`-specific wiring is
added, since it still has no endpoint of its own (ADR-0012).

`REDFISH_STANDALONE`'s inventory/credentials files are deliberately
**not** mounted onto the API Deployment (Decision 5's narrower point).

`tools/run_collector.py` gains one small, public function,
`build_provider_for_manager_type(manager_type, *, settings)` — the exact
resolution `_run()` already performs (resolve credentials, handle
`_ENDPOINTLESS_TYPES`, pre-flight `UCS_MANAGER`'s login for
`UCS_CENTRAL`, call `_build_provider`) minus the dry-run/name-filter/
reporting concerns a single-server lookup doesn't need. The API's new
route calls this, not a second, parallel resolution path — one place
decides how a `ManagerType` becomes a constructed provider, whether the
caller is a CronJob or a request handler.

## Non-goals (unchanged from the spec)

No `BareMetalHost`/`Secret`/`NMStateConfig` generation, no NIC bonding
policy (`mac_indices`) — the full ordered NIC list is returned, bonding
selection stays downstream. No BMC credentials in the response or
anywhere persisted. No reservation/lock: concurrent callers can race and
receive the same server — an accepted gap, not solved with new state
(the random draw is what keeps *repeated* queries from piling onto the
same one server, not a substitute for a real lock a future slice could
add if the race ever actually bites).

**`UcsManagerProvider.get_one`'s `lsServer`/`networkElement`/`topSystem`
queries stay whole-domain**, not scoped further: a service profile lives
outside the physical DN tree `query_dn(hierarchy=True)` returns (profiles
live under `org-root/...`, physical compute units under `sys/...` —
two separate trees joined only by `LsServer.pn_dn`), and the domain
itself has already been narrowed to one by `UcsCentralProvider.get_one`
before this runs — so a whole-domain profile/fabric-interconnect/cluster-
name query here is bounded by one domain's size, not the fleet's.

## Deferred

**Intersight's `get_one()` owner-relation filters
(`ComputeBlade.Moid eq '<moid>'` and siblings) are unverified against a
live tenant.** They mirror the same relation shape `_build_joins`/
`_owning_server` already parse from full, unfiltered rows — built from
Cisco's OpenAPI contract, following ADR-0017's own research bar, but
never exercised as a `$filter` expression against a real Intersight
endpoint. If a future `verify_intersight` pass finds the nested-property
filter syntax behaves differently than documented, only `IntersightProvider.
get_one` and its `_direct_owner_filter`/`_owner_filter`/`_in_filter`
helpers need to change — `_list_servers`' fleet-wide join is untouched by
this ADR and needs no re-verification.

## Consequences

- A new `INVENTORY_CAPACITY_ALIASES` and `INVENTORY_MAX_AVAILABLE_COUNT`
  setting, both documented in `.env.example`.
- The API pod's credential footprint changes for the first time since
  ADR-0012 — it goes from "no vendor credentials at all" to "the same
  read-mostly manager logins the collectors hold." `docs/arc42.md` §5/§7
  need updating to say so.
- `ServerInventoryProvider` gains a sixth mandatory method; every existing
  and future provider must implement it, same as `_list_servers`/
  `health_check` today.
- The `$sample`-based random draw means this endpoint's results are
  **not reproducible** run-to-run even against an unchanged fleet — this
  is a deliberate feature (Decision 3), not a testing inconvenience;
  tests prove randomness by running the query repeatedly and asserting
  variation, not by asserting a fixed sequence.

## Verification

`uv run pytest -q`, `uv run ruff check . && uv run ruff format --check .
&& uv run ty check backend/app tools tests`, `uv run python
scripts/check_comment_density.py`, `helm template
deploy/helm/server-scan` (the new `envFrom` entry), and the full `/gate`
skill. New tests cover the four health tiers and their exhaustion, the
alias-expansion exact-match-only rule, `vendor`/`source_provider`
disambiguating `UCS_CENTRAL` from `INTERSIGHT` within `vendor=cisco`, the
fake-provider-backed live-recheck-downgrades-a-candidate integration
scenario, `count` validation/clamping/randomness, and route ordering
against `/servers/{server_id}`.

## Update (2026-09-25): what a BMH caller actually needs to trust the answer

Building the `install-server` Temporal workflow against this endpoint — the
replacement for `bmh-generator-operator` that Decision 1's 2026-09-13 update
anticipated — surfaced four defects, each confirmed against a running
instance. All four are fixed; the endpoint's shape is otherwise unchanged.

### 1. `HEALTHY` is not evidence that a server can be installed

`GET /servers/available` returned servers with **no NIC MACs at all** — 38 of
371 assignable servers (10.2%) on a 2,500-server instance. Such a server cannot
become a `BareMetalHost`: `spec.bootMACAddress` is required and there is
nothing to put in it.

They were not merely included, they were drawn **first**, because they read
`HEALTHY`. That is the health engine working as designed and is worth stating
plainly, because it is counter-intuitive: `overall` is the worst category by
`HEALTH_SEVERITY_RANK`, `UNKNOWN` ranks *below* `HEALTHY` (0 < 1), and a
category with no data read is `UNKNOWN` and skipped (ADR-0027's 2026-09-21
update). A server whose NIC read failed therefore has no network facts to fail
a policy on and comes out `HEALTHY` overall. **The less that was read, the
healthier a server looks.**

The platform already knows: such a server carries `identity.nic_macs` in
`unread_fields`. The selection gate simply never consulted it — the "`None`
means could not read this run" rule was honoured in storage and dropped at the
one place a caller acts on it.

`?min_nic_macs=` now folds two clauses into the assignability filter and into
`server_still_qualifies`, which must agree or a candidate the query returned
would be rejected on every draw:

```python
f"identity.nic_macs.{min_nic_macs - 1}": {"$exists": True},   # at least N
"unread_fields": {"$ne": "identity.nic_macs"},                # and actually read
```

Both halves are load-bearing. The length test alone passes a server whose NIC
read failed and whose MACs were carried forward from an earlier run; the
`unread_fields` test alone passes one that genuinely has too few.

**It is opt-in — the default of `0` imposes nothing.** Excluding MAC-less
servers by default is arguably the right behaviour, since this endpoint exists
to serve BMH creation and such a server can never be used for it. It is
deliberately not the default yet: that would change what every existing caller
receives, silently, and the change is worth making on its own once
`install-server` is the only caller rather than bundled into the change that
introduced the parameter. `install-server` passes `2`.

### 2. `?health=` — the tier fill was not what a provisioning caller wanted

The fill is best-first across `HEALTHY` → `WARNING` → `MAJOR`, so a caller that
installs only on `HEALTHY` hardware was handed a `WARNING` server whenever no
`HEALTHY` one was free — and paid a live recheck, against a vendor manager, for
a candidate it then discarded. `?health=` restricts the draw to one tier.
`CRITICAL`/`UNKNOWN` remain rejected outright (400), as they are never
assignable.

This does not change the default: without `?health=` the endpoint fills exactly
as before. The platform's position that `WARNING` and `MAJOR` hardware is
provisionable is unchanged — the parameter lets a caller be stricter than the
platform, not the reverse.

### 3. `?name=` returned an arbitrary server, not "exactly one"

Decision 1 says *"Exactly one server must match, or 404."* The implementation
was `find_one` with **no sort**, which returns an arbitrary document when
several match — and several do: on the same instance, 2,500 documents carried
only 1,454 distinct names, 548 names belonging to more than one document. That
is not a seeding artifact. Correlation is on `(vendor, serial_normalized)`
(ADR-0011/0016), so one hostname legitimately spans several documents whenever a
machine is re-serialled, moved between vendors, or seen by two collectors.

Observed: `?name=ocp4-bat-yam-compute-02` matched three documents, `find_one`
returned the `INSTALLED`/`CRITICAL` one, and the endpoint answered **404 "no
longer available"** — while two assignable servers of that exact name sat in the
collection.

Now counted first: more than one match raises `AvailableServerNameAmbiguousError`
(409, `AVAILABLE_SERVER_NAME_AMBIGUOUS`) listing the matching ids in `details`,
so the caller narrows with `vendor`/`source_provider` rather than being handed a
machine it never chose. This makes the stated contract real; it does not change
it.

### 4. A filtered-out result blamed the pattern

`lookup_by_pattern` folded `extra_filters` into `base_filters` before the first
count, so `?pattern=ocp-dell&vendor=hp` answered *"no server name matches
'ocp-dell'"* — sending whoever reads it to debug a pattern that was fine. The
name clause is now counted alone first, and the filtered set second, so the
message names whichever emptied the result.

### `link_state` and `speed_mbps` on `AvailableInterface`

Bonding needs to know which ports are up, so `AvailableInterface` now carries
`link_state` (and `speed_mbps`, for asserting members match). This is the one
addition to the item since the 2026-09-13 update's "do not grow it back into a
full detail" — justified because without it the projection cannot answer the
question it exists to serve.

**How unevenly vendors report it is the caller's problem, and it is real.**
Dell (OpenManage) and `REDFISH_STANDALONE` report a true Redfish `LinkStatus`.
UCS gained a usable vNIC signal on 2026-09-14 (`operability`), with no genuine
`DOWN` yet observed in production. Intersight vNICs usually report `OperState`
empty. **HPE OneView reports `UNKNOWN` unconditionally** — `portMap` carries no
link state at all, so nothing is being lost in translation and none can be
synthesised (docs/hpe-collectors.md). A caller requiring strictly-`UP` members
can therefore never select an HPE server. That is a data gap, not an API one;
exposing the field lets the caller see it and fail explicitly rather than
silently never choosing those servers.

### Related, and deliberately still open

**Physical-port distinctness is not expressible for every vendor.** A bond needs
two *ports*, not two interfaces: two NPAR partitions of one port are two MACs on
one wire. `dell_port_nics` already reduces `network.interfaces` to one entry per
physical port and rewrites `location` to `controller/port/partition`, and
OneView reads only `physicalPorts`. But `location` is `None` for every Cisco and
HPE server, and Cisco's interfaces are vNICs rather than ports at all. A caller
can group by `location` where it parses and fall back to `name`; nothing better
exists in the stored data today.

**`identity.nic_macs` is not the same set as `network.interfaces[].mac`.** For
Dell, `dell_port_nics` reduces the interfaces and deliberately leaves the MAC
list whole (`# nic_macs stays whole on purpose`), so a 4-port NPAR'd card
reports 4 interfaces and 16 MACs. A caller selecting bond members must read
`interfaces`, not `nic_macs`. `?min_nic_macs=` filters on `nic_macs` because it
is a cheap floor, not because it is the right list to select from.

**Still no reservation.** Decision 3's "no reservation/lock" stands, and
`install-server` works around it in two places: its workflow id keys on the
CANDIDATE POOL (`^ocp-<infraEnv>`), which serialises concurrent draws from one
pool, and it skips any candidate that already has a BareMetalHost, which covers
the sequential case an id cannot — a second run started before any cluster has
reported the node, while this endpoint still calls that machine unclaimed.
Installing several servers from one pool concurrently needs a real short-TTL
reservation here; that is its own ADR.

## Update (2026-09-26): `min_nic_macs=0` really does mean "impose nothing"

The 2026-09-25 update above said `?min_nic_macs=` defaults to `0` so that
existing callers are unaffected. The first implementation did not honour that.
`nic_mac_filters(0)` correctly returned `{}`, but `server_still_qualifies`
applied its `"identity.nic_macs" not in unread_fields` clause unconditionally —
so the Mongo draw admitted a server whose NIC read had failed and the
post-recheck predicate then rejected it.

Two consequences, neither visible in a unit test that exercised only one side:

* **The default narrowed.** A server whose NIC MACs were unread stopped being
  returned at all, which is exactly the behaviour change the opt-in default was
  chosen to avoid.
* **Every draw wasted a replacement round.** `_fill_from_tiers` re-drew and
  re-discarded the same servers until `_MAX_REPLACEMENT_ROUNDS_PER_TIER` was
  spent, then returned short.

Both clauses are now gated on `min_nic_macs >= 1`, so the predicate admits
exactly what the filters drew. The invariant worth stating plainly: **the Mongo
filter and the post-recheck predicate must agree clause for clause**, because a
candidate the query returns and the predicate rejects is drawn and discarded on
every round. `test_the_default_draw_and_this_predicate_admit_the_same_servers`
pins it.

### What the gate is worth, measured on a real fleet

Against the 200-server fleet in the `cluster-2qsc5` sandbox, `?pattern=^ocp-`:

| Query | Returned |
|---|---|
| `health=HEALTHY` | 4 |
| `health=HEALTHY&min_nic_macs=2` | **1** |

Three of the four servers that read `HEALTHY` and `AVAILABLE` carry
`identity.nic_macs` in `unread_fields` and have **zero** MACs and **zero**
interfaces. Their NIC read failed, so their network category has no data, its
policies are skipped, and — since `UNKNOWN` ranks below `HEALTHY` and the
rollup is `max()` (ADR-0027) — the failure never reaches the overall verdict.

So 75% of what this endpoint calls healthy and assignable cannot be installed
at all, and the health tiers cannot express that. That is the measurement
behind `?min_nic_macs=`: it is not a refinement of the health filter, it is the
only gate that speaks to installability.
