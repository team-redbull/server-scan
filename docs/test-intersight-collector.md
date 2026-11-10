# Testing the Cisco Intersight collector against real hardware

**Read this first: no server inventory has ever been mapped from real
Intersight data by this collector.** Its transport and authentication
path have been exercised against the real `intersight.com` service (see
ADR-0017's validation section), but every field mapping below is built
from the published contract alone — the DevNet Intersight sandbox went
offline on 2026-08-01 with no committed return before ~Q1 2027, and there
is no downloadable emulator equivalent to the UCS Platform Emulator.

Every step below therefore does double duty: it commissions the collector
for your estate, **and** it is the first time this code has seen real
inventory.

`docs/adr/0017-intersight-collector.md` records what is proven, what is
assumed, and what only a live run can settle. Section 4 here settles the
biggest assumption in a single query.

**In a hurry, or running this in a site you have to travel to?**
`docs/field-test-checklist.md` is the short version: four exported
variables and one command, plus what to bring back.

Nothing in sections 1–5 writes anything: no MongoDB connection, no
`Manager` document, no `POST` to Intersight. The first write happens in
section 6, and only when you ask for it.

---

## 1. Mint a read-only API key

Intersight has **no username/password path for its REST API**. Every
request is signed with an API key, so "the credential" is two things: an
**API Key ID** and that key's **PEM private half**.

1. In Intersight, go to **Settings → API Keys → Generate API Key**.
2. Give it a description naming this platform, so a future operator can
   tell what it belongs to before revoking it.
3. Choose **API key for OpenAPI schema version 3** if offered. Version 2
   (RSA) also works — the collector reads the PEM header and picks the
   signing algorithm itself, so there is nothing to configure either way.
4. **Save the private key when it is shown. It is shown once.**
5. Copy the **API Key ID**. It is a long `/`-joined string, not a
   username.

**Least privilege.** Create the key under an account holding the
**Read-Only** system role, not an administrator. This collector never
writes: it issues `GET`s against `compute`, `server`, `adapter`,
`storage`, `graphics`, `management` and `memory`. If your organisation
prefers a custom role over the built-in one, grant read on those object
types and nothing else. Confirm the role's actual privilege list in
**Settings → Roles** before provisioning — Cisco's role contents are not
something this document can promise for your tenant.

**The key must be unencrypted.** A passphrase-protected PEM is
deliberately unsupported: there is nowhere uniform to put a fourth
credential value, and it would be protecting a secret that sits beside it
in the same Kubernetes Secret anyway. The collector says so explicitly
rather than failing obscurely.

---

## 2. Point the collector at the right endpoint

```bash
INVENTORY_INTERSIGHT_IP=intersight.com          # SaaS tenant
INVENTORY_INTERSIGHT_IP=isight.corp.example.com # on-prem appliance
```

A **bare hostname**: no scheme, no port, no path. The collector builds
the URL itself and rejects anything else up front, because the `Host` it
signs must match the one it sends.

**Air-gapped sites: this must be a Private Virtual Appliance.**
`intersight.com` is on the public internet. A *Connected* Virtual
Appliance still calls home and does not solve the problem.

**TLS certificate verification is unconditionally disabled** for this
collector — a deliberate, explicit user decision (2026-08-31), reversing
this doc's earlier "never disabled, import the CA" stance. There is no
`INVENTORY_INTERSIGHT_CA_BUNDLE` or verify flag; the signed request and
its response go to whatever answers at `INVENTORY_INTERSIGHT_IP`, in
every environment including a production tenant. See
`app.infrastructure.providers.intersight.client.IntersightClient`.

---

## 3. Run the pre-flight verifier

```bash
export INVENTORY_INTERSIGHT_IP=...
export INVENTORY_INTERSIGHT_API_KEY_ID='<API Key ID>'
export INVENTORY_INTERSIGHT_API_KEY_PEM="$(cat ~/intersight-key.pem)"

uv run python -m tools.verify_intersight
```

Read-only. It signs a `GET`, reports what the tenant holds, and prints a
verdict.

### What good output looks like

```
1. CONNECTION
-------------
endpoint : intersight.com
key id   : 61970b91.../61970b91.../626f24e5...
key      : PEM supplied

auth     : OK — the API key was accepted and inventory is readable.

2. WHAT THIS TENANT HOLDS
-------------------------
servers sampled : 200
  ManagementMode Intersight                138
  ManagementMode IntersightStandalone       47
  ManagementMode UCSM                       15  <- owned by the UCS Central collector; ...

servers this collector would ingest: 185 of 200

3. THE SERVER NAME (the field everything else depends on)
---------------------------------------------------------
servers with an assigned server.Profile : 185 / 185
names carrying a parseable site token   : 185 / 185
names matching INVENTORY_COLLECTOR_NAME_PATTERN='^ocp': 185

first 10 resolved name(s):
  ocp4-prod-tlv-infra-01
  ...

VERDICT
-------
GOOD — every collectable server resolved a profile name, and
       185 of them match the collection pattern.
```

### What each failure means

| Output | Cause | Fix |
|---|---|---|
| `not a PEM private key` | `_API_KEY_PEM` holds something else | It is a PEM. See section 1. |
| `could not be parsed` | Truncated or mangled PEM | Re-copy including the BEGIN/END lines. |
| `passphrase-protected` | Encrypted key | Supply an unencrypted PEM. |
| `HTTP 401 ... clock is +N s` | Node clock drift | Fix NTP on the node, then retry. |
| `HTTP 401` with no clock note | Expired, revoked, wrong id, or mismatched key | Intersight answers all four identically. Check in that order. The message now quotes Intersight's own text and a `traceId` — that id is what Cisco TAC needs to find the request. |
| `401` mentioning **account region** | Tenant may not be in the default region | Try the regional hostname in `INVENTORY_INTERSIGHT_IP`. Untested; report what works. |
| `HTTP 403 ... Read-Only` | Key's role cannot read inventory | Grant read on `compute` and friends. |
| `unreachable` | No route, DNS, or TLS failure | Air-gapped? You need a Private Virtual Appliance. |
| `BAD — no server's name matches` | Wrong pattern, or names not where we look | **Stop.** See section 5. |

---

## 4. Settle the `TotalMemory` unit — do not skip this

**This is the single highest-risk unknown in the collector.**
`TotalMemory` carries no documented unit anywhere in Cisco's contract:
not on `compute.PhysicalSummary`, not on `compute.Blade`, not on
`compute.RackUnit`. Its sibling `AvailableMemory` *is* documented in MB,
while per-DIMM `memory.Unit.Capacity` is documented in MiB. The collector
assumes MiB, matching what `..ucs_manager.mapping` already assumes for
the same hardware.

**If that assumption is wrong, every server's memory is over-reported by
4.86%, silently, forever.** Nothing else in the platform would notice.

Section 4 of the verifier settles it with two independent signals. The
first is free — `AvailableMemory` sits on the same object and *is*
documented "in MB". The second is authoritative: it sums the server's
DIMMs, whose `memory.Unit.Capacity` is documented "in MiB", reached
through `memory.Array` because a DIMM carries no reference to its server.

```
4. THE TotalMemory UNIT (ADR-0017's highest-risk open item)
-----------------------------------------------------------
server            : WZP24140ABC
TotalMemory       : 524288   (no documented unit)
AvailableMemory   : 524288   (documented 'in MB')
  -> the two agree exactly. Whatever unit AvailableMemory uses,
     TotalMemory uses it too. See the DIMM check below for which.
sum of DIMM sizes : 524288   (documented MiB, across 16 DIMM(s))

SETTLED: TotalMemory is in the same unit as the DIMMs (MiB). ...
```

If the verifier reports it **could not read `memory/Arrays`**, filtering
on a relationship's `Moid` is not supported by that endpoint — the
syntax is not something this repo has been able to confirm against a
live tenant. Do the comparison by hand in the Intersight UI instead and
record the answer. Do not skip it.

- **`SETTLED`** — record it in ADR-0017's UNVERIFIED list as resolved and
  delete the caveat. That is a real fact bought with a live run; it
  belongs in `docs/cisco-collectors.md` with its provenance.
- **`MISMATCH ... MB-vs-MiB ratio`** — change `_BYTES_PER_MB` in
  `app.infrastructure.providers.intersight.mapping` to `1000 * 1000`
  **before scheduling the CronJob**, and update ADR-0017.
- **`MISMATCH`** with any other ratio — do not trust
  `memory_total_bytes` at all. Report the numbers in ADR-0017 first.

---

## 5. Check the names before anything is written

The platform parses a server's **site** out of its name and filters the
fleet with `INVENTORY_COLLECTOR_NAME_PATTERN`. A name sourced from the
wrong field does not fail — it collects nothing, or labels the whole
fleet "Unassigned".

`compute.PhysicalSummary.Name` is documented as never being an operator
hostname: it is a fabric-interconnect cluster name plus a chassis slot,
or the CIMC's own name, or a model plus a server id. The real name comes
from the associated `server.Profile`.

So in verifier section 3:

- **`servers with an assigned server.Profile` well below the total** —
  those servers fall back to their `UserLabel`, then to the summary's
  name. Expected for standalone-claimed CIMCs; a problem for
  Intersight-managed servers, and worth understanding before you rely on
  their sites.
- **`names carrying a parseable site token` below the total** — those
  servers will show as "Unassigned". Note that unlike UCS Central,
  **Intersight servers have no org-path fallback**: a `server.Profile`
  has no `Dn` field, so there is nothing to parse a site from when the
  name says nothing.
- **`names matching ... : 0`** — a real run would ingest nothing. Either
  the pattern is wrong for this estate or the names are not where the
  collector looks. Do not proceed.

Then see the raw DTOs, still without writing anything:

```bash
uv run python -m tools.run_collector --manager-type INTERSIGHT --dry-run --limit 1
uv run python -m tools.run_collector --manager-type INTERSIGHT --dry-run
```

Check against the hardware you know: model, serial, CPU counts, memory,
drive count and sizes, the BMC address, and that `attachments` shows
`PHYSICAL` and `VNIC` rows separately rather than all of one kind.

`--debug-http` logs one line per request — method, path and status only.
The private key, the signature and the `Authorization` header are never
logged, and there is no flag that would log them.

---

## 6. The first real run

```bash
uv run python -m tools.run_collector --manager-type INTERSIGHT
```

Exit codes: `0` complete, `2` not configured (the message names the
variables), `1` the run failed, **`3` PARTIAL** — some servers were
written but a sub-resource query failed, so this run did not see the
whole fleet. A `3` is not a crash and not a success: the named
sub-resource reports `None`, the previously-stored value is preserved
rather than overwritten, and the message says which query failed.

Then check the result through the API and UI: the servers appear, their
`source_provider` is `INTERSIGHT`, their sites are right, and their
health states reflect real hardware rather than absent data.

---

## 7. Scheduling it

```yaml
collectors:
  intersight:
    enabled: true
    ip: "isight.corp.example.com"
    apiKeyId: "..."
    apiKeyPem: ""    # pass with --set-file, not in a committed file
```

```bash
helm upgrade --install server-scan-collectors ./deploy/helm/server-scan-collectors \
  --set config.existingConfigMap=server-scan-api-config \
  --set-file collectors.intersight.apiKeyPem=/path/to/intersight-key.pem
```

Everything the collector needs comes from `values.yaml`; the chart
renders its own Secret and no pre-existing Secret has to be created.
Keep the PEM out of a committed values file —
`tests/unit/test_no_committed_secrets.py` fails the build if one lands
there.

The default schedule is every 6 hours, matching every other collector
(2026-09-10, at the operator's request — this used to be hourly, and the
wire cost would still support that: one run costs on the order of a
hundred requests regardless of fleet size, because every sub-resource is
listed once for the whole estate and joined in memory).

**Do not add `UCSM` to `managementModes`** unless your UCS domains are
genuinely not registered with UCS Central. Those servers are exactly the
ones the UCS Central collector already owns, and collecting both makes
one document's fields flip on whichever CronJob ran last.

---

## What to write down afterwards

ADR-0009's UCSPE validation found five defects that were invisible
without real hardware. This collector has had no equivalent, so the first
real run is worth documenting properly. In ADR-0017's UNVERIFIED list,
record:

1. The `TotalMemory` result from section 4.
2. Whether UCSM-mode servers have a `server.Profile` at all.
3. Whether `AssociatedServer` or `AssignedServer` was the one populated.
4. Whether `MgmtIpAddress` was present, or the BMC address came from
   `management.Interface`.
5. Anything that was empty in practice despite being in the schema.

Move each settled fact into `docs/cisco-collectors.md` **with its
provenance** — which tenant, which date, which appliance version. A fact
without its source becomes folklore nobody dares change.
