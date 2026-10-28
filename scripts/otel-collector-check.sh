#!/usr/bin/env bash
# Checks around the OTel Collector's deploy in slipstream (`just obs-up`). preflight
# passes once what the Collector reads at start exists: the grafana-cloud-endpoints
# ConfigMap the eks stack creates and the grafana-cloud-write Secret ESO syncs. Kubernetes
# checks neither until the pod starts, and a pod that starts without them waits in
# CreateContainerConfigError until the rollout times out. rollout waits for the
# Deployment to roll out and, when it does not, prints why each Collector pod waits.
# Usage: otel-collector-check.sh preflight|rollout
set -euo pipefail

namespace=slipstream

preflight() {
  if ! kubectl -n "${namespace}" get configmap grafana-cloud-endpoints -o name >/dev/null; then
    echo "the grafana-cloud-endpoints ConfigMap is not in ${namespace}: run \`just cluster-up\`, which creates it from the grafana stack's outputs" >&2
    return 1
  fi
  if ! kubectl -n "${namespace}" wait --for=condition=Ready externalsecret/grafana-cloud-write --timeout=120s >/dev/null; then
    echo "the grafana-cloud-write Secret has not synced from Secrets Manager: see why in \`kubectl -n ${namespace} describe externalsecret grafana-cloud-write\`; if the write token was never stored, run \`just grafana-cloud-setup\`" >&2
    return 1
  fi
}

rollout() {
  if kubectl -n "${namespace}" rollout status deploy/otel-collector --timeout=120s; then
    return 0
  fi
  # A container that cannot start waits with a reason and a message naming the cause,
  # such as CreateContainerConfigError and the ConfigMap key or Secret it lacks.
  echo "the Collector did not roll out; its pods wait on:" >&2
  kubectl -n "${namespace}" get pods -l app=otel-collector -o jsonpath='{range .items[*]}{.metadata.name}{": "}{range .status.containerStatuses[*]}{.state.waiting.reason}{" "}{.state.waiting.message}{end}{"\n"}{end}' >&2 ||
    echo "(could not read the Collector pods' status)" >&2
  echo "see the pod's events in \`kubectl -n ${namespace} describe pod -l app=otel-collector\`" >&2
  return 1
}

case "${1:-}" in
preflight) preflight ;;
rollout) rollout ;;
*)
  echo "usage: otel-collector-check.sh preflight|rollout" >&2
  exit 2
  ;;
esac
