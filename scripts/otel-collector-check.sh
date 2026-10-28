#!/usr/bin/env bash
# Checks around the OTel Collector's deploy in slipstream (`just obs-up`). preflight
# passes once what the Collector reads at start exists: the grafana-cloud-endpoints
# ConfigMap the eks stack creates and the grafana-cloud-write Secret ESO syncs. Kubernetes
# checks neither until the pod starts, and a pod that starts without them waits in
# CreateContainerConfigError until the rollout times out. rollout waits for the
# Deployment to roll out and, when it does not, prints why each Collector pod waits.
# delivery reads the Collector's own metrics at <metrics-url> until both Grafana Cloud
# exporters have sent more than in the baseline scrape on stdin, and fails as soon as
# either has failed to send more: the Collector stays ready while Grafana Cloud rejects
# what it sends.
# Usage: otel-collector-check.sh preflight|rollout|delivery <metrics-url> <baseline
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

# get_count SCRAPE NAME EXPORTER prints counter NAME of EXPORTER in SCRAPE as an
# integer, 0 when the Collector has not counted one yet. The client renders large
# counts in exponent form.
get_count() {
  awk -v series="$2{exporter=\"$3\"}" '$1 == series { v = $2 } END { print v + 0 }' <<<"$1"
}

# get_increase NAME EXPORTER prints how far counter NAME of EXPORTER rose from delivery's
# baseline scrape to its latest.
get_increase() {
  echo $(($(get_count "${scrape}" "$1" "$2") - $(get_count "${baseline}" "$1" "$2")))
}

delivery() {
  local url="$1" baseline scrape exporter kind failed pending
  baseline="$(cat)"
  # Spans leave on the batch processor's timeout, metric points on span_metrics' 60 s
  # flush; 20 scrapes 5 s apart cover both.
  for _ in $(seq 20); do
    if ! scrape="$(curl -sf "${url}")"; then
      echo "cannot read the Collector's metrics at ${url}; check the port-forward to svc/otel-collector" >&2
      return 1
    fi
    pending=()
    for exporter in otlp_grpc/grafana-cloud:spans prometheus_remote_write/grafana-cloud:metric_points; do
      kind="${exporter#*:}"
      exporter="${exporter%:*}"
      failed="$(get_increase "otelcol_exporter_send_failed_${kind}" "${exporter}")"
      if ((failed > 0)); then
        echo "${exporter} failed to send ${failed} ${kind//_/ } to Grafana Cloud. Check the write token in the grafana-cloud-write Secret and the instance IDs and endpoints in the grafana-cloud-endpoints ConfigMap; the Collector logs why: kubectl -n ${namespace} logs deploy/otel-collector | grep 'Exporting failed'" >&2
        return 1
      fi
      (($(get_increase "otelcol_exporter_sent_${kind}" "${exporter}") > 0)) || pending+=("${exporter}")
    done
    if ((${#pending[@]} == 0)); then
      echo "Grafana Cloud took the Collector's spans and metric points"
      return 0
    fi
    sleep 5
  done
  # An exporter that cannot reach its endpoint retries, counting neither a send nor a
  # failure until it gives up.
  echo "nothing from ${pending[*]} reached Grafana Cloud in 100 s, and nothing failed: an unreachable endpoint keeps the exporter retrying. Check the endpoints in the grafana-cloud-endpoints ConfigMap and kubectl -n ${namespace} logs deploy/otel-collector" >&2
  return 1
}

usage() {
  echo "usage: otel-collector-check.sh preflight|rollout|delivery <metrics-url> <baseline" >&2
  exit 2
}

case "${1:-}" in
preflight) preflight ;;
rollout) rollout ;;
delivery)
  [[ "$#" == 2 ]] || usage
  delivery "$2"
  ;;
*) usage ;;
esac
