# ADR-0055: admins run a collector or prune CronJob from the UI as a real Job

Status: accepted (2026-11-04)

## Context

An operator who wants a collector run now (after fixing a credential, or to see a new server) has to
`oc create job --from=cronjob/<name>`. The ask: a "Run job" button in the UI that behaves the same, so the
admin sees the real Job, its pods and their logs, and nothing runs inside the API process.

## Options

1. **The API creates a Job through the Kubernetes API** (chosen). The Job is built from the CronJob's
   `spec.jobTemplate`, so it runs the same image, env, Secrets and limits as the scheduled one.
2. In-process run in the API pod: rejected. No pod or logs to inspect, and the collector credentials
   would be read by the Route-exposed API pod (they are deliberately not, except those ADR-0032 needs).
3. A separate runner service: rejected. Another Deployment, another image and RBAC, for one button.
4. A console deep link to the CronJob page: rejected for this scope (the admin would still need cluster
   access and would see none of it in the UI).

## Decision

- **Client: plain `httpx`** against `https://$KUBERNETES_SERVICE_HOST` with the ServiceAccount token (re-read
  on each request, since it rotates) and the cluster CA. `httpx` is already pinned; the official `kubernetes`
  and `kubernetes-asyncio` clients would add a large dependency tree, two generated model layers and a fresh
  mirror entry for six REST calls (list CronJobs, list Jobs, list Pods, get Job/Pod, create Job, read log).
- **The Job is what `kubectl create job --from=cronjob/X` builds**: `jobTemplate` labels, annotations and spec
  copied, the CronJob as controller `ownerReference`, `cronjob.kubernetes.io/instantiate: manual`, and the
  name `<cronjob>-manual-<6 chars>` cut to fit 63 characters. A `server-scan.io/triggered-by` annotation
  names the actor on the Job itself. The job runs exactly as scheduled (no forced dry run).
- **Allow-list by label, in this namespace only.** A CronJob is triggerable only with the label
  `server-scan.io/triggerable: "true"`, which the eight `server-scan` chart CronJobs (six collectors, two
  prunes) carry. The API never accepts a job spec. A 404 answers any other name. Logs are served only along
  the chain triggerable CronJob -> a Job it owns (ownerReference name and uid) -> a pod that Job owns.
- **The `nodes-status` chart is excluded on purpose**: its jobs live in other clusters and releases, are not
  labelled, and a Role in this namespace cannot reach them.
- **409 while a CronJob has a running Job**, scheduled or manual (any Job it owns that is `active` or still `pending`, i.e. without a Complete or Failed condition; the Job is created without `blockOwnerDeletion`, as kubectl does, so no `cronjobs/finalizers` RBAC is needed); the
  list marks it `active` so the UI disables the button. Two admins clicking in the same second can still both
  pass the check (no lock; the CronJobs run `concurrencyPolicy: Forbid`, which a manual Job bypasses).
- **Admin only on every route, reads included** (`require_admin` on the router): logs and Job names are
  operational detail. A personal token is never `ADMIN` (ADR-0053), so it gets 403.
- **Audit**: `JOB_TRIGGERED` (`cronjob`, `job`, `namespace`) is recorded after the Job is created. If the
  audit write fails the Job still exists but is unrecorded in `audit_events`; its annotation is the fallback.
- **Confirm dialog with a warning** for the collector that can restart an iDRAC (ADR-0050) and for the two
  prunes; the text is the CronJob's annotation `server-scan.io/trigger-warning`, so the chart owns it.
- **Backends**: `INVENTORY_JOBS_BACKEND` = `kubernetes`, `fake` or `off`. Unset: `fake` in development and
  test, `off` in staging and production, so a production API with the feature disabled never shows fake data.
  The chart sets `kubernetes` and `INVENTORY_JOBS_NAMESPACE` (downward API) when `jobTrigger.enabled`.
  The fake gives eight CronJobs with history, a failed Intersight run, and a trigger that is active for 20 s
  and then succeeds with growing log lines.

## Security trade-off

The Route-exposed API pod now carries a ServiceAccount token and a namespaced Role: CronJobs get/list; Jobs
get/list/create; pods get/list; pods/log get. Kubernetes RBAC cannot restrict `create` on Jobs by label or by
spec, so the allow-list is enforced in our code, not by the cluster. An attacker who gets code execution in
the API pod could create any Job in this namespace (with its ServiceAccounts and mounted Secrets). The
mitigations are: off by default (`jobTrigger.enabled: false`), nothing cluster-wide, admin-only endpoints, the
namespace's own pod-security admission, and the Job pods keep `automountServiceAccountToken: false`.
Enable it only where that trade is accepted.

## Which jobs are switched off (2026-11-04 update)

A collector with `enabled: false` renders no CronJob, so the cluster cannot say it exists. With
`jobTrigger.enabled` the chart also renders a `<release>-jobs-catalog` ConfigMap (`catalog.json`: every
triggerable CronJob name and its `values.yaml` flag), mounted at `/etc/server-scan/jobs` and read per
request (`INVENTORY_JOBS_CATALOG_FILE`), so a values change shows without a pod restart. The page shows
`enabled: true|false` on each card; a catalog entry with no CronJob is listed as not deployed and cannot be
run. A missing or unreadable catalog means every CronJob found counts as enabled.

## Consequences

The chart gains a ServiceAccount, Role and RoleBinding (only when enabled) and a label on eight CronJobs. The
page is `/jobs` in the UI. A manual Job is subject to the CronJob's history limits like a scheduled one.
