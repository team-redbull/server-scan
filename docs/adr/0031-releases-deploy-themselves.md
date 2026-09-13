# ADR-0031: a release deploys itself by pinning redbull-platform's chart copy

Date: 2026-09-13
Status: Accepted

Extends ADR-0010 (images and versions are cut on every push to `main`)
to the step that was still manual: making the cluster run them.

## Context

Since ADR-0010 every green push to `main` tags a version and publishes
both images, but nothing deployed them. The platform is deployed by Argo
CD from `team-redbull/redbull-platform`, whose `gitops/charts/server-scan`
is a *copy* of this repo's `deploy/helm/server-scan` — templates and files
verbatim, `values.yaml` carrying that cluster's overrides — with the image
tag pinned by hand. Every release therefore ended with the same ritual:
`rsync` the templates, edit two `tag:` lines and `appVersion`, render,
commit, push. Done three times on 2026-09-13 alone, and forgettable:
the chart copy was two upstream releases behind at one point and the
`appVersion` had read `11.2.0` since long before the rename.

The org already automates this for its other services through
`team-redbull/.github`'s reusable `ghcr-build-push.yml`, which bumps the
gitops values with `yq` and pushes with a rebase-retry. It was
considered and not reused: it builds the image itself (this repo builds
two, with SHA-pinned actions per ADR-0013), it versions from
`<chart>/vX.Y.Z` tags in the *chart* repo (a second sequence beside
ADR-0010's Conventional-Commits one), and it bumps a single image path.

## Decision

A `deploy` job in `.github/workflows/ci.yml`, after `publish`, on `main`
only, borrowing the reusable workflow's mechanics and nothing else:

1. Check out `redbull-platform` with the org secret `REDBULL_WRITE_TOKEN`
   (the default `GITHUB_TOKEN` cannot write to another repo).
2. `rsync --delete` `templates/` and `files/` from this repo's chart into
   the copy — they are verbatim by design — and `sed` the three lines the
   release moves: the two `    tag:` lines in `values.yaml` and
   `Chart.yaml`'s `appVersion`, each followed by a check that exactly that
   many lines now carry the version. **`values.yaml` is edited by line,
   never replaced**: it is the cluster's override file. `yq -i`, which the
   org workflow uses, was tried and rejected — it re-emits the whole
   file, dropping every blank line and reflowing maps, a 74-line noise
   diff per release on a 41 KB hand-maintained file.
3. `helm lint` and `helm template` the result. A runner cannot reach the
   cluster, so this is not `oc apply --dry-run=server` — but a `required`
   value the gitops `values.yaml` does not set fails here, before Argo
   (the class of error that shipped red on 2026-09-13).
4. Commit scoped to `gitops/charts/server-scan` as `github-actions[bot]`
   — the author every other service's bump already carries in that repo;
   the "operator is the only visible contributor" rule is this repo's —
   and push with a rebase-retry, because every service pushes to the
   same branch.

`publish` exposes `steps.version.outputs.version` as a job output for it.

## Consequences

- A `feat:` that changes the chart deploys itself; a value override a new
  template needs is still a human edit of the gitops `values.yaml`, and
  the render step fails loudly until it is made.
- The server-side dry-run stays a habit for chart changes, not a gate —
  the runner cannot perform it.
- The first release under this job is the first one that deployed
  itself.
