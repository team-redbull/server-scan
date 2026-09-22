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
fi

if [ "$want_helm" = 1 ]; then
  for chart in deploy/helm/*/Chart.yaml; do
    step "helm lint $(dirname "$chart")" helm lint "$(dirname "$chart")"
  done
  step "helm template server-scan (defaults)" \
    sh -c 'helm template ci-lint deploy/helm/server-scan > /dev/null'
  step "helm template server-scan (frontend + metrics)" \
    sh -c 'helm template ci-lint deploy/helm/server-scan \
      --set frontend.enabled=true --set route.host=scan.apps.example.com \
      --set metrics.serviceMonitor.enabled=true --set metrics.prometheusRule.enabled=true \
      --set metrics.userWorkloadMonitoring.enabled=true > /dev/null'
  step "helm template server-scan (every collector)" \
    sh -c 'helm template ci-lint deploy/helm/server-scan \
      --set collectors.redfishStandalone.enabled=true \
      --set collectors.redfishStandalone.credentialsFile=files/redfish/credentials.toml \
      --set collectors.ucsCentral.enabled=true --set collectors.intersight.enabled=true \
      --set collectors.openmanage.enabled=true --set collectors.oneview.enabled=true \
      --set collectors.fake.enabled=true > /dev/null'
  step "helm template nodes-status" \
    sh -c 'helm template ci-lint deploy/helm/nodes-status --set nodes.clusterName=ci-cluster > /dev/null && \
      helm template ci-lint deploy/helm/nodes-status --set nodes.clusterName=ci-cluster \
      --set agents.enabled=true --set agents.mceName=ci-mce > /dev/null'
fi

if [ "$want_frontend" = 1 ]; then
  step "npm run lint" sh -c 'cd frontend && npm run lint'
  step "npm run typecheck" sh -c 'cd frontend && npm run typecheck'
  step "vitest" sh -c 'cd frontend && npm run test -- --run'
  step "npm run build" sh -c 'cd frontend && npm run build'
  step "dark-only css (no prefers-color-scheme)" \
    sh -c '! grep -l prefers-color-scheme frontend/dist/assets/*.css'
fi

[ "$status" = 0 ] && echo "GATE PASSED" || echo "GATE FAILED"
exit $status
