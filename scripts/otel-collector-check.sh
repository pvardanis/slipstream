#!/usr/bin/env bash
# Checks around the OTel Collector's deploy in slipstream (`just obs-up`). preflight
# passes once what the Collector reads at start exists: the grafana-cloud-endpoints
# ConfigMap the eks stack creates and the grafana-cloud-write Secret ESO syncs. Kubernetes
# checks neither until the pod starts, and a pod that starts without them waits in
# CreateContainerConfigError until the rollout times out. rollout waits for the
# Deployment to roll out and, when it does not, prints why each Collector pod waits.
# delivery reads the Collector's own metrics at <metrics-url> every 5 s and passes when,
# since the baseline scrape on stdin, its OTLP receiver has accepted a span and both
# Grafana Cloud exporters have sent more. It fails as soon as either exporter has failed
# to send more, or when after 95 s no span was received or one has still sent nothing:
# the Collector stays ready while Grafana Cloud rejects what it sends or never answers.
# Usage: otel-collector-check.sh preflight|rollout|delivery <metrics-url> <baseline
set -euo pipefail

namespace=slipstream

# report_kubectl_failure ERROR prints kubectl's ERROR, for failures other than a missing
# object, with the context kubectl ran against.
report_kubectl_failure() {
  echo "kubectl failed: $1" >&2
  echo "kubectl's current context is $(kubectl config current-context 2>&1); check it points at the slipstream cluster" >&2
}

preflight() {
  local err
  if ! err="$(kubectl -n "${namespace}" get configmap grafana-cloud-endpoints -o name 2>&1 >/dev/null)"; then
    case "${err}" in
    *NotFound*) echo "the grafana-cloud-endpoints ConfigMap is not in ${namespace}: run \`just cluster-up\`, which creates it from the grafana stack's outputs" >&2 ;;
    *) report_kubectl_failure "${err}" ;;
    esac
    return 1
  fi
  if ! err="$(kubectl -n "${namespace}" wait --for=condition=Ready externalsecret/grafana-cloud-write --timeout=120s 2>&1 >/dev/null)"; then
    case "${err}" in
    *"timed out"*) echo "the grafana-cloud-write Secret has not synced from Secrets Manager: see why in \`kubectl -n ${namespace} describe externalsecret grafana-cloud-write\`; if the write token was never stored, run \`just grafana-cloud-setup\`" >&2 ;;
    *NotFound* | *"doesn't have a resource type"*) echo "the grafana-cloud-write ExternalSecret is not in ${namespace}: run \`just cluster-up\`, whose eks stack creates it" >&2 ;;
    *) report_kubectl_failure "${err}" ;;
    esac
    return 1
  fi
}

rollout() {
  if kubectl -n "${namespace}" rollout status deploy/otel-collector --timeout=120s; then
    return 0
  fi
  # A container that cannot start waits with a reason and a message naming the cause,
  # such as CreateContainerConfigError and the ConfigMap key or Secret it lacks; one that
  # crashes also shows how it last exited; a pod no node has room for shows why.
  echo "the Collector did not roll out; its pods:" >&2
  kubectl -n "${namespace}" get pods -l app=otel-collector -o jsonpath='{range .items[*]}{.metadata.name}{":"}{range .status.containerStatuses[?(@.state.waiting)]}{" waiting "}{.state.waiting.reason}{" "}{.state.waiting.message}{end}{range .status.containerStatuses[?(@.lastState.terminated)]}{" last exited "}{.lastState.terminated.exitCode}{" "}{.lastState.terminated.reason}{end}{range .status.conditions[?(@.reason=="Unschedulable")]}{" unschedulable: "}{.message}{end}{"\n"}{end}' >&2 ||
    echo "(could not read the Collector pods' status)" >&2
  echo "see the pod's events in \`kubectl -n ${namespace} describe pod -l app=otel-collector\`, and a crashed Collector's last log in \`kubectl -n ${namespace} logs -l app=otel-collector --previous\`" >&2
  return 1
}

# require_collector_metrics SCRAPE SOURCE fails unless SCRAPE, read from SOURCE, is the
# Collector's own metrics, which always carry its uptime.
require_collector_metrics() {
  grep -qF otelcol_process_uptime <<<"$1" && return 0
  echo "$2 is not the Collector's metrics: it lacks otelcol_process_uptime; check that the port-forward to svc/otel-collector holds the local port" >&2
  return 1
}

# get_count SCRAPE NAME LABEL VALUE prints counter NAME in SCRAPE as an integer, summed
# over every series of NAME whose LABEL is VALUE, 0 when the Collector has not counted one
# yet. The Collector renders large counts in exponent form.
get_count() {
  local count
  if ! count="$(awk -v name="$2{" -v label="$3=\"$4\"" '
    index($1, name) == 1 && index($1, label) {
      if ($2 !~ /^[0-9]+(\.[0-9]+)?(e\+[0-9]+)?$/) { bad = $2; exit }
      v += $2
    }
    END { if (bad != "") { print bad; exit 1 } print v + 0 }' <<<"$1")"; then
    echo "the Collector's $2 for $4 reads '${count}', not a count" >&2
    return 1
  fi
  echo "${count}"
}

# get_increase NAME LABEL VALUE prints how far counter NAME of the series whose LABEL is
# VALUE rose from delivery's baseline scrape to its latest.
get_increase() {
  local now before
  now="$(get_count "${scrape}" "$1" "$2" "$3")" || return 1
  before="$(get_count "${baseline}" "$1" "$2" "$3")" || return 1
  echo $((now - before))
}

delivery() {
  local url="$1" baseline scrape attempt received exporter kind failed sent pending
  baseline="$(cat)"
  require_collector_metrics "${baseline}" "the baseline scrape on stdin"
  # Spans leave on the batch processor's timeout, metric points on span_metrics' 60 s
  # flush; 20 scrapes 5 s apart, over 95 s, cover both.
  for attempt in $(seq 20); do
    if ! scrape="$(curl -sSf --max-time 5 "${url}")"; then
      echo "cannot read the Collector's metrics at ${url}; check the port-forward to svc/otel-collector" >&2
      return 1
    fi
    require_collector_metrics "${scrape}" "${url}"
    # Sends count only after a span the OTLP receiver accepted since the baseline.
    received="$(get_increase otelcol_receiver_accepted_spans receiver otlp)"
    pending=()
    for exporter in otlp_grpc/grafana-cloud:spans prometheus_remote_write/grafana-cloud:metric_points; do
      kind="${exporter#*:}"
      exporter="${exporter%:*}"
      failed="$(get_increase "otelcol_exporter_send_failed_${kind}" exporter "${exporter}")"
      if ((failed > 0)); then
        echo "${exporter} failed to send ${failed} ${kind//_/ } to Grafana Cloud. Check the write token in the grafana-cloud-write Secret and the instance IDs and endpoints in the grafana-cloud-endpoints ConfigMap; the Collector logs why: kubectl -n ${namespace} logs deploy/otel-collector | grep 'Exporting failed'" >&2
        return 1
      fi
      sent="$(get_increase "otelcol_exporter_sent_${kind}" exporter "${exporter}")"
      ((sent > 0)) || pending+=("${exporter}")
    done
    if ((received > 0 && ${#pending[@]} == 0)); then
      echo "Grafana Cloud's endpoints accepted the Collector's spans and metric points"
      return 0
    fi
    ((attempt == 20)) || sleep 5
  done
  if ((received == 0)); then
    echo "no span reached the Collector's OTLP receiver in 95 s: check that the port-forward to svc/otel-collector holds local port 4318" >&2
    return 1
  fi
  # An exporter whose endpoint is unreachable, or answers 429 or 5xx, retries, counting
  # neither a send nor a failure until it gives up.
  echo "nothing from ${pending[*]} reached Grafana Cloud in 95 s, and nothing failed: an exporter whose endpoint is unreachable, or answering 429 or 5xx, retries without counting a failure. Check the endpoints in the grafana-cloud-endpoints ConfigMap, and the retries in kubectl -n ${namespace} logs deploy/otel-collector | grep 'will retry'" >&2
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
