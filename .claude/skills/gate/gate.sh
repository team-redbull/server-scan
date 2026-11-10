#!/usr/bin/env bash
# The CI gate, locally, in CI's order. Every step runs; the exit code is
# the first failure. `--backend`, `--helm`, `--frontend` pick a subset.
set -uo pipefail
cd "$(git rev-parse --show-toplevel)"

want_backend=1 want_helm=1 want_frontend=1
if [ $# -gt 0 ]; then
  want_backend=0 want_helm=0 want_frontend=0
  for arg in "$@"; do
    case "$arg" in
      --backend) want_backend=1 ;;
      --helm) want_helm=1 ;;
      --frontend) want_frontend=1 ;;
      *) echo "unknown flag: $arg (use --backend, --helm, --frontend)" >&2; exit 64 ;;
    esac
  done
fi

status=0
# CI's deploy job renders THIS repo's templates with the platform repo's own
# hand-maintained values.yaml (only templates/ and files/ are synced), so a new
# value a template reads must already be tolerated there. Same steps, locally.
deploy_render() {
  local work chart status=0
  work=$(mktemp -d)
  gh repo clone team-redbull/redbull-platform "$work/gitops" -- --depth 1 --quiet || status=1
  chart="$work/gitops/gitops/charts/server-scan"
  if [ "$status" = 0 ]; then
    rsync -a --delete deploy/helm/server-scan/templates/ "$chart/templates/" &&
      if [ -d deploy/helm/server-scan/files ]; then
        rsync -a --delete deploy/helm/server-scan/files/ "$chart/files/"
      else
        rm -rf "$chart/files"
      fi &&
      (cd "$chart" && helm lint . && helm template server-scan . -n server-scan > /dev/null) ||
      status=1
  fi
  rm -rf "$work"
  return $status
}

step() {
  local name=$1; shift
  echo "==> $name"
  if "$@"; then echo "    ok"; else echo "    FAILED: $name"; status=1; fi
}

if [ "$want_backend" = 1 ]; then
  step "ruff check" uv run ruff check .
  step "ruff format --check" uv run ruff format --check .
  step "ty" uv run ty check backend/app tools tests
  step "comment density" uv run python scripts/check_comment_density.py
  step "import-linter" uv run lint-imports
  # CI's `lint` job also audits dependencies. These need the network and fail
  # on an advisory published AFTER your last green run, with no code change.
  step "pip-audit" uv run --with pip-audit pip-audit --skip-editable
  step "deptry" uv run --with deptry==0.24.0 deptry . --known-first-party app
fi

if [ "$want_helm" = 1 ]; then
  for chart in deploy/helm/*/Chart.yaml; do
    step "helm lint $(dirname "$chart")" helm lint "$(dirname "$chart")"
  done
  step "helm template server-scan (defaults, no collector)" \
    sh -c '! helm template ci-lint deploy/helm/server-scan | grep -q "name: ci-lint-collector-"'
  step "helm template server-scan (frontend + metrics)" \
    sh -c 'helm template ci-lint deploy/helm/server-scan \
      --set frontend.enabled=true --set route.host=scan.apps.example.com \
      --set metrics.serviceMonitor.enabled=true --set metrics.prometheusRule.enabled=true \
      --set metrics.userWorkloadMonitoring.enabled=true > /dev/null'
  step "helm template server-scan-collectors (every collector)" \
    sh -c 'helm template ci-lint deploy/helm/server-scan-collectors \
      --set collectors.redfishStandalone.enabled=true \
      --set collectors.redfishStandalone.credentialsFile=files/redfish/credentials.toml \
      --set collectors.ucsCentral.enabled=true --set collectors.intersight.enabled=true \
      --set collectors.ome.enabled=true --set collectors.oneview.enabled=true \
      --set collectors.kubevirt.enabled=true --set collectors.kubevirt.clusterName=ci-cluster \
      --set collectors.kubevirt.caBundleConfigMap=kv-ca \
      --set collectors.fake.enabled=true > /dev/null'
  step "helm template server-scan-collectors (hub)" \
    sh -c 'helm template ci-lint deploy/helm/server-scan-collectors \
      --set config.existingConfigMap=server-scan-api-config \
      --set collectors.ucsCentral.enabled=true --set collectors.ome.enabled=true \
      --set collectors.kubevirt.enabled=true --set collectors.kubevirt.clusterName=ci-hub > /dev/null'
  step "helm template server-scan-collectors (kubevirt cluster)" \
    sh -c 'helm template ci-lint deploy/helm/server-scan-collectors --set triggerable=false \
      --set collectors.kubevirt.enabled=true --set collectors.kubevirt.clusterName=ci-cluster \
      --set collectors.kubevirt.url=https://kubevirt-redfish.apps.example.com \
      --set db.mongoUri=mongodb://mongo.example.com:27017 \
      --set-string db.cursorSecret=ci-cursor-secret-not-real > /dev/null'
  step "helm template server-scan-collectors (defaults render nothing)" \
    sh -c 'out=$(helm template ci-lint deploy/helm/server-scan-collectors) && [ -z "$(printf %s "$out" | tr -d "[:space:]")" ]'
  step "helm template server-scan-collectors (kubevirt needs clusterName)" \
    sh -c '! helm template ci-lint deploy/helm/server-scan-collectors \
      --set collectors.kubevirt.enabled=true > /dev/null 2>&1'
  step "deploy render (templates + redbull-platform values)" deploy_render
  step "helm template nodes-status" \
    sh -c 'helm template ci-lint deploy/helm/nodes-status --set nodes.clusterName=ci-cluster > /dev/null && \
      helm template ci-lint deploy/helm/nodes-status --set nodes.clusterName=ci-cluster \
      --set agents.enabled=true --set agents.mceName=ci-mce > /dev/null'
fi

if [ "$want_frontend" = 1 ]; then
  step "npm audit" sh -c 'cd frontend && npm audit'
  step "npm run lint" sh -c 'cd frontend && npm run lint'
  step "npm run typecheck" sh -c 'cd frontend && npm run typecheck'
  step "vitest" sh -c 'cd frontend && npm run test -- --run'
  step "npm run build" sh -c 'cd frontend && npm run build'
  step "dark-only css (no prefers-color-scheme)" \
    sh -c '! grep -l prefers-color-scheme frontend/dist/assets/*.css'
fi

[ "$status" = 0 ] && echo "GATE PASSED" || echo "GATE FAILED"
exit $status
