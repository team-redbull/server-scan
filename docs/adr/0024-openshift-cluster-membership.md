# ADR-0024: cluster membership is reported by the clusters, per cluster, as a reconcile

Date: 2026-09-10
Status: Accepted

Supersedes nothing. `Server.openshift` has existed since Phase 1 and only
the fake seeder ever wrote to it; this is the decision about what fills it
and what it holds.

## Context

A server's only cluster signal was `Classification.installation_type`, a
regex verdict on its hostname. That says what a machine was *named* to be,
never whether anything is using it — a freed server and a running one are
byte-identical under it. "What can I build on" had no answer.

Nothing already in the platform could answer it either. Vendor managers
(UCS Central, Intersight, OME, OneView) know hardware, not workloads: to
UCS, a blade running a production cluster and a blade sitting idle are
both `associated`. Only the cluster knows.

## Decision

Two CronJobs, `tools/collect_openshift.py --source nodes|agents`, deployed
**per cluster** by ArgoCD from `deploy/helm/nodes-status`. Every
cluster runs the nodes job over its own worker nodes; an MCE hub also runs
the agents job over its Agents.

They write `Server.openshift` and nothing else, the same discipline
`MaintenanceService` follows — which is what lets one server be
simultaneously `HOSTED_CLUSTER`, `CRITICAL`, in maintenance and
`INSTALLED` without four writers fighting over one document. Deliberately
not `IngestService`: that rebuilds a whole `Server` from a
`ProviderServer` and would blank `name`, `identity.external_ids`,
`network.interfaces` and `connectivity`, none of which a cluster knows.

### Why a reconcile, not a write of what was seen

**Nothing in Kubernetes reports a removal.** A server freed from a cluster
simply stops appearing in its node list. A job that only wrote its
observations would leave every server it ever saw `INSTALLED` forever, and
the inventory would never show free capacity again.

So each run reconciles **the set it owns** — the servers already naming
*its* cluster — claiming what it sees and freeing what it does not:

```
for observation in observations:        # on the cluster -> claim
    seen.add(server.id)
for server in claimed_by(scope):        # Mongo says this cluster...
    if server.id not in seen:           # ...but the cluster did not
        free(server)                    #    -> AVAILABLE
```

`scope` is `{"openshift.cluster_name": <this cluster>}` for a nodes job
and `{"openshift.mce_name": <this MCE>}` for an agents job. **That scope
is what makes per-cluster deployment safe**: a job can only ever release
servers that name its own cluster, so a broken job in one cluster cannot
free another's machines. A fleet-wide "anything I did not touch is
available" sweep would have cluster A's job free every server in clusters
B and C on every run, because it cannot see them.

### Two refusals

Both matter more than they look, and both exist because the reconcile
frees on absence:

- **A failed API read exits non-zero and writes nothing.** A read that
  failed is not evidence of an empty cluster.
- **A *successful* read returning nothing also refuses.** A cluster with
  no workers is far more likely a broken selector, an RBAC change or a
  mid-upgrade blip than a genuinely emptied cluster — and acting on it
  would free everything that cluster holds.

### Correlation is by hostname, in two steps

`spec.hostname` first, `status.inventory.hostname` as fallback. The order
is the whole reason it works across vendors: Cisco reports the server's
real name as its hostname, while Dell reports one derived from a MAC that
matches nothing — there the real name is only in the **requested**
hostname an operator set.

`clean_hostname` returns `None` rather than `""` for a blank value. A
present-but-empty requested hostname would otherwise short-circuit the
`or` and strand every Dell host.

**`BareMetalHost` is deliberately not read**, even though `bootMACAddress`
would be a stronger key than a hostname. Not every server has a BMH, so a
BMH lookup would cover part of the fleet while looking complete.

### Node selection: label *and* name, not either

Nodes are selected by `node-role.kubernetes.io/worker` and then filtered
by a configurable name list (default `infra,control-plane`). Both, because
infra nodes usually carry the worker label too — so the label alone keeps
them — while names alone would misfire on a node called
`compute-infra-01`.

## Decision 2: `httpx` against `kubernetes.default.svc`, not a Kubernetes SDK

Every SDK worth using drags `google-auth`, `oauthlib`, `requests` and
`websocket-client` into an air-gapped mirror, and this needs two GETs.
ADR-0017 rejected a large SDK on the same grounds. In-cluster auth is a
token file and a CA file, so there is no kubeconfig to parse and **no new
dependency at all**.

The Agent CRD's version is discovered at runtime rather than pinned, which
removes the one real advantage `oc get agents` had: the job keeps working
when MCE moves the CRD to v1.

## Decision 3: three states, and five fields

`OpenShiftState` is `AVAILABLE` / `INSTALLED` / `INSTALLED_TO_INVENTORY`,
replacing `UNKNOWN` / `UPI_NODE` / `HOSTED_NODE` / `AVAILABLE`.

The old shape encoded *what kind* of node a server is into its status,
which `InstallationType` already answers, and left the same fact reachable
two ways with no single value the inventory could filter on.

**There is deliberately no "nobody looked yet".** `AVAILABLE` is the
default and the only state reached by absence. A separate `UNKNOWN` would
be indistinguishable from "no cluster holds this" everywhere it is shown,
and every server has to answer the in-use question somehow.

`OpenShiftLifecycle` carries exactly five fields:

| Field | Why it is here |
|---|---|
| `lifecycle_state` | The question this model exists to answer |
| `cluster_name` | Which cluster. Unique across this estate, including across MCEs |
| `mce_name` | Which MCE reported it; `None` on a plain cluster node |
| `last_reported_at` | Diagnostic. The reconcile is set-based, so nothing infers availability from this going stale |
| `reported_by_agent_id` | Which job instance wrote this, for tracing a wrong value back |

An earlier shape carried five more. Each was dropped for its own reason:

- **`cluster_id`** — cluster names are unique across this estate, so an id
  disambiguates nothing.
- **`role`** — every server this platform tracks is a worker.
- **`node_name`** — it is the server's name, which is what the hostname
  correlation just proved.
- **`agent_id`, `bmh_name`, `boot_mac`** — cluster-side handles this
  inventory never follows. `bmh_name` and `boot_mac` were never populated
  at all, since BareMetalHost is not read.

**Existing documents need a decode rule, and the first version of this
ADR was wrong to say they did not.** It claimed the old values "decode
as-is". They do not: Pydantic rejects an unknown enum member outright, so
a single stored `UPI_NODE` raised `ValidationError` inside
`Server.model_validate` and took down every read path that touches it —
`GET /servers`, the seeder, and every ingest correlating on an existing
document. Found in a real cluster on 2026-09-10, where a collector run
reported `errors=232` of 258 servers and the seeder crashed outright.

`OpenShiftLifecycle` therefore carries a `mode="before"` field validator
that maps any state the enum no longer has onto `AVAILABLE`. It is the
same catch-all rule the sites aggregation already applies so slice totals
add up, and it is what makes the sentence above true rather than
aspirational. The jobs then overwrite the value on their first run.

The general rule this cost us: **narrowing a persisted enum is a
migration, not an edit.** Nothing in the type system flags it, the tests
all passed because every fixture was written by the new code, and the
failure only appears against a database that predates the change.

## Decision 4: kept separate from `Classification`, on purpose

`InstallationType` says what kind of server this is; `OpenShiftState` says
whether it is in use. **When they disagree the server is misnamed or
misplaced, and that disagreement is the signal.** Reconciling them
silently would destroy it. `IngestService` therefore carries the whole
`openshift` object forward untouched on every ingest, exactly as it does
`maintenance`: hardware and cluster membership are observed by different
systems on different schedules, and neither is entitled to blank the
other's findings.

## Deployment

A Helm chart, one release per cluster, replacing an earlier kustomize
base/overlay tree — ArgoCD points an `Application` per cluster at the
chart path and carries the few values that differ:

```yaml
# UPI cluster                    # MCE hub
nodes:                           nodes:
  enabled: true                    enabled: true
  clusterName: ocp4-tlv            clusterName: mce-tlv
                                 agents:
                                   enabled: true
                                   mceName: mce-tlv
```

`clusterName`/`mceName` are Helm `required` — a release without them fails
at template time rather than deploying a job that exits 2 on every
schedule. `agents.enabled` is off by default: a cluster with no Agent CRD
would run the job every 15 minutes and fail every time, turning a real
alert channel into noise. RBAC renders only the rule an enabled job needs,
so a UPI cluster gets no `agents` grant.

`concurrencyPolicy: Forbid`, not `Replace`: two overlapping reconciles
would race on the same servers, and the loser's release would undo the
winner's claim.

## Consequences

- Free capacity is a first-class, filterable fact
  (`?openshift_state=AVAILABLE`), and the sites landing page can show it.
- Every cluster needs network to the platform's MongoDB and a copy of the
  `mongo-uri` and `cursor-secret` Secrets. The jobs write directly, like
  every collector; they never call the platform's API.
- A cluster that stops running its job leaves its servers `INSTALLED`
  indefinitely. Nothing detects that yet — it is the same gap as
  collector staleness detection, still item 0 of the not-done list, and
  this feature adds two more CronJobs that it will need to cover.
- `INSTALLED_TO_INVENTORY` is only ever produced by the agents job, so an
  estate with no MCE will never see that state.

## Update (2026-09-21): an unmatched host is an ERROR line, not only a count

A node or agent whose hostname matches no server in the inventory (the
job creates nothing — the vendor collectors own what hardware exists) was
counted in the summary and printed to stdout, but the structured log only
carried `unmatched=<n>`. Each one is now also logged at `ERROR` as
`openshift.host_not_in_inventory` with the hostname and the reporting
cluster, so a log query on the CronJob finds the exact hosts. The exit
code is unchanged: 3 (PARTIAL) whenever any host was unmatched. The job
still reads the cluster through the Kubernetes API rather than shelling out
to `oc` (Decision 2), so there is no `oc get nodes -o name` to add.

## Update (2026-09-22): the chart can render its own `mongo-uri`/`cursor-secret` Secrets

Consequence above said every cluster needs "a copy of the `mongo-uri` and
`cursor-secret` Secrets," which meant provisioning them out of band —
`oc create secret` or a secrets operator — before every release. At the
operator's request, `db.mongoUri`/`db.cursorSecret` in values are now an
alternative: when set, `templates/db-secret.yaml` renders both Secrets
itself, so one `helm install`/`helm upgrade` (or one ArgoCD sync) is the
whole deployment, values included.

That is plaintext in the values file, which is why it is an option rather
than the default (both stay `""`, so the pre-existing-Secret path is
unchanged unless a release opts in). It is a reasonable choice only where
the values file's own storage is already trusted for secrets — the
operator's case is a private, air-gapped Git repo — and stops being one
the moment that repo's access model changes or a secrets operator becomes
available for the environment.

Same request added `jobTtlSeconds` (default 1800, 30 minutes), applied to
both CronJobs' `jobTemplate.spec.ttlSecondsAfterFinished` — the same
TTL-controller mechanism the server-scan chart's collector CronJobs already
use, just shorter here to match the 15-minute schedule instead of the
collectors' multi-hour one.

## Update (2026-09-22): the worker label selector is gone; name exclusion is the only filter

"Node selection: label *and* name, not either" above was deliberate, and
its stated reason — a name match alone "breaks the moment anyone names a
node differently" — held for as long as `node-role.kubernetes.io/worker`
was reliably present. At the operator's request, after confirming it is
**not** reliably present on every worker across their clusters: some
worker nodes carry no `worker` role label at all, so the label selector
was silently dropping real capacity in those clusters, with nothing to
detect it — the exact "a truncated read looks like an empty cluster"
failure mode this ADR otherwise goes out of its way to avoid, just
approached from the other direction (a truncated *result*, not a failed
read).

`OpenShiftClient.worker_nodes()` now lists every node in the cluster with
no `labelSelector` at all, and `exclude_name_parts` is the only filter
left. This inverts the risk the original decision was written to avoid:
before, a mis-named exclude term could only ever *under*-exclude (miss a
node that should have been dropped, caught by the label). Now it can also
*over*-include (a control-plane/infra/bootstrap node with no matching
substring is counted as fleet capacity), with nothing to catch it — the
label was the only thing making that direction safe.

Mitigations, not a fix for the underlying risk: `master` joined the
shipped default (`infra,control-plane,master` — the chart's
`excludeNameParts`, `INVENTORY_OPENSHIFT_EXCLUDE_NAME_PARTS`,
`Settings.openshift_exclude_name_parts`), since it is a common UPI
control-plane name the old default never needed to cover. Every operator
deploying this chart now needs to audit their own cluster's actual node
names — there is no longer a structural guarantee that a wrongly-named
control-plane node gets excluded.

## Update (2026-09-22): the same filter now applies to `agents` too

`exclude_name_parts` was `nodes`-only: `agents()` took no such parameter,
and `agent_observation` had no name-based exclusion at all — an Agent
carrying an unwanted hostname (an MCE hub's own infra, say) had no way to
be dropped the way a node could. At the operator's request, the same
filter now applies to both.

Rather than duplicate the substring check per source, or teach `client.py`
about hostname resolution (its whole job is "make the API call, hand back
plain dicts" — hostname semantics belong to `records.py`), the check moved
out of `client.py` entirely: `records.name_excluded(hostname, parts)` is a
pure function, and `tools.collect_openshift._observe` applies it once,
uniformly, to `ClusterObservation.hostname` — the value already resolved
per source (`node_observation`'s `metadata.name`, `agent_observation`'s
`spec.hostname` falling back to `status.inventory.hostname`) — rather than
to either source's raw field. `OpenShiftClient.worker_nodes()` and
`agents()` are back to being plain listers with no filtering of their own.

The chart's `excludeNameParts` moved from `nodes.excludeNameParts` to the
top level, alongside `timeZone`/`jobTtlSeconds`, and both CronJob
templates now set `INVENTORY_OPENSHIFT_EXCLUDE_NAME_PARTS` from it —
previously only the `nodes` CronJob set that variable at all, so `agents`
silently ran on `Settings`' own hardcoded default regardless of what an
operator configured.
