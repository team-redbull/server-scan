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

## Node selection

`nodes.excludeNameParts` (default `infra,control-plane`) drops nodes by
name substring, *after* the `node-role.kubernetes.io/worker` label
selector. Both filters apply: infra nodes usually carry the worker label
too, so the label alone keeps them, while names alone would misfire on a
node called `compute-infra-01`. Add to it per cluster rather than editing
the code.

## What each cluster needs

- **Network** to the platform's MongoDB, plus the `mongo-uri` and
  `cursor-secret` Secrets (`db.*` names them). The jobs write directly,
  like every collector — they never call the platform's API.
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
