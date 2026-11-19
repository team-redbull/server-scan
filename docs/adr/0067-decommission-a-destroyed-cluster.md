# ADR-0067: A destroyed cluster is retired by an admin, on a Clusters page

**Status:** Accepted, implemented 2026-11-19.

## Context

A membership job (`tools.collect_openshift`, ADR-0024) runs as a CronJob **inside** the cluster it reports, and only
that job frees the servers that name its cluster (or MCE). When the cluster is destroyed its job goes with it, so:

- its servers stay `INSTALLED` forever (and are never offered by `/servers/available`);
- its `membership_runs` record is only ever overwritten, never deleted, so its gauges stay in Grafana with an
  ever-older "last run", and `ServerScanClusterSilent` / `ServerScanMembershipRunSilent` fire for ever (indistinguishable
  from a broken CronJob, which is what teaches people to ignore them);
- nothing in the API releases them (deleting a server brings it back with the next collector run, ADR-0065).

## Decision

**A human says a cluster is gone; the platform makes that easy to see and one action to do.** Absence of evidence never
frees a server (ADR-0024's "two refusals"): freeing makes a server assignable, and an automatic timeout would, after a
long network break or a suspended job, hand out a node that is running.

- **Admin only on both routes, the list included** (the router carries `require_admin`, like `/jobs`, ADR-0055): a viewer, an
  auditor and a personal token get 403, the Clusters link is hidden and the page is behind `AdminGate`.
- **`GET /api/v1/openshift/clusters`**: one row per membership job *with a run record*: `kind`
  (`nodes`|`agents`), `name`, `held` (servers its job scope matches), `last_run_at`, `silent_seconds`, and `silent`
  when the last run is older than **`INVENTORY_OPENSHIFT_SILENT_AFTER_SECONDS` (default 604800, 7 days)**. Longest
  silent first. A hosted cluster held by a live MCE has no record of its own, so it is never listed (and never falsely
  "silent").
- **`POST /api/v1/openshift/clusters/{kind}/{name}/decommission`** (admin only): frees every server in that job's scope
  (`openshift.cluster_name` for `nodes`, `openshift.mce_name` for `agents`; the same filter the job reconciles with) by
  `OpenShiftMembershipService.release`, i.e. the job's own `_free`, one `OPENSHIFT_STATE_CHANGED` per server attributed to
  the admin; deletes the `kind:name` run record, so the gauges and both alerts disappear on the next gauge refresh;
  records one `CLUSTER_DECOMMISSIONED` event (`kind`, `name`, `freed`, `last_run_at`). 404 for a job with no record,
  422 for another `kind`. The list and detail caches are invalidated.
- **A `CLUSTER_NOT_REPORTING` event the first time a job passes the threshold.** A check in the API process
  (`INVENTORY_OPENSHIFT_SILENT_CHECK_SECONDS`, default 900, 0 disables; not tied to `/metrics` being scraped) runs
  `ClusterDecommissionService.notify_silent`: for each silent job it atomically sets `not_reporting_notified_at` on the run
  record (a filter on "silent and not yet marked", so several API replicas record one event, not several) and then records
  the event (`kind`, `name`, `held`, `last_run_at`, `silent_seconds`, actor `SYSTEM` / `cluster-watch`; no server). The next
  real run (`record_run`) clears the mark, so a cluster that returns and goes quiet again is reported again; a failed event
  write releases the mark so the next check retries. A job already silent when this ships is reported once, at the first check.
- **Self-correcting:** a cluster that was in fact alive claims its servers back and rewrites its record on its next run
  (a test covers it). Decommissioning is therefore not destructive, only premature.
- **UI:** a **Clusters** page (nav and page for admins only) with a type-the-name dialog that says how
  many servers become Available; a "not reporting for N days" banner (admins only: it asks the API nothing for anyone else) on the Sites page and on the detail page of a server
  whose own cluster or MCE is silent; the two silent alerts' descriptions point at the page.

## Not done

- An automatic release after N days of silence (rejected above; if ever added it should be report-only first, like the
  prune job, ADR-0037).
- Listing a cluster no job ever wrote a record for (a record exists from every real run since 2026-09-24).
- `INVENTORY_OPENSHIFT_SILENT_AFTER_SECONDS` is not a Helm value; the default applies unless the env var is set.
