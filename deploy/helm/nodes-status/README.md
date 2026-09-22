# Cluster membership CronJobs

Deployed **to every cluster**, by ArgoCD — one release per cluster. This
is a separate chart from `deploy/helm/server-scan`, which deploys the
platform itself: these jobs run *inside* a cluster and report which
servers it is using.

Two jobs, and which ones a cluster gets is the whole per-cluster config:

| Job | Runs on | Reads | Writes |
|---|---|---|---|
| `openshift-nodes` | every cluster | its own worker nodes | `INSTALLED` + the cluster name |
| `openshift-agents` | MCE hubs only | its Agents | `INSTALLED` + cluster, or `INSTALLED_TO_INVENTORY` |

## A UPI cluster

The nodes job alone.

```yaml
nodes:
  enabled: true
  clusterName: ocp4-tlv
db:
  mongoUri: "mongodb://server-scan:...@mongo-host:27017/server-scan?authSource=server-scan"
  cursorSecret: "..."
```

## An MCE hub

Both: the agents job for the fleet this MCE manages, the nodes job for the
hub's own hardware, which is a cluster like any other and would otherwise
never be reported as in use.

```yaml
nodes:
  enabled: true
  clusterName: mce-tlv
agents:
  enabled: true
  mceName: mce-tlv
db:
  mongoUri: "mongodb://server-scan:...@mongo-host:27017/server-scan?authSource=server-scan"
  cursorSecret: "..."
```

`agents.enabled` is off by default on purpose: a cluster with no Agent CRD
would run the job every 15 minutes and fail every time, turning a real
alert channel into noise. `clusterName`/`mceName` are `required` — a
release without them fails at template time rather than deploying a job
that exits 2 on every schedule.

An ArgoCD `Application` per cluster points at this chart path and carries
those few values; nothing else differs between clusters.

## What a job may release

Neither job writes `AVAILABLE` for the whole fleet. Each reconciles only
the servers already naming **its own** cluster (or MCE): a server it no
longer lists is released, and one belonging to another cluster is never
touched. That is what makes per-cluster deployment safe — a broken job in
one cluster cannot free another cluster's machines.

Two refusals are built in, and both matter more than they look:

- A failed API read exits non-zero and writes nothing. A read that failed
  is not evidence of an empty cluster.
- A *successful* read returning nothing also refuses to write. A cluster
  with no workers is far more likely a broken selector, an RBAC change or
  a mid-upgrade blip than a genuinely emptied cluster.

## Name-based exclusion — both jobs

`excludeNameParts` (top-level, default `infra,control-plane,master`) is
the **only** filter, for **both** `nodes` and `agents`: each job reads
every node/Agent and drops the ones whose resolved hostname contains one
of these substrings. There is no `node-role.kubernetes.io/worker` label
selector any more (ADR-0024's 2026-09-22 updates): it was not reliably
present on every worker across this operator's clusters, and a worker
missing the label was silently never reported as capacity. For `agents`,
"resolved hostname" means the requested (`spec.hostname`) or reported
(`status.inventory.hostname`) hostname — the same value that names it on
the server, not the Agent CR's own `metadata.name` (a UUID).

**That means every host in your fleet that is not real capacity — a
control-plane/master node, an infra node, an MCE hub's own bootstrap
host, whatever else your naming convention uses — must be named here, or
it is counted as one.** Audit your actual node/Agent hostnames before
relying on this — `infra`/`control-plane`/`master` cover the common
OpenShift conventions, but a differently-named one needs its own
substring added. One list for both jobs, per cluster, rather than editing
the code. Remember the match is a plain substring: a node called
`compute-infra-01` is dropped by `infra` too, which is why a term needs
to be specific enough not to also catch hosts you want kept (`vcompute`
vs `compute` is exactly this: adding `vcompute` drops only names
containing that whole substring, not every name containing `compute`).

## Cleaning up finished runs

`jobTtlSeconds` (default `1800`, 30 minutes) sets both CronJobs'
`jobTemplate.spec.ttlSecondsAfterFinished` — same TTL-controller mechanism
as the server-scan chart's `collectors.jobTtlSeconds`, just shorter here
because these jobs run every 15 minutes rather than every few hours: long
enough to check a run's logs before the next one lands, short enough that
finished Jobs never pile up. Set to `0` to disable and rely on
`successfulJobsHistoryLimit`/`failedJobsHistoryLimit` alone.

## What each cluster needs

- **Network** to the platform's MongoDB, plus the `mongo-uri` and
  `cursor-secret` Secrets (`db.*` names them). The jobs write directly,
  like every collector — they never call the platform's API.

  Two ways to get those Secrets there, picked per release:

  - **Set `db.mongoUri` / `db.cursorSecret` in values** (`db-secret.yaml`)
    and this chart renders both Secrets itself — nothing to create by
    hand, nothing outside `helm install`/`helm upgrade` or an ArgoCD sync.
    That puts the connection string and the cursor-signing key in
    plaintext in whatever holds this values file, so it's the right
    choice only where that file's own storage is already trusted for
    secrets (e.g. a private, air-gapped Git repo) — not a public or
    shared one.
  - **Leave them blank** and pre-provision Secrets named `db.secretName` /
    `db.cursorSecretName` yourself (a secrets operator, or `oc create
    secret`) — unchanged from before, and still the better choice once a
    secrets operator (Vault, External Secrets, sealed-secrets) exists for
    the environment.

  `db.dbName` (default `server-scan`) is which database the connection is
  actually used against — read explicitly by name, never taken from the
  URI's own path segment. **It must be the same value as the server-scan
  chart's `db.dbName`**, since both write into the same database.
- The API image, pullable from this cluster.
- Nothing else: RBAC ships with the chart, read-only on `nodes` and on
  `agent-install.openshift.io` agents, and only the rule a job enabled
  here actually needs.

## Check before trusting a run

```bash
oc create job --from=cronjob/<release>-openshift-nodes probe-1 -- \
  python3 -m tools.collect_openshift --source nodes --dry-run
```

`--dry-run` writes nothing and prints the match rate plus every unmatched
hostname. A real run also logs each one as an `ERROR`
(`openshift.host_not_in_inventory`, with the hostname) and exits 3. A low match rate means correlation is failing, not that the
cluster is empty — check that Dell hosts have their **requested** hostname
set, since their reported one is derived from a MAC and matches nothing.
