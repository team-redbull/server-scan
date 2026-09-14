# Cisco collectors — verified implementation facts

This is the technical reference behind
`app.infrastructure.providers.ucs_common`,
`app.infrastructure.providers.ucs_manager`,
`app.infrastructure.providers.ucs_central` and
`app.infrastructure.providers.intersight`. Every docstring in those
modules points here rather than carrying its own explanation, so the
`##` headings below are load-bearing: renaming one breaks the
cross-references in several source files.

It is for whoever is about to change a Cisco collector. Almost nothing
here is inferrable from the code — most of it was established by reading
installed SDK source, and several items cost a live UCS Platform Emulator
run to learn. Each fact is recorded with its provenance, because a fact
without a source becomes folklore nobody dares change.

**Relationship to the ADRs.** `docs/adr/0009-ucs-manager-collector.md` is
the UCS Manager collector's decision record and holds its UCSPE
validation; `docs/adr/0014-ucs-central-multi-domain-collector.md` is the
UCS Central one; `docs/adr/0017-intersight-collector.md` is Intersight's. Those are dated decisions and are not maintained — they
record what was decided and why, at a moment. **This document describes
current behaviour and must be updated when the code changes.** Where a
fact is already recorded in an ADR it is cited here in one line rather
than restated, so the ADR stays the single source for it.

Extracted from the code comments on 2026-08-18, when this repo moved
justification out of inline comments and into documentation (see
`CLAUDE.md`, standing convention 8).

## Shared object model and DN joins

`ucsmsdk` and `ucscsdk` describe the same object model, which is why
`ucs_common` is shared rather than duplicated per provider. Property
parity was verified attribute-by-attribute against both installed
packages — ADR-0014's Evidence section lists the exact attributes
checked. Every attribute the mapping reads was confirmed against the
*installed* `ucsmsdk==0.9.27` generated MO source
(`ucsmsdk/mometa/**/*.py`'s `prop_meta` dicts), not against
documentation.

Only the DN *root* differs: `sys/chassis-1/blade-1` under `ucsmsdk`,
`compute/sys-1009/chassis-1/blade-1` under `ucscsdk`. Every function in
`ucs_common` works on relative structure — ancestry, prefix containment —
and never on an absolute root, which is what lets both SDKs share them
unchanged.

`computeBlade` and `computeRackUnit` carry the same relevant property
set, so one mapping function handles both.

**Why shared and not copied.** The grouping and BMC-selection rules were
each wrong in a way that only a live UCS Platform Emulator exposed
(ADR-0009's validation sections). Two copies guarantees the next such fix
lands in only one of them.

### Everything is a domain-wide query joined client-side

Every lookup either collector makes is a domain-wide `query_classid`
joined client-side by distinguished name, never a per-server
`query_children`. **This is a correctness requirement before it is a
performance one.**

`mgmtIf` and `adaptorHostEthIf` are *grandchildren* of a compute unit,
not children, so a `configResolveChildren` scoped to a blade's DN and
filtered to either class matches nothing at all. ADR-0009's "Confirmed on
real hardware" section records the live proof — 0 objects returned
against a blade DN, 6 found by the domain-wide join. The supporting
metadata, confirmed against the installed `ucsmsdk==0.9.27` MO metadata:

```
adaptorHostEthIf  parents=['adaptorUnit']                        rn=host-eth-[id]
adaptorUnit       parents=['computeBlade','computeRackUnit',...]  rn=adaptor-[id]
mgmtIf            parents=['adaptorHostEthIf','mgmtController']   rn=if-[id]
mgmtController    parents=[...,'computeBlade','computeRackUnit']  rn=mgmt
```

so the real DNs are `sys/chassis-1/blade-1/adaptor-1/host-eth-1` and
`sys/chassis-1/blade-1/mgmt/if-1` — two levels down.

**`hierarchy=True` does not fix this.** It does not widen the class
filter's depth; it only asks the server to attach each *matched* object's
subtree, which `ucsmsdk.ucscoreutils.extract_molist_from_method_response`
then flattens with no class filter at all — so a match would return
foreign MO classes mixed into the list. ADR-0009 confirms
`hierarchy=False` returns 0 too: the depth was the problem, not the flag.

**Cisco's own SDK does the same thing.** Its blade -> `mgmtIf` lookup in
`ucsmsdk/utils/ucskvmlaunch.py` uses `configScope`, not
`configResolveChildren`, and `ucsmsdk/utils/inventory.py` collects
adapters with a domain-wide `query_classid`.

**The DN join is exact, not heuristic.** `ucsmo.py` builds every MO's
`dn` as `parent_dn + "/" + rn`, so a descendant's DN always starts with
its owning server's DN followed by a separator.

### `presence` and `is_equipped`

The full enum, from `ComputeBladeConsts`/`ComputeRackUnitConsts` in
either SDK: `empty`, `equipped`, `equipped-deprecated` (`ucsmsdk` only),
`equipped-identity-unestablishable`, `equipped-not-primary`,
`equipped-slave`, `equipped-unsupported`,
`equipped-with-malformed-fru`, `inaccessible`, `mismatch`,
`mismatch-identity-unestablishable`, `mismatch-slave`, `missing`,
`missing-slave`, `unauthorized`, `unknown`.

Every `equipped*` variant is a physically-present server and **no
non-equipped value shares the prefix**, which is what makes the
`startswith("equipped")` test exact rather than a heuristic. This
listing exists nowhere else; the ADRs do not enumerate it.

`equipped-slave` and `equipped-not-primary` are excluded: they are the
secondary half of a multi-node server (a B460's slave blade),
physically present but not independently addressable, and both SDKs
report the logical server under the primary's DN — so ingesting them
double-counts one machine as two. ADR-0009 records the exclusion
decision.

The same enum family is reused on `processorUnit` and
`storageLocalDisk`, so an empty socket or drive bay reporting
`presence="empty"` is skipped by the same test.

### `group_by_owning_server_dn`

A domain-wide `query_classid` returns instances owned by chassis, fabric
interconnects and IO modules as well as servers — `mgmtIf` alone hangs
off about a dozen parent classes — so anything not under one of the
given server DNs is dropped rather than mis-attributed. Objects owned by
something other than a server, such as chassis-level shared storage, are
dropped by the join for free rather than needing their own filter.

The implementation walks **each MO's own ancestor DNs, nearest first**,
rather than testing every server DN as a prefix. Three properties fall
out of that choice, and only the first is in an ADR:

1. Segment-boundary exactness: `sys/rack-unit-1` cannot claim
   `sys/rack-unit-10`'s descendants (ADR-0009).
2. **Nearest-ancestor-wins**: a nested `computeServerUnit` keeps its own
   descendants instead of donating them to the enclosing server.
3. Cost is O(MOs × DN depth), not O(MOs × servers), and DN depth is a
   handful of segments regardless of fleet size.

## BMC and management interface selection

ADR-0009's "Wrong, and fixed as a result" section records this in full:
filtering `mgmtIf` on `access == "out-of-band"` selected **nothing** on a
real domain. Verified against UCSPE 4.2, a blade's own management
interfaces report `access="unspecified"` with `subject="blade"`, and the
only two `out-of-band` interfaces in the entire domain belong to the
fabric interconnects (`subject="switch"`), which are under no server's DN
at all.

Selection is therefore **by position in the tree**: a compute unit owns
exactly one management controller at `{server_dn}/mgmt`, and the
interfaces beneath it are the CIMC's. `out-of-band` is still preferred
when present, for domains that do set it.

**The `_NON_BMC_ACCESS` exclusion set** — `in-band`, `internal`,
`virtual` — is excluded regardless of tree position. These are
`MgmtIfConsts.ACCESS_*` values:

- `in-band` rides the data path rather than the CIMC, so its address is
  not the BMC address even though it hangs off the same controller.
- `internal` is adapter-internal plumbing: the other `mgmtIf`s under a
  server hang off its adapters at `{server_dn}/adaptor-N/mgmt/...`.
- `virtual` is likewise never a physical BMC address.

Everything else is accepted, including the `unspecified` a real blade
reports.

**Address form.** `_bmc_address` emits an `ipmi://{host}:623` URI,
matching the form `app.domain.value_objects.bmc_address.parse_bmc_address`
already recognizes for Cisco — a UCS-managed CIMC's out-of-band interface
is reachable the same way a standalone one is. `0.0.0.0` and `none` are
unset sentinels and yield `None`, not an address, from either source
below.

**The address itself has three possible sources, tried in this order,
and `mgmtIf.ext_ip` is the least reliable one.** Discovered against real
hardware in two passes (2026-08-18), not UCSPE — the emulator never got
a service profile past `config-failure`, so it never exercised any of
this. A fully associated profile assigns the CIMC's out-of-band address
through the service profile's management IP address policy, which UCS
Manager records as a `vnicIpV4PooledAddr` (pool-assigned) or
`vnicIpV4StaticAddr` (static) MO. Both classes list two different valid
parents in the installed `ucsmsdk`'s `mo_meta.parents`: the service
profile's own `lsServer` DN, and the physical compute unit's
`mgmtController` (a sibling of `mgmtIf`, at `{server_dn}/mgmt`).

The **first pass** assumed the compute-unit location, by analogy with
`bmc_interface`'s DN-prefix selection of `mgmtIf` — reasonable from the
schema alone, but empirically wrong: on the domain that surfaced this,
querying `vnicIpV4PooledAddr`/`vnicIpV4StaticAddr` domain-wide and
joining by compute-unit DN prefix found nothing, and the BMC address
still came back missing after that fix shipped. The **second pass**
found the actually-populated location: a **direct child of the service
profile's own DN** (`{profile_dn}/ipv4-pooled-addr`), matching what the
sibling project team-redbull/ServerScanner does in production — its
`CiscoStrategy` queries `VnicIpV4PooledAddr` as a child of the service
profile MO it already has in hand, never of the physical compute unit.

`ucs_common.management_ip_by_parent_dn` indexes every
`vnicIpV4PooledAddr`/`vnicIpV4StaticAddr` carrying a real address by its
immediate parent DN, keeping both a profile-scoped and a
compute-unit-scoped entry possible in the same dict — one domain-wide
pass covers both without a second per-server query. Per server,
`mapping._management_ip_addr` looks up the assigned profile's DN first
and the compute unit's `{server_dn}/mgmt` DN second, so a deployment
that does populate the compute-unit-scoped MO (schema-valid, just not
observed) is still covered as a fallback rather than silently dropped.
`_bmc_address` then tries that resolved MO's `addr` first and falls back
to `mgmt_if.ext_ip` only when neither DN resolved one. On the domain
observed, `mgmtIf.ext_ip` came back an unset sentinel while the
profile-scoped pooled address carried the real one; `bmc_mac` was
unaffected throughout — it always came from `mgmtIf.mac`, populated in
every run. Only the *address* half of the pair was ever missing, and
only because it was being looked for in the wrong of two schema-valid
places.

## Service profiles and server names

A UCS server's name comes from its **service profile**, not
`computeBlade.name`, which is an optional user label empty in practice —
verified against UCSPE 4.2, where a blade with a profile bound to it
still reported `name=""`.

ADR-0009 records the consequence: falling back to the DN names every
server `sys/chassis-3/blade-1`, a location rather than an identity, which
carries neither the site token (`app.domain.value_objects.site`) nor the
installation-type convention the classification rules match on. A
UCS-sourced fleet would be permanently unsited and unclassified. The DN
remains the last-resort name, and stays the `external_id` regardless —
identity and display name are different jobs.

### The profile's own DN doubles as its org path

`ProviderServer.profile_dn` carries the assigned `lsServer`'s own DN
verbatim (e.g. `org-root/org-five/ls-worker-01`), populated in
`compute_unit_to_provider_server` alongside the template fields. UCS
Manager organizations nest as DN segments, so this single string is both
"which service profile" and "which org tree it lives in" — there is no
separate org field to carry. It is distinct from `profile_template_*`:
that pair names the reusable template a profile was created from, this
names the one profile instance bound to this specific server. Currently
surfaced only in `tools/run_collector.py`'s `--dry-run` print, not
threaded into `IngestService`/`Server` — adding persistence is a
separate decision (`Server` model, MongoDB, API, UI) that hasn't been
asked for yet.

### Profiles and templates share one class

`lsServer` carries both real service profiles and the templates they
derive from, distinguished **only** by the `type` attribute. There is no
separate `lsServiceProfileTemplate` class in either model — confirmed two
ways against the installed packages: both SDKs'
`find_class_id_in_mo_meta_ignore_case` return `None` for that name, and
`LsServer.prop_meta["type"]` restricts to exactly `initial-template`,
`updating-template` and the normal profile type in both. Querying the
non-existent class name aborts the whole run (ADR-0009). One query
returns both kinds; partitioning happens in
`ucs_common.partition_profiles`.

### Template resolution is by DN, not by name

A bare template name is unique only *within* one org — two orgs can each
own a `worker-template` — so the template's full DN (e.g.
`org-root/ls-template-mytemplate`) is the stable identifier.
`partition_profiles` returns template DNs keyed by bare name, and that
mapping is therefore lossy across orgs by construction.

`oper_src_templ_name` is UCS Manager's own resolved absolute DN for the
source template, following the `oper*` convention across `lsServer`'s
policy-name properties, so it is preferred: it is collision-proof across
orgs where the by-name lookup is not. Resolution order is
`oper_src_templ_name` -> by-name lookup -> bare name, the last two
covering a profile whose template was since deleted or renamed.

### `LsServer.domain` — the domain link on UCS Central

The installed `ucscsdk` carries four domain-ish attributes on
`LsServer`: `domain`, `domain_dn`, `domain_group` and `domain_group_dn`.
**`domain` is the one that names the UCS Manager a profile lives on** —
it is the value a UCS Central client opens its second, per-domain session
against, which is the same hop `app.domain.models.manager`'s docstring
records as the one real hierarchy Cisco UCS has. Worth keeping written
down because picking `domain_group` instead is a plausible and silent
mistake.

Profiles with an empty `domain` are ignored for pruning purposes: an
unassociated or domain-less profile says nothing about which domains are
worth contacting.

## Adapter interfaces, MACs and fabric attachments

ADR-0009 records the headline finding: `adaptorExtEthIf` (physical
adapter port, present on every discovered server) and `adaptorHostEthIf`
(logical vNIC, exists only once a service profile is associated) were
strictly complementary on UCSPE 4.2 — of 14 servers, **12 had only
ext-eth, 2 had only host-eth, none had both**. Querying either class
alone leaves most of the fleet with no network data at all. Both are
collected, and fabric attachments use both together.

**On real, fully-associated hardware both classes are present at once**
for the same physical port, unlike UCSPE — a physical uplink and the
vNIC UCS Manager virtualizes on top of it can both report the same
`fabric`, so `len(ProviderServer.attachments)` overcounts physical
uplinks if the two aren't told apart. `ProviderAttachment.interface_kind`
is `"PHYSICAL"` for an `adaptorExtEthIf` attachment, `"VNIC"` for an
`adaptorHostEthIf` one — the two are always built as separate
`_attachments()` calls and concatenated, never merged into one pass, so
this label is exact rather than inferred. **To answer "what is this
server physically cabled to," read the `PHYSICAL` rows**; the `VNIC`
rows describe the OS-facing logical carve-out pinned to one fabric side,
which matters for troubleshooting guest networking but not for verifying
the wire.

### `fabric_name` is the domain, not the switch

UCS Manager exposes no per-FI hostname — the only name-like thing a
domain has is `topSystem.name`, its own cluster name, identical for both
FI-A and FI-B of one domain. `fabric` (`"A"`/`"B"`) already tells the two
sides apart within one domain; `fabric_name` names the domain itself,
which matters the moment a fleet has more than one — otherwise every
domain's `fabric A` reads identically on the attachment line, and only
`fabric_serial` (a real but opaque FI serial number) disambiguates them.
Confirmed live 2026-09-07 against a real air-gapped domain, both via
`tools.verify_ucs_central`'s section 6 preview and independently by the
user running `ucsmsdk` directly. `topSystem` is a domain singleton —
`ucs_manager/provider.py` queries it once per domain, the same session
already open for everything else, not once per server. See ADR-0009's
"Update (2026-09-07): `fabric_name` built and confirmed live".

### Which MAC the OS actually sees

The two classes are *not* interchangeable for this, and the ADRs do not
cover it. `nic_macs` prefers the vNIC's MAC and falls back to the
physical port's only when a server has no vNIC — unassociated, or an
older-generation adapter with no vNIC abstraction — so a cabled server
never reports zero NICs merely for lacking a service profile.

A Cisco VIC presents virtual interfaces to the OS rather than exposing
its physical uplink port, so the physical port's burned-in MAC faces the
fabric interconnect and is never visible inside the OS.

> **Provenance conflict, unresolved — do not treat this as settled.**
> The commit that introduced this preference (`de46ee3`, "feat: prefer
> vNIC MACs over physical-port MACs") states in its message that it was
> "confirmed against a live UCS Central run, where a fully-associated
> server reported one physical and one logical MAC per adapter port, both
> real Cisco OUIs, and only the vNIC MAC matched the OS-visible NICs".
> **No such run is recorded in any ADR**, and ADR-0014's Status still
> reads "Not yet validated against a live UCS Central". The 2026-08-17
> ADR updates were code and documentation changes, not validation runs.
> Either the commit message describes a run nobody wrote up, or it
> overstates its evidence. Resolve this before relying on the claim, and
> record the answer in ADR-0014.

MACs reported as `not applicable` or `derived` are UCS placeholders and
are skipped.

### The per-interface view, and its OS-level names (2026-09-10)

`nic_macs` (the flat, correlation-key MAC list) is not the whole story
any more — `..ucs_manager.mapping._nics`/`..intersight.mapping._nics`
build one `ProviderNic` per counted MAC, the same vNIC-preferred/
physical-fallback rule as `nic_macs` itself, so the two always agree in
count and order. `name` is `adaptorHostEthIf.name`/`.id` (UCS Manager) or
`HostEthInterface.Name`/`.ExtEthInterfaceId` (Intersight) — a Cisco vNIC
carries no FQDD, so `location` stays `None` and `speed_mbps` stays `None`
(neither API reports a numeric interface speed). `link_state` reuses
`_oper_state`/`normalize_oper_state`, so it reads `UNKNOWN` on the same
~99.75% of vNICs the fabric-attachment `oper_state` field already does
(ADR-0009's dated update) — this is a real limitation of what the field
measures, not a mapping gap.

**The OS-level name is a fixed, operator-confirmed rule, not derived per
model like Dell's.** The operator confirmed against real UCS hardware
(`ip link` on a booted host) that a service profile's own vNICs start at
`eno5` and count up — onboard/shared management interfaces already claim
`eno1`-`eno4`. `app.domain.value_objects.nic_names.cisco_eno_names`
implements exactly that: the Nth interface in `NetworkInfo.interfaces`
(in order) is named `eno{4+N}`, computed positionally at the API-response
layer (`ServerDetail.from_server`, gated on `identity.vendor ==
Vendor.CISCO`) rather than stored — same shape as Dell's
`NicNameCatalog`, but a hardcoded fleet-wide rule instead of configured,
per-model operator knowledge, since this one convention does hold across
every Cisco model in this estate.

### Operational state mapping

ADR-0009 records that passing UCS's own vocabulary (`operable`,
`admin-down`) through untouched left every fabric path counted as neither
up nor down — a server with four attachments stored `fabric_paths_up: 0,
fabric_paths_down: 0`, silently disabling the connectivity health signal
for every UCS server.

`admin-down` maps to `DISABLED`, not `DOWN`: it is the normal state of an
adapter port on a server with no service profile, and
`compute_connectivity_facts` counts neither, so an unassociated server
does not masquerade as a fault.

### Fabric Interconnect identity

`networkElement` is queried domain-wide — exactly two results in
practice, the redundant FI pair — and joined onto every attachment by
its bare `id` (`"A"`/`"B"`), the same value `switch_id` already carries.
It supplies `fabric_model` and `fabric_serial`, the two identifying
facts UCS Manager's schema actually exposes per Fabric Interconnect.

**`fabric_name` and `fabric_id` remain deliberately unpopulated.**
`ucsmsdk`'s `NetworkElement` has no distinct configured-hostname
property — confirmed from the installed package's `prop_meta`, which
lists `model`/`serial`/`oob_if_ip`/etc. but nothing name-shaped beyond
the `id` letter already captured as `fabric`. UCS's own architecture is
why: a domain's two Fabric Interconnects share one cluster identity
(`topSystem.name`/`topSystem.address`, the management VIP), and each
physical FI otherwise has only its own out-of-band console IP
(`NetworkElement.oob_if_ip`, not currently collected) — there is no
separate per-FI DNS-style hostname in UCS Manager's own data model to
read. A synthetic label (e.g. `f"{domain_name}-{switch_id}"`) was
considered and deliberately not written into `fabric_name`: that field
already flows through `IngestService` into the persisted `Server`
document and API/UI, so inventing a value UCS Manager never configured
would read as more authoritative than it is. Revisit if an operator's
naming convention makes that derivation reliably correct for their
fleet.

`fabric_port` comes from `peer_dn`, which only physical ports
(`adaptorExtEthIf`) carry — logical vNICs have no fabric-side peer. An
interface whose `switch_id` is absent or `NONE` produces no attachment at
all.

`ProviderAttachment.provider` records **which collector observed** the
attachment, not which product owns the fabric: a UCS Central run reports
`UCS_CENTRAL` for hardware still fronted by a domain's own fabric
interconnects.

### `operability` — a second vNIC signal ADR-0009 did not check (settled 2026-09-14)

ADR-0009 found `AdaptorHostEthIf.oper_state` (vNICs) UNKNOWN on 99.75% of
a live fleet and concluded "no evidence a better per-vNIC connectivity
signal exists in this MO at all." Operator report plus a re-check of the
installed `ucsmsdk`'s `AdaptorHostEthIf.py` shows that conclusion missed
one thing: the MO also declares `operability`, a **separate** property
from `oper_state` carrying the identical operable/inoperable/... enum —
present since UCS Manager 1.0(1e), so not a newer-firmware artifact — and
it is `operability`, not `oper_state`, that UCS Manager's own GUI labels
"Operability" under Equipment > NICs, which the operator sees reading
`Operable` there while this collector's `link_state` reads `UNKNOWN` for
the same vNIC.

**Confirmed live 2026-09-14** (`tools/verify_ucs_central.py` section 8,
against 12583 vNICs on the operator's fleet): `operability` reads
`operable` on **100%** of them, while 12551 of those same 12583 (99.75%,
matching ADR-0009's own count) read `oper_state=unknown`. `operability`
is therefore a real, populated signal where `oper_state` is not. No
vNIC in this fleet has yet read anything but `operable`, so whether a
real fault reads `inoperable` the way health policy assumes stays
unconfirmed — but `_OPER_STATE_MAP` falls back to `UNKNOWN` for any
value it does not recognize, never a false `DOWN`, so an exotic
transient `operability` value (`config`, `discovery`, ...) cannot
manufacture a false CRITICAL either way. Judged worth wiring in on that
basis rather than waiting for a live negative case.

**Built:** `_vnic_link_state` reads `operability` and is now what
`_nics`'s vNIC branch and a `VNIC`-kind `_attachments` call both use for
`link_state`/`oper_state`; a physical port (`_oper_state`, `oper_state`)
is unchanged. This reopens the exact case ADR-0027 fixed by gating
`network.all_links_down`/`single_link_up` on `network.links_known_count`
— that gate stays, but its denominator is no longer permanently ~0 for
UCS servers (see ADR-0027's 2026-09-14 update). `fake/generator.py`'s
`_nics_for` was updated to match (convention 10): `UCS_CENTRAL` now
draws from the same `_link_states` fault injection every other
link-state-reporting collector uses; `INTERSIGHT` is untouched, still
hardcoded `UNKNOWN` — no equivalent field has been researched for it.

## CPU, memory and storage

ADR-0009's 2026-08-16 update covers the class hierarchy (`computeBlade`
-> `computeBoard` -> `processorUnit`, and -> `storageController` ->
`storageLocalDisk`), the ancestor-walk join, and the
`MediaType`/`HealthSeverity` mappings.

`ComputeBoard`, `ProcessorUnit`, `StorageController` and
`StorageLocalDisk` were also confirmed to exist as real classes in
`ucscsdk`, property-identical to `ucsmsdk` (ADR-0014's 2026-08-16
update). That confirmation is now historical: the collector reads this
data from each domain's UCS Manager, never from Central's replica.

### Unit assumptions — both unproven

**`total_memory` is assumed to be MB, and this remains unproven.** The
SDK cannot settle it: the package is code-generated from the MIT schema
and carries no unit metadata for any property —
`prop_meta["total_memory"]` is a bare `uint` with no unit annotation, doc
string or range. The assumption rests on UCS Manager's own GUI labelling
the column "Total Memory (MB)". ADR-0009's "Still not settled" records
why UCSPE did not resolve it: it reports `49152` for all 14 servers
regardless of model, and contradicts itself elsewhere. **Only real
hardware will settle this.**

**`storageLocalDisk.size` is assumed to be MB on the same basis, also
unproven.** The one piece of independent corroboration is weak: a sibling
project's from-scratch Cisco collector
(`team-redbull/ServerScanner`) made the identical MB assumption for this
exact field. Still unverified until a disk of known size is read back.

`size == "not-applicable"` is a documented sentinel
(`StorageLocalDiskConsts.SIZE_NOT_APPLICABLE`) and means unknown
capacity, not zero. A disk whose capacity cannot be read still
contributes a drive entry — model, serial and health are worth reporting
— with `capacity_bytes=None`, and adds nothing to the storage total
rather than counting as zero bytes.

### CPU model

`cpu_model` comes from the **first equipped** `processorUnit`. UCS
reports one per socket and a real multi-socket server is expected to be
symmetric, so the first equipped socket represents the server rather than
being an arbitrary pick.

**Caveat, still open.** `cpu_model` and the per-drive storage detail were
mapped from SDK evidence alone (ADR-0009's 2026-08-16 update) and had not
been exercised against a live domain when that update was written. That
is still true: no live UCS Central validation has happened, so nothing
has since confirmed these fields against real hardware.

## Power supplies (PSUs)

**Added 2026-09-02**, at the user's request, and the first UCS field this
collector reports that the platform's domain model already had a slot
for but no provider had ever filled — `app.domain.models.hardware.Psu`/
`Power` and the health engine's `power.psu_count`/`power.failed_psu_count`
metrics predate this collector (see ADR-0017's Intersight PSU section for
the fuller history and the `power.failed_psu_count` comparison bug that
fix caught and corrected — vendor-neutral, so it applies here unchanged).

**Current state, 2026-09-04:** that gap is fully closed and this is no
longer the only source. Intersight landed PSUs days before this, and
`..redfish.mapping.psus_from_supplies` then covered Dell and every
standalone BMC, with OneView covering HPE (ADR-0022). Every collector
reports PSUs now; what stays specific to Cisco is the blade limitation
below.

`equipmentPsu` (confirmed against the installed `ucsmsdk==0.9.27` source,
`mometa/equipment/EquipmentPsu.py`) declares five parent classes:
`computeRackUnit`, `equipmentChassis`, `equipmentFex`,
`equipmentRackEnclosure`, `networkElement` — **no relationship to an
individual blade at all.**

- **Rack-mount servers**: a PSU's DN (`sys/rack-unit-3/psu-1`) is a
  direct child of the rack unit's own DN, so `ucs_common
  .group_by_owning_server_dn`'s existing ancestor-walk join resolves it
  the same way it does every other grandchild MO. No new join logic was
  needed.
- **Blade servers**: a PSU's DN (`sys/chassis-1/psu-1`) is a child of the
  **chassis**, a *sibling* of the blade (`sys/chassis-1/blade-1`), not an
  ancestor of it. The same ancestor-walk join therefore finds no match
  and silently drops it — **a blade server reports no PSUs through this
  collector.** This is a real capability gap, not a bug: nothing about
  `equipmentPsu` lets a query attribute a shared chassis PSU to one
  specific blade among several. Reporting it once per chassis, shared
  across every blade in it, would be a materially different feature —
  not implemented.

### Fields collected, and why only these

| `Psu` field | From `equipmentPsu` | Why |
|---|---|---|
| `id` | `id` (bay number, `"1"`–`"6"` per the schema's own range) | Identity |
| `model` | `model` | Identity |
| `serial` | `serial` | Identity |
| `health` | `oper_state`, via the shared `normalize_oper_state` helper (UP/DOWN/DISABLED/UNKNOWN) | Same field and vocabulary Intersight's `psu()` already uses for the identical purpose — keeping `Psu.health` mean the same thing regardless of vendor is what lets one health-policy rule work correctly no matter which collector produced the data. |
| `capacity_watts` | `psu_wattage` | A real `uint` (`prop_meta` range `0-20000`), no unit ambiguity — but a literal `0` is folded to `None` too, confirmed live to mean "unpopulated on this model", not "rated for no power" (see below). |

**Only equipped slots are reported at all**, via the existing
`ucs_common.is_equipped()` helper (checking `presence`, the same field
already used to skip an unpopulated CPU socket or disk bay). An empty
PSU bay is a different claim from a PSU that failed — a server
provisioned with fewer PSUs than physical bays would otherwise show a
permanent "failed" PSU it never had, corrupting
`power.failed_psu_count` for exactly the servers that setting exists to
protect.

**Deliberately not collected**, because nothing currently consumes them
— `vendor`, `revision`, `part_number`, `psu_type`, `psu_input_src`,
`psu_firmware_version`, `thermal`, `voltage`, `perf`. All real fields on
`equipmentPsu`; none has a health metric, a UI field, or any other
reader yet. Add one only when something is built that actually needs
it — see the GPU-telemetry and CPU-model sections above for the same
principle applied earlier in this file.

### `oper_power` — a second signal, collected but not yet trusted

`equipmentPsu` carries a **second, separate** state field: `power`
(`degraded`/`error`/`failed`/`off`/`ok`/`online`/... — a materially
different enum from `oper_state`'s, and specifically about whether the
PSU is delivering power, not the MO's general operational state). Unlike
every other field above, this one is collected into the provider's dict
under `oper_power` **without being folded into `Psu.health`** — it is not
part of the `Psu` domain model, is never persisted, and is surfaced only
in `tools/run_collector.py --dry-run`'s PSU line (`... health=UP
power=ok`), always `—` for a provider that doesn't report it
(Intersight has no equivalent field).

**Why both, deliberately unreduced to one:** nothing here settles whether
`oper_state` or `power` tracks a real PSU failure more reliably — that
needs a live domain with an actual bad PSU, and no such data exists yet.
Reducing to one signal now would be a guess dressed as a decision. Once a
live run shows the two agreeing or disagreeing on a real failure, fold
the answer into `_psu()` and delete this section's uncertainty — the
same "settle it live" pattern already used for `TotalMemory`'s unit
(ADR-0017) and the `ComputeBoard` join gap.

### `power_watts` — real-time input power, a separate MO from `psu_wattage` (settled 2026-09-14)

Operator report: the UI showed `0W` for rack-mount PSUs whose own model
string names a wattage (e.g. `UCSC-PSU1-770W`), while UCS Manager's GUI
shows a real number under Equipment > Rack-Mount Server > PSUs > PSU N >
Statistics > "Rack Unit Power State" > Input Power (W). Checking the
installed `ucsmsdk`'s `mometa/equipment/` explains why: that GUI value is
`EquipmentRackUnitPsuStats.input_power` (a `float`, child DN
`.../psu-N/rackunit-power-stats`, fed by the stats poller), a completely
separate MO from `equipmentPsu` and its `psu_wattage` (a `uint`, only
present since UCS Manager 3.2(2c)) that `capacity_watts` already read
correctly per its own contract — the two answer different questions
(rated capacity vs. current draw), so `power_watts` was added *alongside*
`capacity_watts`, never in place of it.

**Confirmed live 2026-09-14** in three steps, all `tools/verify_ucs_central.py`:
section 7 found `psu_wattage` reading `0` on 4770 of 5582 equipped
rack-unit PSUs fleet-wide (the rest read `2500`/`1050`, real values — the
property is not universally broken, just unpopulated on a majority of
this fleet's PSU models) while `equipmentRackUnitPsuStats` returned
**zero** MOs through UCS Central for any of them; section 9 then queried
one domain's own UCS Manager directly, bypassing Central, and got **42
`equipmentPsu` and 2 `equipmentRackUnitPsuStats`** back — confirming
Central's own API does not proxy statistics-class objects from member
domains at all (equipment/config inventory only), independent of whether
a domain's stats-collection policy is enabled. The 2-of-42 ratio is
itself informative: real-time PSU stats look to be populated per-rack-unit,
not fleet-wide, so `power_watts` will be `None` on most PSUs even once
wired in — expected, not a defect, and exactly what `_psu_input_power`'s
`None`-on-absent-stat contract already assumes.

**Built:** `UcsManagerProvider._list_servers`/`get_one` now also query
`equipmentRackUnitPsuStats` (14th domain-wide query,
`test_scales_query_count_independently_of_fleet_size` bumped 13 -> 14);
`_psus` joins each stat to its owning PSU by DN
(`.../psu-N/rackunit-power-stats` -> `.../psu-N`), and `_psu_input_power`
parses `input_power` the same string-to-number way `_psu_wattage` parses
`psu_wattage`. `Psu.power_watts: float | None` is the new domain field;
Intersight, OneView and Redfish all set it explicitly to `None` with a
one-line pointer, matching `Gpu.power_watts`'s own per-vendor-capability
precedent — none of the three has been researched for an equivalent real-
time PSU draw property yet.

### `capacity_watts` reading 0 is `psu_wattage` being unpopulated on this model, not a real rating (2026-09-14)

Shipping `power_watts` surfaced the display this was masking: a PSU
whose `power_watts` now reads a real number (e.g. `248`) still showed
`0W rated` beside it, because `_psu_wattage` returned the literal `0`
`psu_wattage` reports on this fleet's majority of PSU models as if it
were a genuine reading. It cannot be — no equipped PSU is rated for zero
watts, and the model string itself always names a real wattage (e.g.
`UCSC-PSU1-770W`) `psu_wattage` simply never repeats. `_psu_wattage` now
folds a parsed `0` into `None` the same way an absent or unparseable
value already was, so the UI and `--dry-run` both stop showing a PSU
line's rated capacity at all when it isn't real data, rather than
showing a `0W` that reads as a hardware fact.

## GPUs (coprocessor cards vs. graphics cards)

**Added 2026-09-02**, at the user's request. `ucsmsdk` (confirmed
`0.9.27`, installed) declares **two** separate GPU-shaped MO families,
and picking the wrong one would either miss real GPUs or mislabel
unrelated hardware as one:

- `coprocessor.CoprocessorCard` — introduced UCS Manager 4.0(1a).
  Identity fields only (`model`/`vendor`/`serial`/`revision`/`pci_addr`/
  `pci_slot`/`device_id` family), `oper_state`/`power`/`thermal` status
  enums, **no `firmware_version`, no numeric telemetry field of any
  kind.**
- `graphics.GraphicsCard` (+ its child `graphics.GraphicsController`,
  one per GPU die on a multi-GPU card) — introduced UCS Manager 2.1(3a),
  materially extended over its lifetime. Same identity/status fields as
  `CoprocessorCard`, **plus** `firmware_version` (2.2(7b)) and
  `temperature` (4.0(2a)).

### Why `graphicsCard`, not `coprocessorCard`

Neither class's own docstrings say which one models a physical GPU —
the SDK doesn't editorialize. Settled from two independent, official
sources instead:

1. **Cisco's own UCS Manager System Monitoring Guide and server
   install guides** (e.g. the UCS C240 M7 and B200 M4 install guides,
   both fetched from cisco.com) document GPU inventory living at
   **Equipment → … → Inventory → GPUs**, populated once an NVIDIA GPU
   card is physically installed and the server rediscovered. Nothing in
   any of Cisco's own documentation ties `coprocessorCard` to GPU
   hardware at all — it isn't mentioned in GPU-related install or
   monitoring guides anywhere this was checked.
2. **`GraphicsCardConsts.MODE_COMPUTE = "compute"` /
   `MODE_GRAPHICS = "graphics"`** is the documented NVIDIA GRID/vGPU
   compute-vs-graphics operating-mode toggle — a concept with no reason
   to exist on any hardware that isn't a GPU. `CoprocessorCard` has no
   equivalent field.

Both are also confirmed present, byte-identical in field shape, on the
current `CiscoUcs/ucsmsdk` GitHub `master` — this isn't an artifact of
one pinned version. `ucscsdk` (UCS Central's own SDK) has **no**
`coprocessor` package at all, and no `graphics` class either at the
usual mirrored path — moot either way, since UCS Central never reads
per-server hardware from its own SDK; every field here comes from
`UcsManagerProvider`, which `UcsCentralProvider` wraps per domain (see
"UCS Central domain discovery and pruning" below). If real hardware
ever turns up a GPU visible under `coprocessorCard` but not
`graphicsCard`, that would be the live evidence to revisit this
decision — none exists yet.

### Fields collected

| `Gpu` field | From `graphicsCard` | Why |
|---|---|---|
| `vendor` | `vendor` | Identity |
| `model` | `model` | Identity |
| `serial` | `serial` | Identity |
| `pci_address` | `pci_addr` | Identity |
| `firmware_version` | `firmware_version` | Real field, unlike `coprocessorCard` |
| `health` | `oper_state`, via the shared `normalize_oper_state` helper | Same field/vocabulary every other OperState-sourced health value in this platform uses |
| `temperature_celsius` | `temperature` | See below — the one genuinely new kind of data this addition brings |
| `memory_bytes`, `memory_type`, `ecc_mode_enabled`, error counts, `power_watts` | — | No field for any of them exists on `graphicsCard` or `graphicsController`; `None` by construction, the same capability ceiling already confirmed twice for Intersight's `graphics.Card` |

Only equipped cards are reported, via `is_equipped()` — the same
presence check used everywhere else in this module.

**How a card joins to its server.** `graphicsCard`'s only parent is
`computeBoard`, a DN path segment directly under the server's own DN
(confirmed via `ComputeBoard`'s own `mo_meta`: `rn="board"`, parents
`computeBlade`/`computeRackUnit`/`computeServerUnit`), so a card's DN is
`.../blade-3/board/graphics-card-1` — the same pattern `processorUnit`
already uses (`.../board/cpu-1`). `group_by_owning_server_dn`'s
ancestor-walk therefore resolves a GPU's owning server with no new
logic, for blades and rack units alike — unlike PSUs, which have no such
path back to a blade at all. `graphicsController` (the per-die child)
carries no field this collector does not already read off
`graphicsCard`, checked against the installed SDK, and is not queried.

### `INVENTORY_GPU_MODELS` — filling the `memory_bytes` gap this API leaves

**Added 2026-09-02.** Neither `graphicsCard` here nor Intersight's
`graphics.Card` (`docs/adr/0017-intersight-collector.md`) reports GPU
memory size anywhere — confirmed against both SDKs' full field sets and
Cisco's own metrics API, per the table above. What both *do* report is
the card's PID (`model`, e.g. `P1001-200`), Cisco's stable per-SKU part
number.

`app.domain.value_objects.gpu_catalog.GpuCatalog` is that lookup.
`IngestService.gpu_catalog.enrich()` runs on every GPU a provider
reports — both Cisco collectors and the Redfish-sourced ones — before
the `Gpu` model is built: a recognized card gets its `model` replaced by
a friendly name and `memory_bytes` filled in; anything unrecognized, or
a value a future API version actually reports, passes through unchanged.

**Revised 2026-09-04 (`docs/adr/0021-built-in-gpu-catalog-with-model-matching.md`),
in two ways this section originally documented the opposite of.**

**It ships a default table.** `gpu_models.DEFAULT_GPU_MODELS` covers 30
NVIDIA and AMD datacenter cards, every VRAM figure sourced from an NVIDIA
or AMD datasheet or a Cisco UCS spec sheet (ADR-0021 lists the source per
family). An unconfigured deployment enriches from it — the earlier "unset
enriches nothing" behaviour is gone. `INVENTORY_GPU_MODELS`
(`PID:Friendly Name:VRAM_GB` triples, comma-separated — see
`.env.example`) is still read and now **overrides** the built-in table
rather than being the only source: a configured entry wins for the
identifiers it names, and every other built-in row survives. That keeps
the correction case the original "config, not code" argument was really
about, without making every estate retype NVIDIA's product line before it
can report GPU memory.

**It matches a model string as well as a PID.** A PID is Cisco-only;
Dell's iDRAC and HPE's iLO report no PID at all, only a model string
(`NVIDIA A100-PCIE-40GB`, `NVIDIA H100 80GB HBM3`). Both go through one
normalization — uppercase, drop leading vendor/brand words, strip every
separator — and are then compared for **equality**, never as a substring
or prefix. `A10` and `A100` differ by one character and by 3x the VRAM,
so a looser rule would report a confidently wrong number. Spellings that
survive normalization as different strings get an explicit alias on the
row. A bare model name is a match key only where the model shipped in one
capacity: a GPU reporting plain `NVIDIA A100` (40GB and 80GB both exist)
matches nothing and keeps `memory_bytes: None`.

### `temperature` — real telemetry, unlike everywhere else in this file

Every other Cisco-sourced status-shaped field in this codebase
(`oper_state`, `power`, `thermal`, `perf`, PSU's `power`, …) is one of a
fixed enum list — `MoPropertyMeta`'s constructor is handed a list of
valid strings. `GraphicsCard.temperature` is different: it validates
against a **decimal-number regex**
(`^([\-]?)([123]?[1234]?)([0-9]{0,36})(([.])([0-9]{1,10}))?$`) with a
`"not-applicable"` sentinel for unknown — the identical shape
`storageLocalDisk.size` uses for its own confirmed-numeric MB reading —
and it was added in 4.0(2a), a full point release after the MO class
itself (2.1(3a)), consistent with a real sensor reading being added to
an already-shipped identity-only object rather than being part of its
original shape.

**The unit is still unverified.** No docstring anywhere in this SDK
states it. Assumed Celsius — every other temperature reading this
platform collects (Redfish's GPU/CPU telemetry) is Celsius, and
`Gpu.temperature_celsius`'s own name already commits to it — but that
is this collector's assumption, following the same convention as
`TotalMemory`'s unit before ADR-0017 settled it live, not a documented
fact. `"not-applicable"` maps to `None`, mirroring
`storageLocalDisk.size`'s identical sentinel handling
(`_disk_capacity_bytes`). Settle by reading this field off a live
server with a GPU whose actual temperature is known.

## SDK behaviour, sessions and timeouts

### `ucsmsdk` (UCS Manager)

ADR-0009 covers the async-wrapper decision: `ucsmsdk` has no async
support, so every `login`/`logout`/`query_classid` is dispatched through
`app.infrastructure.blocking.run_abandonable` — a daemon thread, not
`asyncio.to_thread` — with one `UcsManagerClient` (and `UcsHandle`) per
domain per run, never shared across concurrent tasks. **Updated
2026-09-06 (Phase 2 of the production-hardening pass):** `UcsHandle`'s own
`timeout=` bounds one socket operation, not the whole call (see below), so
`_with_timeout` now also imposes a whole-call deadline via
`asyncio.timeout()` — `login()` previously had no deadline at all beyond
that per-socket one. See "Timeouts, abandoned threads and poisoned
clients" below for the mechanism and its consequences, shared with UCS
Central.

Confirmed against the installed `ucsmsdk==0.9.27` source, not
documentation:

- **Constructor**: `UcsHandle(ip, username, password, port=None,
  secure=None, proxy=None, timeout=None)`. `timeout` is urllib's, so it
  bounds each individual socket operation (connect, and each blocking
  read). It is **not** a total-request or total-run deadline. Read from
  the same source (moved here from `UcsManagerClient._with_timeout`'s
  docstring, 2026-09-13): `socket.create_connection` resolves DNS
  *before* applying that timeout at all, one `post()` can retry up to
  three times internally (a TLSv1 fallback, a redirect), and a fresh
  `login()` issues up to three `post_elem` calls of its own — so
  `login()` alone is unbounded in the DNS-hang case and only loosely
  bounded otherwise, which is why the wrapper's own deadline exists.
- **Endpoint must be a bare hostname or IP.** `UcsSession.__create_uri`
  builds `"%s://%s:%s" % (protocol, ip, port)` with `ip` interpolated
  raw, so a scheme or an embedded port produces a mangled URL
  (`https://https://host:443`). `_validate_endpoint` rejects both up
  front rather than letting it surface as an opaque connection error.
- **`query_classid` returns a plain list**, never `None`; `[]` when
  empty.
- **Exceptions come from two disjoint trees**, both rooted at
  `Exception`: `UcsError` (with `UcsException`,
  `UcsValidationException`) and `UcsWrapperException` (with
  `UcsLoginError`, `UcsConnectionError`, `UcsOperationError`). Catching
  the two roots covers all six.
- **Network failures are in neither tree.** `ucsdriver.post` re-raises
  urllib's errors untouched, and `URLError`/`socket.timeout` are
  `OSError` subclasses — so every call catches `OSError` alongside the
  SDK roots, and callers only ever see one `UcsManagerConnectionError`.
- **`login()` raises on bad credentials** and never returns a falsy
  value, so an authentication failure cannot silently proceed as if
  connected.
- **`logout()` before a successful login is a no-op** (`_logout` returns
  early when the session cookie is `None`), so calling it from a
  `finally` after a failed login costs nothing and sends no request.

**Why `login()` sits inside the `try`** in `list_servers`:
`ucssession._login` sets the session cookie and only *then* calls
`_update_version()` / `_update_domain_name_and_ip()`, either of which can
raise with the session already established server-side. Logging in
outside the `try` would leak that session until UCS Manager times it out.

### `ucscsdk` (UCS Central)

Confirmed directly against the installed `ucscsdk==0.9.0.10` source,
which `diff -rq` showed byte-identical to github.com/CiscoUcs/ucscsdk
master at `6c9a34f` (ADR-0014).

- **`UcscHandle(ip, username, password, port=443, proxy=None)` takes no
  `timeout`.** `ucscsession.post` calls `ucscdriver.post(uri, data,
  read)` without forwarding one, so `urlopen` runs with `timeout=None`
  and a wedged Central blocks forever. ADR-0014 records this and the
  decision to impose a deadline in the wrapper;
  `UcsCentralClient._with_timeout` is that control.
- **The imposed timeout leaks a thread, deliberately.** See "Timeouts,
  abandoned threads and poisoned clients" below — this bullet originally
  described `asyncio.wait_for(asyncio.to_thread(...))`, which was itself
  the bug Phase 2 fixed: the abandoned thread stalled interpreter
  shutdown for 300 seconds and then hung *unboundedly*, not merely until
  "the OS gives up". ADR-0014 records the original tradeoff; the
  mechanism it describes is superseded, not the decision to accept a
  leaked thread at all.
- **`port` must be 443.** `__create_uri` raises for any other value, so
  unlike `ucsmsdk` there is nothing to configure and an endpoint with an
  embedded port is always wrong.
- **`endpoint` must be a bare hostname or IP.** `__create_uri` builds
  `"%s://%s%s%s" % ("https", ip, ":", port)` with `ip` interpolated raw,
  so a scheme produces `https://https://host:443`.
- **`query_classid(class_id=None, filter_str=None, hierarchy=False,
  need_response=False, dme='central-mgr')` returns a list**, `[]` when
  empty.
- **Exceptions come from two disjoint trees**, both rooted at
  `Exception`: `UcscError` (with `UcscException`,
  `UcscValidationException`) and `UcscWrapperException` (with
  `UcscLoginError`, `UcscConnectionError`, `UcscOperationError`).
  Catching the two roots covers all six — the same split `ucsmsdk` uses,
  with a `c` in the names. Recorded here because the class names differ
  and this set is written down nowhere else.
- **Network failures are not in either tree**, and **`logout()` before a
  successful login is a no-op** — both exactly as for `ucsmsdk` above.

### Timeouts, abandoned threads and poisoned clients

Added 2026-09-06 (Phase 2 of the production-hardening pass), shared by
both clients via `app.infrastructure.blocking.run_abandonable` — read its
module docstring first for the full mechanism, confirmed against this
project's own installed CPython 3.13.15, not assumed:

- **Neither SDK's blocking call can be cancelled once started**, and
  `asyncio.wait_for`/`asyncio.timeout` only ever abandon the *await* —
  the underlying OS thread keeps running regardless of which executor it
  came from.
- **A dedicated `ThreadPoolExecutor` does not fix this**, and this was
  measured, not assumed — see `docs/notes/2026-09-refactor-plan.md`'s
  correction. `ThreadPoolExecutor` workers are non-daemon with no way to
  make them otherwise, and land in the same interpreter-wide join-at-exit
  registry the *default* executor's workers do. Only a manually created
  **daemon thread** is never joined at all, by either mechanism.
- **A client is poisoned once its own deadline fires.** `_with_timeout`
  sets `self._poisoned_since` and every further call on that same
  instance is refused — the abandoned thread may still be mutating
  `self._handle`, and a second thread touching the same SDK session
  concurrently is worse than refusing to proceed. Build a fresh client
  (a fresh domain/run already does this) rather than reusing a poisoned
  one.
- **A poisoned instance's `logout()` is also refused**, and that refusal
  is swallowed by `logout()`'s own `except Exception` (it "never
  raises") — so a poisoned client's session is never explicitly closed.
  Whatever remote-side session the abandoned call was mid-request for —
  most concerning for `login()` — may leak until UCS Central/Manager
  times it out on its own. There is no way to avoid this without a
  cancellable SDK call, which neither vendor's SDK offers.
- **`asyncio.timeout()`'s `.expired()`** distinguishes "our own deadline
  fired" from "the awaited call itself raised a `TimeoutError` before the
  deadline" — meaningfully different only for `ucsmsdk`, whose own
  socket-level timeout can genuinely raise one; `ucscsdk` has no timeout
  of its own to raise, so that branch is unreached there in practice, and
  is kept as a correctness boundary rather than folded away.

### Sessions

A client is one instance per collector run, never pooled or reused:
neither handle is documented as safe for concurrent use from multiple
tasks, and `asyncio.to_thread`'s one-call-at-a-time dispatch from a
single client instance keeps every call to that handle sequential.

`list_servers` must be iterated to exhaustion or closed via
`contextlib.aclosing` — abandoning the generator part-way leaves sessions
to be cleaned up at GC time, and both Central and UCS Manager enforce a
per-user session cap. `IngestService.ingest` drains it fully.

**Early close of `UcsCentralProvider._list_servers` cancels *and drains*
the domain tasks** (moved here from the code, 2026-09-13). A consumer
that stops early (`--limit`, a killed run) throws `GeneratorExit` in at
the `yield`, which unwinds that frame but not the per-domain tasks still
running — each holding a `UcsManagerProvider`, i.e. a live `UcsHandle`
session against that per-user cap. The `finally` therefore cancels every
task and then `gather`s them with `return_exceptions=True`, so each domain
still reaches `UcsManagerProvider._list_servers`'s own
`finally: await client.logout()`. Unlike Redfish's version of the same
pattern there is no `except TimeoutError`: UCS Central has no run budget
(ADR-0014's 2026-09-02 update defers it deliberately), so this guards
early close only.

### Debugging

`INVENTORY_UCS_DUMP_XML=1` turns on the SDK's own request/response XML
dump, and is shared by both Cisco clients — one switch for "show me the
Cisco XML", whichever collector is running.
`tools/run_collector.py --debug-xml` sets it. It is read from the
environment rather than threaded through constructors because it belongs
to a run, not to a manager. Never on by default: the dump includes full
inventory payloads and would bury a real collector run.

### Error model

`UcsManagerConnectionError` and `UcsCentralConnectionError` are
deliberately not `app.errors.AppError` subclasses — see
`app.domain.ports.credentials.CredentialNotFoundError` for why
collector-side errors do not go through the API's RFC 9457 error model.

## UCS Central domain discovery and pruning

### Central is a directory, not an inventory source

Central is asked exactly two questions:

1. `computeSystem` — which domains are registered, at what address, and
   what Central believes each holds.
2. `lsServer` — which service-profile names live in which domain, used
   *only* to skip domains that certainly hold nothing of ours.

Everything in a `ProviderServer` then comes from that domain's own UCS
Manager through `UcsManagerProvider` unchanged. The reasoning for that
split, and the removal of the standalone UCS Manager entry point, is
ADR-0014's 2026-08-17 update.

### Why one login reaches every domain

Central hands out each registered domain's address as
`ComputeSystem.address` — confirmed by Cisco's own
`ucscsdk/utils/ucscdomain.py`, whose `get_domain()` filters
`ComputeSystem` on exactly that property (ADR-0014). A single UCS Manager
service account is valid across the domains of one fleet, which is why
there is no `INVENTORY_UCS_MANAGER_IP` and why
`app.infrastructure.credentials.env.resolve_login` exists to ask for a
login without an endpoint.

`DomainTarget.endpoint` falls back to the domain's `name` when `address`
is empty; a domain with neither is skipped with a
`ucs_central.domain_without_address` warning, so the log says "no
address" rather than surfacing an opaque DNS failure later.

### The pruning rules

**Skipping means "do not open a session to this domain". It is never a
deletion**, in UCS or in MongoDB — `IngestService` has no reap path, so a
skipped domain's existing documents simply stop being refreshed.

Three rules, in the order they apply:

1. A domain with no reachable address is skipped.
2. A domain is skipped **only** when Central reports profiles for it
   *and* none match the collector's name pattern.
3. **A domain whose profiles Central does not report is collected, never
   skipped.** ADR-0014's open question is precisely whether Central
   replicates domain-*local* service profiles; pruning on missing
   evidence would silently drop exactly the domains that question is
   about, and the symptom would be a mysteriously small inventory rather
   than an error. Absence of evidence gets a round trip, not a guess.

Rules 2 and 3 are pruning and are silent by design. Rule 1 is a fault:
a registered domain with no address is recorded in the provider's
`collection_errors`, alongside any domain whose collection raised, and
`tools.run_collector` turns a non-empty list into a `PARTIAL` report and
exit status 3. So a domain that *should* have been readable and was not
turns the run red, while a domain that genuinely holds nothing of ours
does not.

The pattern is applied with **`re.search`**, matching
`tools.run_collector._NameFilteredProvider` exactly. This function must
never be stricter than that filter, which is the only thing that decides
which servers are actually ingested. `^ocp` means "starts with" because
the operator wrote the anchor, not because the code added one. The test
`test_pattern_matching_mid_name_still_keeps_the_domain` fails if anyone
changes it to `re.match`.

A profile's `domain` key is looked up against the domain's name, then its
address, then its id: Central may key it by any of the three depending on
how the domain was registered.

### Why no server-side `filter_str`

`ucscsdk` supports
`query_classid(..., filter_str='(name, "ocp.*", type="re")')`, and it is
deliberately unused. The name a server is filtered on lives on its
`lsServer` profile, not on the compute MO, so a server-side filter
narrows only one query — and it would put a second, subtly different copy
of "which servers are mine" beside `_NameFilteredProvider`. One filter,
applied once. The code carries a `# ponytail:` marker saying to revisit
only if payload size ever measurably hurts; at 10k servers this is a few
MB per run.

### `central_external_id` — why DNs are re-rooted

**UCS Manager DNs are domain-local and therefore collide.** Every domain
in the fleet has a `sys/chassis-1/blade-1`. Servers collected here all
carry `manager_id = mgr_ucs_central` (one `Manager` document per type,
from `tools.run_collector.manager_for`), so their external ids land in a
single `Server.external_ids[mgr_ucs_central]` namespace where an
un-rooted DN identifies several machines at once.

Identity resolution happens on vendor+serial in
`app.application.services.ingest`, so this would **not** merge two
servers — it would make the recorded external id useless for saying
*which* server, and `domain_id_from_dn` could no longer recover the
owning domain.

Anything not under `sys/` is returned unchanged: org-rooted DNs
(`org-root/ls-...`, which is what `profile_template_external_id` carries)
are global in Central and already correct.

### Cost, and the shape this deliberately is not

Two Central queries, then per collected domain one login plus the 14
domain-wide queries `UcsManagerProvider` issues (pinned by
`test_scales_query_count_independently_of_fleet_size`; 14 as of
`equipmentRackUnitPsuStats`'s addition ("Power supplies (PSUs)" above) —
was 13 after `graphicsCard`, 12 after `equipmentPsu`, 11 before that, 10
at earlier points in this file's own history). **That per-domain cost
is flat in server count** — a domain holding 500 servers costs the same
as one holding 10 — so the only levers are how many domains get
contacted (pruning) and how many at once (`concurrency`, from
`INVENTORY_UCS_CENTRAL_DOMAIN_CONCURRENCY`).

This is not the shape a naive port takes. The obvious implementation
resolves each matching profile's `pn_dn` with a `query_dn` and then walks
`query_children` for board, CPUs, controllers and disks — five-plus round
trips *per server*, on top of the per-domain login. The
domain-wide-query-plus-client-side-join that `UcsManagerProvider` already
implements collapses that to a constant number of requests per domain,
and `group_by_owning_server_dn` is what makes the join exact.

**Two different levels of "streamed," worth telling apart.** Within one
domain, `_collect_domain` still buffers that domain's servers into a
plain list rather than fanning them out through a per-server queue — one
domain's `ProviderServer` list is small next to the managed objects
`UcsManagerProvider` already holds for that same domain while joining
them, so a queue would add machinery to save nothing, and that
reasoning is unchanged. *Across* domains, though, `list_servers()` no
longer waits for the whole batch: it yields each domain's list the
moment that domain finishes (`asyncio.as_completed`, ADR-0014's
2026-09-02 update), not after every concurrent domain resolves
(`asyncio.gather`, the original and now-replaced shape). Getting finer
than per-domain — streaming individual servers across domains as they
complete — was considered and rejected for the same reason as above: it
would need a real fan-in queue for a saving this collector's domain
sizes don't make worth it.

A failing domain is caught, logged as `ucs_central.domain_failed` with a
`collected_before_failure` count, and contributes an empty list — an
unreachable or slow domain must never cost the fleet its entire run,
mirroring `tools.run_collector._run_one_manager`'s per-manager
isolation. Since ADR-0014's 2026-09-02 update, this also means a domain
that already finished keeps its data even if a *different* domain is
what triggers `activeDeadlineSeconds` — the two failure modes (one
domain erroring vs. the whole pod being killed) now degrade the same
way, whereas before the fix, a killed pod lost every domain regardless
of how many had already finished.

### `_log_one_domain` — the check on Central's domain list

The domain list is the one thing this collector still takes from
Central's replica and cannot verify any other way, so every run emits
`ucs_central.domain_summary` per registered domain. `total_physical_cnt`
is what Central *believes* a domain holds; `collected_servers` is what
that domain's own UCS Manager actually returned. **Logged per domain, as
soon as that domain's own result is known** (added 2026-09-02, alongside
the streaming fix above) — a skipped domain right after planning, a
collected domain the moment `as_completed` yields it — not batched at
the end of the run the way it originally was; a kill mid-run no longer
loses the coverage line for a domain that had already finished, the
same way it no longer loses that domain's server data. Three failures
live in the gap, none visible from the total ingested count:

1. A domain Central lists but whose UCS Manager we could not collect from
   — an unreachable address, a login not valid on that domain, or pruning
   that cut too hard.
2. A domain whose registration Central has but whose inventory it never
   synced — `inventory_status` says so directly.
3. Central's own view going stale — `last_refreshed_ts`. Note this is now
   only ever a statement about the *domain list*, never about server
   data, which comes straight from the domain.

**`collected_servers` is `None` for a domain never contacted and `0` for
one contacted that returned nothing.** That distinction is load-bearing:
"we did not ask" and "we asked and got nothing" are different failures
and must not collapse into the same number.

`ucs_central.domain_collected_nothing` warns on the reported>0 /
collected==0 gap. `ucs_central.profiles_in_unregistered_domain` warns
when Central reports profiles whose `domain` matches no registered
`computeSystem` — inventory we were told about but cannot reach, and the
one case the per-domain loop cannot surface since it iterates registered
domains.


## Intersight managed objects

**Provenance for this whole section: the generated models in the
installed `intersight==1.0.11.2026072720` wheel**, which are the OpenAPI
contract rendered as Python. Most of it is still unconfirmed against a
live tenant — see ADR-0017's "Validation" section for exactly what a
2026-09-01 field test against the user's own on-prem tenant did and did
not settle. The `TotalMemory` unit (the "Units" section below) and the
`storage.Controller`/`graphics.Card`/`processor.Unit` `ComputeBoard`
fallback (the "join topology" table above) are fleet-verified — the rest,
including the "CPU model" section, is still *contract-verified* only
(confirmed to exist and to be cheap in the SDK's own model, not yet
confirmed against a live response), which is a weaker claim. Facts a real
run settles
should be moved here with their own provenance line, individually.

### The server anchor

`compute.PhysicalSummary` is the consolidated blade-and-rack view and is
the only anchor query. It carries `Moid`, `Dn`, `Name`, `UserLabel`,
`Model`, `Serial`, `Uuid`, `Vendor`, `TotalMemory`, `NumCpus`,
`NumCpuCores`, `NumThreads`, `MgmtIpAddress`, `ManagementMode`,
`ServiceProfile`, `AlarmSummary`, `ChassisId` and `Presence`.

It carries **no** typed relationship lists — `compute.Blade` and
`compute.RackUnit` have `adapters`, `storage_controllers`, `bmc` and so
on, and the summary has none of them. That does *not* mean per-server
queries are needed: every child object carries an **inverse** reference
back up, which is what the fleet-wide join uses.

`ManagementMode` is one of `IntersightStandalone`, `UCSM` or `Intersight`
(`compute_physical_summary.py:471`), and the schema's declared default is
`IntersightStandalone`.

### The join topology

Each sub-resource is listed once for the whole estate and attached
client-side. Two of the joins are two-hop, because the object does not
reference the server directly:

| Object | Reaches its server via |
|---|---|
| `server.Profile` | `AssociatedServer`, else `AssignedServer` |
| `adapter.Unit` | `ComputeBlade` / `ComputeRackUnit` |
| `adapter.ExtEthInterface` | `AdapterUnit` -> `adapter.Unit` |
| `adapter.HostEthInterface` | `AdapterUnit` -> `adapter.Unit` |
| `storage.Controller` | `ComputeBlade` / `ComputeRackUnit`, or `ComputeBoard` -> `compute.Board` |
| `storage.PhysicalDisk` | `StorageController` -> `storage.Controller` |
| `graphics.Card` | `ComputeBlade` / `ComputeRackUnit`, or `ComputeBoard` -> `compute.Board` |
| `management.Controller` | `ComputeBlade` / `ComputeRackUnit` |
| `management.Interface` | `ManagementController` -> `management.Controller` |
| `processor.Unit` | `ComputeBlade` / `ComputeRackUnit`, or `ComputeBoard` -> `compute.Board` |
| `compute.Board` | `ComputeBlade` / `ComputeRackUnit` |
| `equipment.Psu` | `ComputeRackUnit` **only** — no `ComputeBlade`, no `ComputeBoard` |

**The `ComputeBoard` fallback is load-bearing, not defensive
programming.** Confirmed live 2026-09-01 against a real tenant: **0 of
37** `storage.Controller` rows set `ComputeBlade`/`ComputeRackUnit` at
all — every one set only `ComputeBoard`. Without following it, every
drive on every server on that hardware read as unread. `graphics.Card`
and `processor.Unit` carry the identical relationship and got the same
fallback pre-emptively; `adapter.Unit` and `management.Controller` carry
**no** `ComputeBoard` relationship at all (confirmed against Cisco's own
generated Go SDK) and must never have it added to their `$select` — an
unsupported field risks failing the whole query, not just being ignored.
See `docs/adr/0017-intersight-collector.md`'s "The `ComputeBoard` join
gap" for the full story.

Exactly one of `ComputeBlade`/`ComputeRackUnit` is set on any given
object, depending on whether the server is a blade or a rack unit.

### The server's name

The same trap UCS Manager has, and the contract is explicit about it.
`compute.PhysicalSummary.Name` is **never** an operator hostname: it is
the fabric-interconnect cluster name plus a chassis/slot when
UCSM-attached, the CIMC's own name in standalone mode, and model plus
chassis/server id under Intersight management. The real name is
`server.Profile.Name`, reached through the *inverse* relationship.

**`server.Profile` has no `Dn` field at all** (verified: absent from its
`attribute_map`). This matters twice — selecting one would risk failing
the whole profiles query, and it means an Intersight server has **no org
path to fall back to** when its name carries no site token. UCS Central's
servers do; these do not, and they resolve to no site. The only DN
available is `ServiceProfile` on the summary, which the contract says is
populated *only* in UCSM mode.

`server.Profile` carries both `AssignedServer` and `AssociatedServer`,
both typed `ComputePhysicalRelationship`, with no documented precedence.
The collector prefers `AssociatedServer` — the machine actually running
the configuration — and falls back to `AssignedServer`.

### PHYSICAL versus VNIC

Settled by the SDK's own docstrings, and it is the inverse-looking pair
of names that makes this worth writing down:

- **`adapter.ExtEthInterface`** is the *physical* cabled uplink. It has
  `SwitchId`, `ExtEthInterfaceId`, `PeerDn`, `PeerPortId`, `MacAddress`.
- **`adapter.HostEthInterface`** is the *vNIC* — its own docstring uses
  the word "vNIC". It has `Name`, `HostEthInterfaceId`, `VnicDn`,
  `MacAddress`, and **no `SwitchId`**.

`vnic.EthIf` is a **design-time policy object** (LAN Connectivity Policy
configuration), not live state. Do not use it for attachments.

Neither interface class carries a numeric speed. Only the switch-side
`ether.PhysicalPort`/`ether.HostPort` have `OperSpeed`/`AdminSpeed`, as
free-form strings of unverified format, so `speed_mbps` is `None`.

**The dry-run print (`tools/run_collector.py`) hides fabric-interconnect
fields on a `VNIC` attachment**, added 2026-09-01 at the user's request
after seeing every `IntersightStandalone` server print
`fabric None … FI model/serial=—/—` on each of its attachments. A vNIC
structurally never carries a fabric relationship at all (the `SwitchId`
gap just above), so printing FI-shaped fields on one reads as missing
data rather than as a field its kind has no equivalent for — the same
"— " convention `_dry_run_one_manager`'s own comment already applies to
a provider with no service-profile concept at all. A standalone server
never shows an FI-shaped line as a direct consequence, without needing
its own special case: `attachment()` already skips an uncabled physical
uplink (no `SwitchId`), so a standalone server contributes zero
`PHYSICAL` attachments and every remaining line is `VNIC`. This is a
`tools/run_collector.py` change, not a mapping one — it applies to every
provider's dry-run output, not only Intersight's.

### CPU model

**Added 2026-09-01**, reversing a scope cut in ADR-0017 that turned out
to rest on a stale citation of ADR-0009 (see ADR-0017's Decision 5 for
the full correction). `processor.Unit` is a first-class MO, listable
fleet-wide at `/api/v1/processor/Units`, carrying a direct `ComputeBlade`
/ `ComputeRackUnit` relationship — the same cost class as
`storage.Controller`/`graphics.Card`, already in the request plan.

`cpu_model` is the first socket's non-empty `Model`, in the order the API
returns them — the Intersight analogue of `ucs_manager.mapping._cpu_model`'s
"first equipped socket" rule, but without filtering on
`processor.Unit.Presence`: its exact equipped-value string is unverified
for Intersight (`ucsmsdk`'s `"equipped"` prefix is confirmed, Intersight's
is not), and an unpopulated socket has no processor installed and so
reports no `Model` anyway — which is enough to skip it without needing
the enum.

**Unresolved:** whether `Model` or `Description` carries the
human-readable name (e.g. "Intel Xeon Gold 6338") is unverified — both
exist on `processor.Unit` and the SDK docstrings don't distinguish them.
`Model` was chosen to mirror the UCS Manager/Central convention; settle
by comparing both fields on one live row against the Intersight UI's own
CPU panel. Provenance:
`docs/notes/intersight-inventory-model.md`, "Follow-up 2026-09-01", §11.

### Units — the one that can silently corrupt data

- **`TotalMemory` has NO documented unit**, on `compute.PhysicalSummary`,
  `compute.Blade` or `compute.RackUnit` — all three carry the identical
  unit-less docstring "The total memory available on the server.". Its
  sibling `AvailableMemory` *is* documented "in MB", and per-DIMM
  `memory.Unit.Capacity` is documented "in MiB". The collector assumes
  MiB, matching `ucs_manager.mapping`'s assumption for the same hardware.
  **SETTLED 2026-09-01, against the user's own on-prem tenant: it is
  MiB.** A sampled server reported `TotalMemory = 786432`; the
  Intersight UI's own "Memory Capacity" for that same server reads
  768.0 GiB, and `786432 ÷ 1024 = 768.0` exactly. The assumption is
  correct — no change needed. (The `memory/Arrays` relationship filter
  `tools/verify_intersight.py` section 4 normally uses for an in-tool
  DIMM-sum check did not resolve on this tenant, so this fact was
  confirmed by hand against the UI instead; the filter itself is still
  unverified — see ADR-0017.) Provenance: ADR-0017's "The first real
  tenant run (2026-09-01)".
- **`storage.PhysicalDisk.Size` and `.RawSize` are documented "in MB"**,
  and are **strings**, needing parsing.
- **`storage.PhysicalDisk.NonCoercedSizeBytes` is documented in bytes**
  and is an int. The collector prefers it precisely because it names its
  own unit, and falls back to `Size` only when it is absent.

### GPUs — a capability ceiling, not a gap

`graphics.Card` carries `Model`, `Pid`, `Vendor`, `Serial`, `PciAddress`,
`GpuId`, `OperState`, `FirmwareVersion`. It carries **no memory,
temperature, power draw, or ECC field**, and neither does `pci.Device`
or `graphics.Controller` — grepped across every model in the wheel. The
collector therefore reports GPU identity with every telemetry field
`None`. Reporting zeros would read as a healthy idle GPU.

The Redfish collector gets this data because it reads `ProcessorMetrics`
and `EnvironmentMetrics` off the BMC directly. Intersight exposes no
equivalent.

**Reconfirmed 2026-09-01 against Cisco's own current, official metrics
documentation** (`developer.cisco.com/docs/intersight/supported-metrics-overview/`),
independent of the pinned SDK wheel above and current as of today's live
API rather than a point-in-time snapshot. Every hardware telemetry
category Intersight documents is: `hw.current`, `hw.fan`, `hw.host`,
`hw.memory`, `hw.network`, `hw.power_supply`, `hw.signal_power`,
`hw.temperature`, `hw.voltage`, `system.cpu`, `system.memory` — no
GPU/graphics/PCIe category exists at all. Two independent sources (the
SDK's inventory model and Cisco's separate telemetry API) now agree:
what an operator sees under the Intersight UI's "Inventory → GPUs" is
almost certainly identity-only, the same fields this collector already
reports.

### Power supplies (PSUs)

**Added 2026-09-01**, at the user's request, and the first provider to
populate a domain field that existed but had never been wired to real
data: `app.domain.models.hardware.Psu`/`Power` and the health engine's
`power.psu_count`/`power.failed_psu_count` metrics predate this
collector, but `IngestService` hardcoded `Power(psus=[])` unconditionally
until now — no vendor's provider had ever reported one.

`equipment.Psu` carries `ComputeRackUnit` but, confirmed against Cisco's
own generated Go SDK, **no `ComputeBlade` and no `ComputeBoard`
relationship at all** — unique among every MO this collector reads. A
blade's PSUs are shared across the whole chassis
(`EquipmentChassis`), not owned by one blade, so this MO structurally
cannot attribute a PSU to an individual blade server. **A blade server
therefore reports no PSUs through this collector; only rack/standalone
servers do.** This is a real capability gap for blade fleets, not a join
bug to fix — there is no per-blade PSU relationship for any join to
follow.

Fields read: `PsuId` (chassis-relative slot, e.g. "1"/"2", the `id`),
`Model`/`Pid` (falls back to `Pid` like every other equipment MO),
`Serial` (inherited from `EquipmentBase`, confirmed present the same way
as `storage.Controller`/`graphics.Card`/`pci.Device`), `OperState`, and
`PsuWattage` (a string, parsed as watts — no unit ambiguity is documented
for this field, unlike `TotalMemory`).

**`health` uses `normalize_oper_state`'s UP/DOWN/DISABLED/UNKNOWN
vocabulary** — the same OperState-sourced pattern `gpu()`'s own `health`
field already uses — not a literal `"OK"`/`"FAILED"` pair. The health
engine's `power.failed_psu_count` fact previously compared against the
literal string `"OK"`, which no provider (this one included) has ever
emitted; that comparison was dead code, exercised for the first time by
this change, and has been corrected to `health == "DOWN"` alongside it —
`UNKNOWN` is deliberately not counted as failed, matching
`storage.failed_drive_count`'s existing philosophy that a read failure is
not evidence of a hardware failure.

### Transport

- Auth is HTTP Signature `hs2019`. The signed header set is
  `(request-target)`, `Host`, `Date`, `Digest` — **not** `(created)`,
  which is what the draft standard and the SDK default to and which
  Intersight rejects. Taken from Cisco's own canonical example, embedded
  in the wheel's `METADATA`.
- The signing algorithm is chosen by key type, not configured: RSA keys
  (API key v2) sign `RSASSA-PKCS1-v1_5`; EC keys (v3) sign ECDSA.
  **Relying on a library default signs RSA-PSS**, which Intersight
  rejects for a v2 key.
- `$top` maxes at 1000 and `$top`/`$skip` is the only paging mechanism —
  there is no continuation token. Nothing documents the result set as
  stable across pages, so the collector orders every query by `Moid`.
  That is our own prudence, not a documented requirement.
- **Cisco publishes no rate limit anywhere reachable**, and the official
  SDK has no retry or backoff for HTTP status codes at all
  (`rest.py` raises on any non-2xx; `Configuration.retries` falls through
  to urllib3's connection-level `Retry(3)`, which does not cover 429).
  The collector implements its own 429 handling, honouring `Retry-After`.
- **No inventory MO has an `Organization` field** — only policy and
  profile MOs do. Organization scoping therefore cannot be used to
  partition inventory between collectors, which is why the UCS Central
  overlap is resolved by `ManagementMode` instead.
- `Results` is `null`, not `[]`, for an empty result set.
- **An error body is a JSON object with `code`, `message`, `messageId`
  and `traceId`.** *Provenance: a live probe against `intersight.com` on
  2026-08-29 with an unregistered key, which returned `code:
  "UnauthorizedOperation"`, `messageId: "iam_apikey_authheader_invalid"`.*
  This is the one Intersight fact in this file confirmed against the
  running service rather than the contract. The client surfaces
  `message` and `traceId` in its own error text; `traceId` is what Cisco
  needs to find a specific request.
- **`messageId` tells three 401s apart.** *Provenance: deliberately
  broken requests against the live `intersight.com` on 2026-08-29.*
  `iam_cookie_invalid` = no `Authorization` header arrived at all;
  `iam_apikey_signature_invalid` = the header could not be parsed;
  `iam_apikey_authheader_invalid` = the header parsed but the key could
  not be verified. Our own well-formed header always produces the third,
  even with corrupted signature bytes, a two-hour-stale `Date`, or a
  signature covering a different path — which is what proves the header
  construction itself is accepted. The client branches on these.
- **Authentication is checked before routing.** A nonsense resource path
  returns the same 401 as a real one, so resource paths cannot be
  validated without a working key. *Same provenance.*
- **Region may matter.** Intersight's 401 text asks the operator to
  "verify the API key and associated account region". Nothing here models
  a region; a tenant in a non-default region would presumably need a
  regional hostname in `INVENTORY_INTERSIGHT_IP`. *Untested.*
