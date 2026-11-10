# ADR-0060: Collectors get their own chart; a KubeVirt collector runs in every KubeVirt cluster

**Status:** Accepted, implemented 2026-11-10.

**Changes** ADR-0031 (what CI pins), ADR-0032 (where the API's recheck credentials come from), ADR-0055 (which
CronJobs the Jobs page lists), ADR-0058 §9 (where the `KUBEVIRT` CronJob lives) and ADR-0059 (the cluster name
becomes required).

## Context

`deploy/helm/server-scan` deployed the API, the UI and every collector CronJob in one release, so a collector
upgrade was a platform upgrade and the other way round. CI pins that release to every green push (ADR-0031).

KubeVirt adds a second reason. The vendor managers are reachable from the hub, but VMs are not: each OpenShift
Virtualization cluster serves its own VMs through its own kubevirt-redfish facade (ADR-0058), and nothing
aggregates them. So a `KUBEVIRT` collector has to run in every KubeVirt cluster, the hub included. The rest of the
`KUBEVIRT` path assumed one facade:
- `manager_for` gave every KubeVirt collector the same `Manager` id, `mgr_kubevirt`, so each cluster's run would
  overwrite the others' `last_run`;
- the `collector_last_run_*` gauges were labelled by `source_provider` only;
- the API pod's live recheck had a single `INVENTORY_KUBEVIRT_URL`. For another cluster's VM it would read the wrong
  facade: a 404, or under the `legacy` id convention a different VM with the same name.

## Decision

1. **Two charts, one image.** `deploy/helm/server-scan` is the platform: API, UI, MCP, Routes, monitoring, the job
   trigger, and the two prune CronJobs.
   - **The prunes stay** because they are hub maintenance, use Redis, and must not run once per cluster.
   - `deploy/helm/server-scan-collectors` holds every collector CronJob, the collector credentials and the Redfish
     inventory, each switched on in `collectors.<type>`.
   - **Both run `server-scan-api`.** A collector is that image with another entrypoint, and both sides share the
     domain model and the Mongo schema, so a second image would add a build and a mirror without decoupling
     anything.
2. **The collectors chart is pinned by hand, after server-scan.** CI's deploy job keeps syncing and pinning only
   `server-scan`. A collectors release's `image.tag` (and the redbull-platform chart copy's `templates/`/`files/`) is
   changed deliberately.
   - **The order matters:** upgrade server-scan first. Both write the same documents, and an older API cannot load a
     value a newer collector writes (ADR-0058's rollback note is that case).
   - **This is the independence the split exists for:** a UI fix no longer restarts every cluster's collectors.
3. **The chart has two deployment shapes.**
   - **Hub:** in the platform's namespace, consuming server-scan's `api-config` ConfigMap
     (`config.existingConfigMap`), `server-scan-db` and the cursor Secret. Sites can then never drift between API and
     collectors (arc42 §7), and the two releases never own the same Secret. Its CronJobs keep
     `server-scan.io/triggerable`.
   - **A KubeVirt cluster:** only `collectors.kubevirt`, with its own `config`, its own `db.mongoUri`/`cursorSecret`
     Secrets and `triggerable: false`. Like `nodes-status` (ADR-0024), it writes the hub's MongoDB directly and needs
     network to it.
   - **What the chart leaves out:** every collector sets `INVENTORY_AUTH_ENABLED=false` and mounts no auth Secret
     (the setting only gates the session-secret check, and the hub's shared ConfigMap may enable it), and no Redis
     (no collector uses it).
4. **A KubeVirt manager is per cluster.**
   - The id is `mgr_kubevirt_<slug>` and the name `kubevirt-<slug>`, where the slug is
     `INVENTORY_KUBEVIRT_CLUSTER_NAME` lowercased with every run outside `[a-z0-9-]` made one `-`
     (`app.domain.models.manager.kubevirt_cluster_slug`).
   - A blank cluster name makes `KUBEVIRT` "not configured" (`EnvConnectionResolver`, exit 2), and the chart
     `required`s it.
   - Every other type keeps one manager per type.
   - **No migration:** a VM moves to its new id on its next collection (`manager_id`, plus a new `external_ids`
     key; the old key is inert). The first KubeVirt run on the new image deletes the old `mgr_kubevirt` document,
     which would otherwise block pruning and keep the stale-run alert firing for ever.
5. **The run gauges carry a `manager` label**, and `ServerScanCollectorRunStale` alerts per manager, on
   `server_scan:collector_last_run_age_seconds:by_manager` against `silentForSeconds`. The `source_provider`
   recording rules are unchanged; without the new rule a dead cluster hides behind a live one.
6. **The hub API never live-rechecks a VM.** It is given no KubeVirt cluster name, so `KUBEVIRT` is unconfigured
   there and `/servers/available` serves a VM from Mongo ("no recheck ran").
   - **Guard:** a KubeVirt `get_one` asked about a VM whose `external_id` names another facade raises
     `NotThisManagersServerError`, and the service answers from Mongo with outcome `skipped_foreign`. It never reads
     the wrong facade. `skipped_foreign` is not a failure in `server_scan:dependency_failure_ratio:rate10m`.
   - The vendor rechecks (ADR-0032) still work: the API pod mounts the collectors release's credentials Secret,
     `recheck.credentialsSecret`, with `optional: true`, so a missing release means "trust Mongo", not a failed pod.
     The per-type timeouts moved to `recheck.timeouts`.
7. **The Jobs page lists what it finds.** server-scan's catalog now names only the prunes: it cannot read another
   release's values. The hub collectors release's CronJobs still appear, because the API lists every
   `triggerable` CronJob in its namespace. A collector switched off simply does not appear, where it used to show as
   "not deployed". A KubeVirt cluster's CronJob is never reachable from the hub (the ADR-0055 reason
   `nodes-status` is excluded).

## Consequences

- **Pruning is held back by any dead cluster.** `tools.prune_servers` requires every enabled manager of a type to
  have a recent, clean run, so one cluster whose collector stopped skips the whole `KUBEVIRT` prune instead of
  deleting that cluster's VMs. To retire a cluster, delete its release and then its manager:
  `db.managers.deleteOne({_id: "mgr_kubevirt_<slug>"})`.
- **Cutover is a scheduled gap, not data loss.** The server-scan upgrade removes the old collector CronJobs, and
  collection resumes when the hub collectors release syncs. Create that Argo Application before merging, and copy
  the `collectors.*` provider blocks from server-scan's values into it.
- **A renamed facade Route** makes a VM's stored `external_id` "foreign" until its next collection, so for up to one
  collector interval it is served from Mongo.
- **Residual identity risk:** a facade that does not report `SerialNumber` falls back to the System `Id`, the bare
  VM name under the `legacy` convention, and correlation is `(vendor, serial)`. Two same-named VMs in two clusters
  would then merge into one document. The fork reports the firmware serial, which KubeVirt randomizes per VM.
- **The two charts must be compatible across a version gap.** A collectors release may run an older tag than the
  API for as long as the operator leaves it there; that is safe in the "older writer" direction only.

## Alternatives considered

- **A separate collectors image.** It decouples nothing the tag does not, and costs a second build, scan and mirror.
  Worth revisiting only to keep vendor SDKs out of the API image.
- **One hub collector reading every facade** (a list of URLs). It needs a Route and credentials per cluster in the
  hub, plus network from the hub to every cluster's ingress, and it still needs per-cluster manager identity.
- **CI pinning both charts.** That keeps today's coupling: every release restarts every cluster's collectors.
