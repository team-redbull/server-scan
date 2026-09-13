# Deployment

Two Helm charts. `helm/server-scan` is the platform — API, frontend
and the per-vendor collector CronJobs — and is what the rest of this
document is about. `helm/openshift-membership` is the pair of jobs that
run *inside* every OpenShift cluster to report what it is using, one
release per cluster; it has its own README, and the section below says
why it is separate.

There used to be a parallel set of plain OpenShift YAML under
`openshift/`. It was removed rather than maintained: it held the same
five resources as the chart's templates, with nothing checking the two
agreed, and they had already drifted — a change to the collector's
credential handling had to be made twice and only landed fully in one.
`helm template` covers the "I want plain YAML" case on demand:

```bash
helm template server-scan deploy/helm/server-scan \
  -f my-values.yaml > manifests.yaml
```

and both ArgoCD and Flux render Helm natively, so a GitOps setup needs no
pre-rendered copy either. The production deployment is exactly that:
`team-redbull/redbull-platform` holds a copy of this chart under
`gitops/charts/server-scan`, and CI's `deploy` job keeps it current —
templates and files synced verbatim, image tags and `appVersion` pinned to
each release — so a green push to `main` reaches the cluster on its own
(ADR-0031). Only that repo's `values.yaml` is hand-maintained.

## Scope

The chart deploys the API, the collectors and — since 0.2.0 — optionally
the frontend, MongoDB and Redis. Which of those it stands up is four
independent switches, so a deployment picks its own shape:

| Value | Default | What it adds |
|---|---|---|
| *(always)* | — | API Deployment/Service/Route/ConfigMap |
| `frontend.enabled` | `false` | The React SPA (Deployment/Service/Route) |
| `mongodb.enabled` | `false` | A bundled MongoDB (Bitnami subchart) |
| `redis.enabled` | `false` | A bundled Redis (Bitnami subchart) |
| `collectors.<vendor>.enabled` | `false` | That vendor's CronJob |

**Both databases stay off by default**, because the original reasoning
still holds for a real production estate:

- MongoDB is the system of record and, in a real air-gapped production
  estate, is expected to already exist as an operated service (with its
  own backup, replication, and upgrade story) that this platform is
  pointed at — not a database this application owns the lifecycle of.
- Redis is an ephemeral cache; a small in-cluster instance is reasonable,
  but is equally fine to omit — every read path degrades to MongoDB on a
  cache miss or a Redis outage (`app.infrastructure.redis`), so Redis is
  never a hard dependency for this platform to run.

What bundling exists for is the case that reasoning does not cover: a lab,
a demo or a small site with no operated MongoDB/Redis to point at. Turning
`mongodb.enabled` on means this chart owns the lifecycle of the platform's
only durable state, backups included — a deliberate trade, not a default.

Each database is resolved on its own, so all four combinations render:

```bash
# Neither — the default. Both URIs come from db.secretName.
helm template server-scan deploy/helm/server-scan

# Both, plus the UI and the fake-data collector: a self-contained demo.
helm install si deploy/helm/server-scan \
  --set mongodb.enabled=true --set mongodb.auth.rootPassword=... \
  --set 'mongodb.auth.passwords[0]=...' \
  --set redis.enabled=true --set redis.auth.password=... \
  --set frontend.enabled=true --set route.host=scan.apps.example.com \
  --set collectors.fake.enabled=true --set backend.cursorSecret=...

# Redis only, against an operated MongoDB.
helm install si deploy/helm/server-scan \
  --set redis.enabled=true --set redis.auth.password=...
```

For whichever database is **not** bundled, the connection string arrives
via a `Secret` (`db.secretName`, default `server-scan-db`, keys
`mongo-uri` / `redis-uri`) that this chart consumes but does not create —
provisioning it is a platform/GitOps concern, consistent with the "no
credentials in source, credentials via secret refs" requirement. A
bundled one is rendered into the chart's own `<release>-bundled-db` Secret
instead, and the two names are deliberately different so bundling one
database never collides with a `server-scan-db` an operator owns.

**Set the bundled passwords explicitly.** Left blank, the Bitnami subchart
generates one — and `helm template`, which is how Argo CD renders this
chart, has no `lookup`, so a generated password is re-minted on every sync
while the database keeps the first one.

**Do not commit them.** For anything beyond a throwaway install, set
`mongodb.auth.existingSecret` / `redis.auth.existingSecret` and let a
secrets operator own the Secret. That changes where the *connection string*
comes from too: the chart never sees the password, so it cannot compose a
URI around it, and it falls back to `db.secretName` for that database. One
Secret then carries both — the subchart's own key
(`mongodb-root-password` + `mongodb-passwords`, or whatever
`redis.auth.existingSecretPasswordKey` names) and the `mongo-uri` /
`redis-uri` the API reads:

```bash
oc create secret generic server-scan-db \
  --from-literal=mongodb-root-password='...' \
  --from-literal=mongodb-passwords='...' \
  --from-literal=redis-password='...' \
  --from-literal=mongo-uri='mongodb://server-scan:...@server-scan-mongodb:27017/server-scan?authSource=server-scan' \
  --from-literal=redis-uri='redis://:...@server-scan-redis-master:6379/0'
```

Air-gapped installs need the subcharts vendored: `helm dependency update
deploy/helm/server-scan` on a connected machine, then commit the
resulting `charts/*.tgz`. Note that Bitnami's charts default their image
to `:latest`; `values.yaml` says how to pin one.

## Image tags

Both images default to the chart's `appVersion` — a real release like
`11.0.2`, published by CI as `X.Y.Z` with no leading `v`
(docker/metadata-action's `{{version}}` strips it, so the git tag `v11.0.0`
becomes the image tag `11.0.0`). Upgrading is a bump of `appVersion`, or of
`backend.image.tag` / `frontend.image.tag` to override one image.

**Do not point these at `latest`.** It reads as "always current" and is the
opposite: `pullPolicy: IfNotPresent` means a node that already holds a
`latest` layer never pulls it again, so the cluster keeps running the first
build it ever saw — through a pod delete, a rollout and every later release.
That is not hypothetical here; it is how this chart's first deployment
failed. A pinned tag also gives GitOps something to diff and roll back,
which a floating one cannot.

## The frontend

`frontend.enabled` deploys the SPA image CI already publishes. The SPA
calls the API same-origin — `frontend/src/api/client.ts` sets no base URL
— so both halves must answer on one hostname.

**Exactly one Route exists either way**: the frontend's when
`frontend.enabled`, the API's otherwise. With the frontend on, the SPA's
own nginx forwards `/api/` and `/health/` to the API Service in-cluster
(`frontend-api-proxy-configmap.yaml`, mounted into
`/etc/nginx/api-proxy.d`, which the image's `nginx.conf` includes), so the
API needs no Route of its own. This replaced a set of five path-scoped API
Routes on 2026-09-12, at the operator's request.

**One Route does not mean one path.** The Route's only job is getting
traffic into the cluster; nginx decides what belongs to the API. These
are forwarded today, all on the single hostname:

| Path | Serves |
|---|---|
| `/api/` | the REST API the SPA calls |
| `/health/` | liveness and readiness, which the SPA's status page reads |
| `/metrics` | Prometheus exposition, readable in a browser |
| `/docs`, `/redoc` | the OpenAPI pages |
| `/openapi.json` | the document those pages fetch |

**To expose another one, add a `location` block to
`frontend-api-proxy-configmap.yaml`** — there is no Route to create and
no hostname to pick. It is an explicit list rather than a catch-all only
because the SPA owns `/` and has client-side routes of its own
(`/servers`, `/rules`, `/health-policies`) that must not be proxied.

`/docs` loads Swagger UI's JavaScript from a public CDN, so on a
genuinely air-gapped cluster the page will render empty even though it is
reachable. `/openapi.json` is unaffected and is the useful half there.

**`route.host` is no longer mandatory with the frontend on.** One Route
means an OpenShift-generated hostname works; set `route.host` when you
want a specific one.

## Container security

The image (`Containerfile`, repo root) runs as a fixed non-root UID as a
sane local default, but nothing here pins `runAsUser` — OpenShift's
`restricted-v2` SCC assigns a UID from the namespace's allocated range at
admission time, and the image writes nothing to disk at runtime (all
logging is to stdout), so it runs correctly under whatever UID the SCC
assigns without an `anyuid` grant.

### The arbitrary UID trap, which cost a release to find

**An image that runs perfectly under `podman run` can still fail under
OpenShift**, and the error will not mention permissions. Both images hit a
version of this on their first ever deployment; both fixes are one line in
their Containerfile, and neither is obvious from a local run.

**The API image: `useradd -d /app` makes `/app` mode `0700`.** `WORKDIR`
then reuses that directory and `COPY` puts root-owned files inside an
unreadable parent. As UID 1001 — which every local `podman run` uses,
because the image says `USER 1001` — that is invisible. OpenShift assigns
an arbitrary UID with **GID 0**, `0700` denies it the working directory,
Python's cwd entry resolves to a directory it cannot read, and
`uvicorn app.main:app` dies with:

```
ModuleNotFoundError: No module named 'app'
```

An import error that says nothing about permissions and sends you looking
at `PYTHONPATH`. The fix is Red Hat's own convention, applied after every
`COPY`:

```dockerfile
RUN chgrp -R 0 /app && chmod -R g=u /app
```

Group 0 with group permissions mirroring owner is what makes an image work
under *any* assigned UID. To reproduce locally, run as a UID that is not
the owner and put it in group 0 — `podman run --user 12345:0 <image>` — not
the plain `podman run` that passes.

**The frontend image: `ubi9/nginx-124` is an S2I *builder* image.** Its own
CMD runs `$STI_SCRIPTS_PATH/usage`, which prints "This is a S2I rhel base
image", exits 0, and never starts nginx. A Containerfile that sets no CMD
inherits that, so the container CrashLoopBackOffs with a help message and
no error at all. It needs an explicit `CMD ["nginx", "-g", "daemon off;"]`.

### Image tags, and why not `latest`

Both images default to the chart's `appVersion` — a real release. `latest`
reads as "always current" and with `pullPolicy: IfNotPresent` is the
opposite: a node that already holds a `latest` layer never pulls it again,
not on a pod delete, not on a rollout, not after a new release. A cluster
here kept a v10-era API for hours after 11.0.0 published a working one, and
deleting the pods would not have changed it. See "Image tags" above.

## Metrics, and getting them into OpenShift's Observe page

The API's `/metrics` carries the HTTP metrics plus the fleet gauges of
ADR-0029 — `server_scan_servers_stale`, `server_scan_collector_last_seen_
timestamp_seconds`, `server_scan_cluster_last_reported_timestamp_seconds`,
`server_scan_servers_by_health` and friends. Those are the only things
in this platform that can say a collector or a cluster's membership job
has *stopped*, because a CronJob pod is never scraped. Beside them:
`server_scan_policy_active{policy_key}` says *what* is wrong across the
fleet, `server_scan_servers_partial` says which collector is reaching
servers but not fully reading them, and
`server_scan_collector_last_run_*` carry each collector's most recent
run — duration, servers fetched, errors, and whether it exited PARTIAL —
so a degraded run shows up in minutes rather than after the stale window.

The chart ships the two Prometheus Operator objects for them:

```yaml
metrics:
  serviceMonitor:
    enabled: true          # scrapes <release>-api's Service, path /metrics
  prometheusRule:
    enabled: true          # ServerScanCollectorSilent, ServerScanServersStale,
                           # ServerScanClusterSilent, ServerScanFleetSnapshotFailing
    staleServersThreshold: 10
    silentForSeconds: 43200
```

Both are **off by default** because they need the `monitoring.coreos.com`
CRDs, which vanilla Kubernetes lacks. On OpenShift the CRDs always exist —
but **user-workload monitoring is off by default**, and a ServiceMonitor
nothing scrapes is the same as none. Three things have to be true on the
cluster, none of which this chart can do for you because they live in the
`openshift-monitoring` namespaces:

1. **Enable user-workload monitoring**, once per cluster. The chart can
   do this for you — `metrics.userWorkloadMonitoring.enabled: true`
   writes the `cluster-monitoring-config` ConfigMap into
   `openshift-monitoring` — so that a single `helm install` of this chart
   is enough. It is off by default because it is a cluster-wide setting
   written from an application chart into a namespace it does not
   otherwise own, and because a cluster that already has that ConfigMap
   collides: label the existing one with `app.kubernetes.io/instance:
   <release>` so Argo adopts it, or delete it just before the sync.

   By hand, it is this:

   ```yaml
   apiVersion: v1
   kind: ConfigMap
   metadata:
     name: cluster-monitoring-config
     namespace: openshift-monitoring
   data:
     config.yaml: |
       enableUserWorkload: true
   ```

   Either way, the `openshift-user-workload-monitoring` namespace then
   gains a Prometheus that scrapes every `ServiceMonitor` in every user
   namespace, and the metrics appear under **Observe → Metrics** in the
   console with the `server-scan` project selected.

2. **Remote-write to your own Prometheus/Thanos**, if you want them
   there too — same mechanism, the *user-workload* ConfigMap:

   ```yaml
   apiVersion: v1
   kind: ConfigMap
   metadata:
     name: user-workload-monitoring-config
     namespace: openshift-user-workload-monitoring
   data:
     config.yaml: |
       prometheus:
         remoteWrite:
           - url: "https://thanos-receive.example.com/api/v1/receive"
             # writeRelabelConfigs keeps the payload to this app; drop it
             # to forward every user-workload metric on the cluster.
             writeRelabelConfigs:
               - sourceLabels: [__name__]
                 regex: "server_scan_.*|http_request.*|cache_operations_total|mongo_ping_failures_total"
                 action: keep
   ```

   Auth (`basicAuth`, `authorization`, `tlsConfig`) goes under the same
   entry, per the OpenShift docs for `remoteWrite`; the Secret it references
   must be in `openshift-user-workload-monitoring`.

3. **Nothing on the ServiceMonitor side.** OpenShift's user-workload
   Prometheus needs no selector label, which is why
   `metrics.serviceMonitor.labels` is empty by default.

On the sandbox, step 1 is the chart's (`redbull-platform` sets
`metrics.userWorkloadMonitoring.enabled: true`); step 2 is not configured.

**Before pushing a change to this chart, dry-run it against the cluster:**

```bash
helm template server-scan deploy/helm/server-scan -n server-scan \
  --set metrics.prometheusRule.enabled=true | oc apply --dry-run=server -f -
```

`helm lint` and `helm template` check syntax; the server-side dry-run
runs every admission webhook, including OpenShift's PromQL parser for
`PrometheusRule`s. A missing value renders as nothing and passes the
first two — it has — and only the webhook catches it.

**Query the `server_scan:` recording rules, not the raw gauges.** Every
API replica exports the same fleet-wide gauges, so the raw series come
back once per pod. The `PrometheusRule` records one de-duplicated series
per gauge — `server_scan:servers_stale:max`, `server_scan:servers:max`,
`server_scan:servers_by_health:max`, and so on — plus two ages that need
no arithmetic, `server_scan:collector_silent_seconds` and
`server_scan:cluster_silent_seconds`. Type `server_scan:` in Observe →
Metrics and autocomplete lists them. The alerts read these too; an alert
on a raw series would fire once per replica.

**Reading the alerts.** `ServerScanCollectorSilent` means a CronJob is not
producing fresh servers at all — check `oc get jobs` and the newest pod's
logs. `ServerScanServersStale` means it *is* running but some servers are
not being read; `server_scan_servers_unreachable` on the same label says
how many of those are a BMC that did not answer (the rest are rejected
logins or hosts the manager dropped). `ServerScanClusterSilent` is the one
that matters most for the membership jobs: without it a cluster that
stopped reporting leaves its servers `INSTALLED` forever.

## Collectors (CronJobs)

Real vendor collectors run as Kubernetes `CronJob`s, one per manager
*type*, invoking `tools/run_collector.py --manager-type <TYPE>` in the
same image as the API (`Containerfile` copies `tools/` alongside `app/`
specifically so no second image is needed). See the repo root
`README.md`'s "How data actually gets in" section for the full design,
`docs/adr/0009-ucs-manager-collector.md` for how the UCS Manager data
path was built and validated, and `docs/adr/0014-ucs-central-multi-
domain-collector.md` for how the Cisco collector drives it per domain.

A collector's entire connection config is one endpoint and one login per
manager type, set in `collectors.<vendor>` in `values.yaml`. There are no
`Manager` documents to create first and no credentials volume to mount.

### The membership jobs are a different chart

`deploy/helm/openshift-membership` is a **separate chart**, not part of
this one, because its jobs run inside every OpenShift cluster rather than
beside the API. One release per cluster, deployed by ArgoCD: a UPI
cluster sets `nodes.enabled` + `nodes.clusterName`, an MCE hub also sets
`agents.enabled` + `agents.mceName`. Both names are Helm `required`, so a
misconfigured release fails at template time instead of running a job
that exits 2 every 15 minutes.

Each cluster needs network to this platform's MongoDB and a copy of the
`mongo-uri` and `cursor-secret` Secrets — the jobs write directly, like
every collector here, and never call the API. That chart's own README has
the values; `docs/adr/0024-openshift-cluster-membership.md` has the
design.

`collectors.timeZone` (default `Asia/Jerusalem`) sets every CronJob's
`spec.timeZone` (Kubernetes 1.27+), so a schedule like `"0 2 * * *"` fires
at 2am local time, DST included, rather than 2am on whatever timezone the
cluster's `kube-controller-manager` happens to run — almost always UTC,
regardless of where the cluster physically sits. Set it to `""` to fall
back to the cluster default instead.

`collectors.jobTtlSeconds` (default `28800`, 8 hours) sets every
CronJob's `jobTemplate.spec.ttlSecondsAfterFinished`: a finished Job (and
its Pod) is deleted that many seconds after it completes, success or
failure. This is independent of, and in addition to, the count-based
`successfulJobsHistoryLimit: 3`/`failedJobsHistoryLimit: 5` every
CronJob template also sets — whichever cleanup condition is met first
wins. Every collector here defaults to the same `"0 */6 * * *"` schedule
(2026-09-10, at the operator's request — a uniform cadence across the
whole fleet), so 8 hours keeps at most two finished Jobs visible at once
per collector: the just-finished one and the previous one, not yet
TTL'd. Set it to `0` to
disable and rely on the count limits alone.

`collectors.ucsManager` is the one carve-out and has no `ip` at all: the
UCS Central collector reads every domain's address from Central at
runtime (`ComputeSystem.address`) and logs into each one with
`collectors.ucsManager.username`/`.password`, so that account has to
authenticate against every registered domain. There is no UCS Manager
CronJob to enable.

Those values render into a single `Secret`
(`templates/collector-credentials-secret.yaml`) and reach the pod as
`INVENTORY_*` environment variables via `envFrom`.

**Do not commit real passwords to `values.yaml`.** Pass them at install
time (`--set collectors.ucsManager.password=...`), from a values file kept
out of git (`-f secrets.yaml`), or — for production — set
`collectors.existingSecret` to a Secret managed by Vault, External
Secrets or sealed-secrets, which makes the chart skip rendering its own.

An externally managed Secret is consumed with `envFrom`, so its keys are
environment variable names and the chart cannot validate them — a typo
surfaces as "not configured" at collector runtime, not at install. For
the Cisco collector the required set is:

```
INVENTORY_UCS_CENTRAL_IP
INVENTORY_UCS_CENTRAL_USERNAME
INVENTORY_UCS_CENTRAL_PASSWORD
INVENTORY_UCS_MANAGER_USERNAME     # no _IP — see the carve-out above
INVENTORY_UCS_MANAGER_PASSWORD
```

Other vendors follow the `INVENTORY_<TYPE>_IP`/`_USERNAME`/`_PASSWORD`
shape; `templates/collector-credentials-secret.yaml` is the full list.

`envFrom` a Secret rather than inline `env` values is deliberate:
`kubectl get cronjob -o yaml` and `kubectl describe pod` both print plain
`env` values to anyone who can read workloads in the namespace, while a
`secretRef` shows only the reference.

`UCS_CENTRAL` covers the whole Cisco fleet — every domain registered with
Central, read through that domain's own UCS Manager. `OPENMANAGE`,
`INTERSIGHT`, `ONEVIEW` and `REDFISH_STANDALONE` each have a CronJob of
their own, all shipped disabled.

A collector pod's exit code is the only signal a CronJob carries: `0`
complete, `1` the manager failed outright (`FAILED (see logs)`), `2` not
configured — the message names the `INVENTORY_*` variables to set, and
is checked before any connection is opened, so a half-configured Secret
never reads as a fleet of bad passwords — and `3` PARTIAL, some servers
written but not the whole fleet. A BMC that did not answer or rejected
the login is printed and logged at ERROR but exits `0`, not `3` — on a
real fleet those happen every run, and PARTIAL had stopped meaning
anything (ADR-0016's 2026-09-10 updates). TLS failures, a per-host time
budget exceeded and anything unrecognized still exit `3`. Each run also
writes its outcome onto the `Manager` document, which is what the
`server_scan_collector_last_run_*` gauges above read.

`collectors.fake` is the sixth CronJob and the one that reaches no vendor
at all: it runs `tools/seed_inventory.py`, for a cluster with no UCS,
OneView, OME, Intersight or BMC to talk to — a demo, a UI environment, or
a soak test of the ingest path itself. It is not a shortcut around the
pipeline; the seeder drives the same `ProviderServer` -> classify ->
health-evaluate -> audit -> upsert path a real collector does.

`count` and `seed` together decide the generated fleet field for field, so
repeated runs at the same pair upsert the same servers, which is what
makes it safe to schedule. **Changing either against a populated database
reports errors rather than replacing the fleet** — servers correlate on
`(vendor, serial)`, so a new seed is a second fleet. Wipe the database
first. And never enable it alongside a real collector: one estate, two
sources of truth.

`ONEVIEW` is one appliance like the rest. Power supplies and CPU thread
counts are its two potentially-per-server costs — each tried the cheap
way first (most servers' bulk sweep already carries both), falling back
to one request per server for whatever it doesn't. Every other collector
reads both out of a response it already fetches for other reasons;
OneView is the only one where either can cost something extra.
`collectors.oneview.collectPsus: false` and
`collectors.oneview.collectCpuThreads: false` turn each off
independently — the rest of the sweep is three bulk calls either way.

Note that Intersight's three fields mean something different: it signs
requests with an API key rather than logging in, so `username` is the API
Key ID and `password` the secret key.

### `REDFISH_STANDALONE`'s inventory

Unlike every other collector, this one's fleet list is a file
(`docs/examples/redfish-inventory.example.toml` shows the shape), not
values you `--set`, because at a few hundred hosts it doesn't fit
`values.yaml` or `--set` sanely. `collectors.redfishStandalone` offers the
same choice `collectors.<vendor>.password` above already does for
credentials:

- Leave `inventoryToml` blank (the default) and provision the
  `<release>-redfish-inventory` ConfigMap yourself — `kubectl create
  configmap <release>-redfish-inventory
  --from-file=inventory.toml=./redfish-inventory.toml`, or your own
  GitOps tooling. This chart never creates or touches that ConfigMap.
- Set `inventoryToml` (a multi-line string) and this chart renders and
  owns the ConfigMap instead — no separate `kubectl` step. For Argo CD
  this is the natural fit: `spec.source.helm.values` on the Application
  is inline YAML anyway, so the fleet list lives in whatever repo that
  Application manifest does. Give it the same access control as any
  other GitOps-committed config: it names every BMC and decides which
  credential each one receives, which is why the example file calls it
  "equivalent to write access to the credential Secret" even though it
  holds no password itself.

## Current state

The backend API and the frontend both have full manifests
(Deployment/Service/Route, plus the API's ConfigMap). The frontend gap
this section used to record is closed as of chart 0.2.0.

CI does now build and publish both images to GHCR on every push to main
(`docs/adr/0010-image-publishing-and-versioning.md`), but nothing
*deploys* them: there is no CD/GitOps wiring and no automatic manifest
update, tracked as pending work in `CLAUDE.md`.

`deploy/helm/openshift-membership` is the exception in one respect: it is
written to be pointed at by an ArgoCD `Application` per cluster, so its
per-cluster values are the only thing that differs between releases.

## Configuration notes

Two traps in `backend/app/config/settings.py` that the variable list in
`.env.example` cannot express, recorded here (2026-09-13, moved out of
the settings module's comments) because each cost a session:

- **A field's name is its environment variable.** pydantic-settings
  derives `INVENTORY_<FIELD>` from the field name with no alias, and
  `extra="ignore"` means a variable nothing reads never raises. The GPU
  catalog field originally shipped as `gpu_model_catalog`, so the
  documented `INVENTORY_GPU_MODELS` was silently a no-op until the field
  was renamed — confirmed live, not by reading: `Settings()` returned the
  `INVENTORY_GPU_MODEL_CATALOG` value and ignored `INVENTORY_GPU_MODELS`.
  A new field's name must match its documented variable letter for
  letter, and `.env.example`, the Helm values and the field are the three
  places to check.
- **`INVENTORY_ENVIRONMENT=production` refuses the committed cursor
  secret.** `cursor_secret` used to be "only a code comment, not enforced
  at startup": an install that forgot `backend.cursorSecret` came up
  healthy and stayed on the dev default forever. A forged cursor is not a
  disclosure risk while every endpoint is open (CLAUDE.md convention 6),
  but it becomes one the moment authentication lands, and rotating a
  secret every deployed cursor already depends on is worse than failing
  startup now. A blank value fails the same way as the default: a
  `secretKeyRef` to an empty key still counts as "set" to
  pydantic-settings, unlike leaving the variable out, so it is a separate
  mistake with the same fix.
