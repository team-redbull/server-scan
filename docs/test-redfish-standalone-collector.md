# Testing the standalone Redfish collector

Step by step, cheapest and safest first. Every step before §4 writes
nothing to MongoDB, and steps 1–2 touch at most one BMC.

Read §0 before running anything. **The two mistakes that cost a morning
are both configuration, not code**, and both are checked at load time — so
you will get a message naming what to fix rather than a failed run.

> **Before you start, one safety rule.** A rejected login is never
> retried, but a *wrong* credential run against a fleet still costs one
> failed login per host — and Dell blocks the collector's IP for an hour
> after 3 failures, while Lenovo can lock an account until an admin
> unlocks it. So: **prove one host works (§1–2) before pointing this at a
> fleet.** That is the whole reason the steps are in this order.

Related: `docs/adr/0016-redfish-standalone-collector.md` for why the
design is shaped this way, and `docs/test-ucs-collector.md` for the
equivalent Cisco runbook.

---

## 0. Configuration

Two files and a handful of variables. Full list with commentary in
`.env.example`; templates in `docs/examples/`.

```bash
# The fleet list — a TOML file, or a directory of *.toml for a sharded estate.
INVENTORY_REDFISH_INVENTORY_FILE=./redfish-inventory.toml

# Per-host logins. Optional: a fleet sharing one account can skip this.
INVENTORY_REDFISH_CREDENTIALS_FILE=./redfish-credentials.toml

# The fallback used by any host that resolves no per-host credential.
INVENTORY_REDFISH_USERNAME=
INVENTORY_REDFISH_PASSWORD=
```

Start from the examples and **keep the real files out of git** — both
names are already in `.gitignore`, and `tests/unit/test_no_committed_secrets.py`
fails the build if a real one is ever added:

```bash
cp docs/examples/redfish-inventory.example.toml   ./redfish-inventory.toml
cp docs/examples/redfish-credentials.example.toml ./redfish-credentials.toml
```

A minimal inventory is three lines:

```toml
[[hosts]]
host = "192.0.2.41"
name = "ocp4-prod-tlv-infra-01"
```

**The two things worth knowing up front:**

**`host` is a bare address.** No `https://`, no path, no `user:pass@`.
All three are rejected at load with a message saying so — the last one
because a credential in an address would otherwise reach MongoDB, the
API and this collector's own dry-run output.

**`name` decides the site.** The platform parses a server's site out of
its name (`ocp4-prod-tlv-infra-01` → site `tlv`). A BMC usually does not
know that name, so without `name =` the server lands in "Unassigned" and
classifies as `UNCLASSIFIED`. That is correct behaviour, not a bug — but
it is almost never what you want.

**`INVENTORY_COLLECTOR_NAME_PATTERN` does not apply to this collector.**
The inventory file is the filter, and a more precise one. If `^ocp` were
applied over a name a BMC does not know, it would discard every host you
listed. `INVENTORY_REDFISH_NAME_PATTERN` overrides that exemption if you
do want a name filter here on top of the inventory file — the exemption
suppresses the shared default, not a pattern set for this collector by
name.

---

## 1. One host, no credentials sent

Start here. This proves the network path, the TLS chain and Redfish
conformance **without presenting a password to anything** — the service
root is unauthenticated by specification, which is exactly what makes
this safe against a typo'd address.

```bash
curl -sS https://192.0.2.41/redfish/v1 | jq '{
  RedfishVersion,
  type: ."@odata.type",
  Systems: .Systems."@odata.id",
  expand: .ProtocolFeaturesSupported.ExpandQuery
}'
```

**What good looks like:**

```json
{
  "RedfishVersion": "1.15.0",
  "type": "#ServiceRoot.v1_5_0.ServiceRoot",
  "Systems": "/redfish/v1/Systems",
  "expand": { "ExpandAll": true, "Levels": true, "MaxLevels": 2 }
}
```

| What you see | What it means |
|---|---|
| `curl: (60) SSL certificate problem` | Expected on a factory BMC. Go to §5's TLS section — do **not** reach for `-k` and move on. |
| `curl: (7) Failed to connect` | Network path, not the collector. Check routing and firewall from where the collector will actually run. |
| `"@odata.type": "ServiceRoot.1.0.0.ServiceRoot"` (dotted, no `v1_`) | Pre-Redfish. This is HPE iLO 4 and it is **out of scope** — the collector rejects it before sending a credential. |
| `"expand"` absent | Fine. `$expand` is a latency optimisation with a mandatory fallback; the collector probes and verifies it per host. |

Repeat once per vendor, model and firmware family you own. That handful
of curls is worth more than any amount of reading.

---

## 2. Dry run — the real code path, still writing nothing

Point the collector at a **canary inventory of one to five hosts** first.

```bash
cat > ./canary.toml <<'EOF'
[[hosts]]
host = "192.0.2.41"
name = "ocp4-prod-tlv-infra-01"
EOF

INVENTORY_REDFISH_INVENTORY_FILE=./canary.toml \
  uv run python -m tools.run_collector --manager-type REDFISH_STANDALONE --dry-run
```

This authenticates, walks the whole traversal, maps everything, prints
it — and writes nothing. It is the honest test of the credential and the
mapping together.

**What good looks like:**

```
=== redfish-standalone (REDFISH_STANDALONE @ ./canary.toml) ===

[1] ocp4-prod-tlv-infra-01
     external_id : redfish://192.0.2.41/redfish/v1/Systems/1
     site (from name): one
     vendor/model: dell / PowerEdge R660
     serial/uuid : FCH2201V0AB / 4c4c4544-0043-...
     cpu         : 2 sockets, 64 cores, 128 threads (Xeon Gold 6338)
     memory      : 512.0 GiB
     storage     : 3.5 TiB total across 8 drive(s)
     bmc         : redfish://192.0.2.41/redfish/v1/Systems/1 (mac 00:00:5e:00:53:99)
     nic macs    : 00:00:5e:00:53:01, 00:00:5e:00:53:02
     attachments : 0
```

**Read four lines specifically:**

1. **`site (from name)`** — if this says `— none in name`, add `name =`
   to the inventory entry. Everything downstream (site rollups,
   classification) depends on it.
2. **`vendor`** — `dell`/`cisco`/`hp` where recognised, `standalone` for
   a manufacturer this platform does not model. `standalone` is not an
   error.
3. **`attachments : 0`** — correct and expected. A standalone server has
   no fabric interconnect, so the seeded `connectivity.fabric_paths_down`
   policies have nothing to evaluate.
4. **Anything reading `not read`** — the collector could not read that
   sub-resource. Distinct from `—`, which means it read it and found
   nothing. `not read` values are **never written over good stored data**;
   see §5.

**Note:** `--dry-run` still needs MongoDB reachable — it connects before
branching. A failure there is not a Redfish problem.

Add `--limit 3` to stop early, and `--debug-http` to log one line per
request (method, path and status only; the session exchange is skipped
entirely rather than redacted).

---

## 3. Dry run — the full inventory

The file-validation step, at zero write risk. Everything that can be
wrong with the inventory is caught here, and it is also the first honest
measurement of how long a sweep takes.

```bash
uv run python -m tools.run_collector --manager-type REDFISH_STANDALONE --dry-run
```

Every load-time failure names what to fix and stops **before any
connection**:

| Message | Fix |
|---|---|
| `parsed 0 hosts … check the volume, not the file` | The ConfigMap did not mount. This is deliberately not the same message as "every host is down". |
| `host 'x' names group 'site-onee', which is not defined. Known groups: …` | A typo. It fails rather than falling through to the default credential — which is how a typo would otherwise spray a shared account. |
| `host 'x' references credential 'y', which is not defined in …` | Add the entry, or point the host elsewhere. |
| `credential 'y' needs both username and password` | A blank password reaches the BMC as a real login attempt, and that attempt counts toward lockout. |
| `host 'x' is already defined in …` | A duplicate would silently lose one entry's settings. |
| `host 'x' disables TLS verification without a verify_tls_reason` | Add the reason. It is what makes the exception visible in review. |
| `host 'x' embeds credentials` | Move the login into the credentials file. |

---

## 4. The real run

```bash
uv run python -m tools.run_collector --manager-type REDFISH_STANDALONE
```

In-cluster, with the CronJob deployed but still suspended:

```bash
kubectl create job --from=cronjob/<release>-collector-redfish-standalone redfish-manual-1
kubectl logs -f job/redfish-manual-1
```

Only un-suspend (`collectors.redfishStandalone.suspend: false`) once a
manual run has come back clean.

### Exit codes — start here when something goes wrong

| Code | Meaning | What to do |
|---|---|---|
| **0** | Every host answered and mapped. | Nothing. |
| **1** | The run aborted. | Act now. Usually the credential failure budget, or MongoDB. |
| **2** | Misconfigured. | The message names the file or variable. Safe to fix and re-run immediately. |
| **3** | **PARTIAL** — some hosts did not answer. | Usually nothing. See below. |

**Exit 3 is the normal outcome** for a fleet of independent BMCs. Forty
of four hundred down is a Tuesday. This is why the CronJob's Job status
is *not* something to alert on — see §6.

---

## 5. Triage

### Exit 3 — read the failure classes, not the individual lines

```
manager=redfish-standalone fetched=360 created=12 updated=348 errors=0
manager=redfish-standalone PARTIAL — this run did not see the whole fleet:
  - 192.0.2.41: unreachable — Could not reach 192.0.2.41: All connection attempts failed
  - 192.0.2.57: TLS verification failed — …
  - 192.0.2.63: login failed for credential '192.0.2.63' — not retried
```

| Pattern | Diagnosis |
|---|---|
| A handful `unreachable` | Hosts off, being reimaged, or dead. **The normal case.** Worth checking whether the *same* hosts fail every run — grep `redfish.host_unreachable`. |
| *Every* host `unreachable` | Not a fleet problem. Egress, routing or DNS from the collector pod. Re-run §1 from a debug pod in the same namespace. |
| A few `login failed` | Those hosts have different credentials. Add a `[credentials."<host>"]` entry. Grep `redfish.bmc_login_failed`; the run summary's `auth_failures` is the total. |
| `TLS verification failed` | See below. |
| `exceeded its 180s budget` | One slow BMC, already contained — the other hosts completed. If it happens every run, investigate that BMC rather than raising the budget for the whole fleet. |
| `authenticated but exposes no system` | The address is a chassis or enclosure manager rather than a server, or Redfish is licence-gated on that hardware. |

### Many `redfish.bmc_login_failed` for the same credential

```
redfish.bmc_login_failed host=192.0.2.41 credential='shared-lab' run_failures=3
```

**Nothing stops the run any more.** The credential circuit breaker that
used to disable a credential after three rejections was removed
2026-09-12 at the operator's request (ADR-0016's dated update): every
host is attempted every run, so a wrong shared credential now costs one
failed login on *every* BMC that uses it, every run.

**So do not simply re-run.** Many hosts rejecting the same credential
means the credential is wrong, not that the hosts are.

1. Verify by hand against **one** host: `curl -u user:pass https://<host>/redfish/v1/Systems`
2. If the account is locked (Lenovo XCC, OpenBMC), an admin must unlock it.
3. If requests from this host are refused for about an hour, that is
   Dell's IP block. It clears on its own.
4. Fix the credential, then re-run.

The run summary's `auth_failures` is the count across the whole run, and
each rejection's own line names the host and the credential.

### TLS failures — do not "fix" these by disabling verification

The collector sends a password in the session POST. With verification
off, anyone in the network path harvests a credential that — on an estate
with shared accounts — works on every BMC.

| `reason` | Fix |
|---|---|
| unknown CA | Point `INVENTORY_REDFISH_CA_BUNDLE` at the issuing CA. **The scalable answer**: import an internal CA to every BMC, or use Dell's custom signing certificate — one import trusts every iDRAC using it. |
| expired | The BMC's certificate lapsed. Renew it. |
| hostname mismatch | The inventory names the BMC differently from its certificate. Fix the inventory entry. |

Only if none of those is possible, per host, with a written reason:

```toml
[[hosts]]
host = "192.0.2.7"
verify_tls = false
verify_tls_reason = "expired factory cert, replacement scheduled INC-1234"
```

The reason is mandatory, and the host is logged at WARNING on **every**
run. There is deliberately no global switch: a per-host opt-out shows up
in a git diff, and a global flag set once during an incident is never
unset.

### "Data went missing from a server"

It should not — and this is worth understanding, because the obvious
guess is wrong.

When the collector cannot read a sub-resource (a `Storage` collection
that 404s, say), it reports `None` for those fields, **not zero**. The
ingest pipeline carries the previous value forward. So a transient read
failure does not blank a server's disks, and — importantly — does not
clear a `CRITICAL` failed-drive finding by reporting zero drives.

If data genuinely disappeared, the collector read the resource
successfully and found it empty. That is a real hardware or
configuration change.

---

## 6. What to alert on

**Do not alert on Job failure.** With exit 3 as the normal outcome the
Job is routinely red, and an alert that fires every day gets muted before
the day it matters.

**Alert on staleness instead:**

1. **No successful run in 3× the schedule interval** (18h at the default
   6h). From `kube_cronjob_status_last_successful_time`. This one rule
   catches every total-failure mode — suspended CronJob, image pull
   failure, wrong credential, Mongo down.
2. **Servers not seen recently.** Since 2026-09-12 this is a gauge —
   `server_scan_servers_stale{source_provider="REDFISH_STANDALONE"}` — and
   the `ServerScanServersStale` alert the chart ships (ADR-0029). The
   manual query below is kept for a database with no Prometheus in front
   of it:

```js
// NOTE: last_seen_at is stored as an ISO 8601 STRING, not a BSON date
// (ADR-0006). Comparing against a Date silently returns nothing.
db.servers.countDocuments({
  source_provider: "REDFISH_STANDALONE",
  last_seen_at: { $lt: "2026-08-22T00:00:00Z" }
})
```

**Why this is manual for now, stated plainly:** the collector is a
CronJob — a pod that lives minutes and exits — so Prometheus never
scrapes it, and no metric it emits could report its own absence. The only
thing that can answer "40 hosts have been failing for two weeks" is the
API reading `last_seen_at`. Until that ships, run the query above on a
schedule. See ADR-0016's consequences.

---

## 7. Confirming the data landed

```bash
curl -s 'localhost:8080/api/v1/servers?source_provider=REDFISH_STANDALONE&page_size=5' | jq '.items[] | {name, vendor, site_id, source_provider}'
```

`?source_provider=REDFISH_STANDALONE` is the filter that answers "these
machines have no manager — do not go looking for them in OpenManage or
UCS". Combine it with `&vendor=cisco` for "Cisco standalone".

A server showing `site_id: null` is a **naming** problem, not a collector
one — go back to §2.

---

## 8. Air-gapped notes

The collector needs outbound 443 to every BMC in its inventory, and
nothing else. It reaches no internet service at runtime.

**It adds no dependency.** The client is built on the `httpx` this
project already pins, so there is nothing new to mirror — see ADR-0016
for why the DMTF and OpenStack Redfish libraries were both rejected.

The test fixture (`tests/redfish_fixture.py`) is stdlib-only and runs
in-process, so CI needs no hardware and no network egress.

---

## Related documents

- `docs/adr/0016-redfish-standalone-collector.md` — the design, the
  evidence, and what is still unproven.
- `docs/examples/redfish-inventory.example.toml` — the fleet list format.
- `docs/examples/redfish-credentials.example.toml` — the credentials format.
- `docs/test-ucs-collector.md` — the equivalent Cisco runbook.
