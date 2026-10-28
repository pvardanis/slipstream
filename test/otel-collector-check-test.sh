#!/usr/bin/env bash
# Unit test for scripts/otel-collector-check.sh: kubectl, curl and sleep are stubbed on PATH,
# so no cluster or Collector is read and no wait is slept; the read of the rollout's pods
# renders its template with the real kubectl over local pod fixtures. It checks that:
# - preflight passes once the grafana-cloud-endpoints ConfigMap is in slipstream and the
#   grafana-cloud-write ExternalSecret is Ready; a missing ConfigMap or ExternalSecret
#   names `just cluster-up`, an unsynced one the describe command and the setup recipe,
#   and any other kubectl failure is shown with kubectl's current context;
# - rollout passes once the Deployment rolls out, and otherwise prints each Collector
#   pod's waiting reason and message, its last exit and why it is unscheduled, with the
#   describe and `logs --previous` commands;
# - delivery passes once the OTLP receiver's accepted spans and both Grafana Cloud
#   exporters' sent counts, under the IDs the Collector config names, rise above the
#   baseline scrape on stdin; it fails at the first scrape where either's failed count
#   rises, a count is not a number, or the page lacks the Collector's uptime, and after
#   20 scrapes 5 s apart when no span was received or one has still sent nothing; the
#   debug exporter's counts never stand in for theirs.
set -euo pipefail
unset KUBECTL_FAIL CURL_EXIT BASELINE DEBUG_SENT LABELS EXTRA_SERIES WHOLE_LINE

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
script="${repo_root}/scripts/otel-collector-check.sh"
# The Grafana Cloud exporters as the Collector's config names them, so the scrapes below
# carry the IDs the Collector counts under.
config="${repo_root}/k8s/otel-collector-config.yaml"
spans_exporter="$(yq '.service.pipelines.traces.exporters[] | select(test("grafana-cloud"))' "${config}")"
points_exporter="$(yq '.service.pipelines.metrics.exporters[] | select(test("grafana-cloud"))' "${config}")"
tmp="$(mktemp -d)"
trap 'rm -rf "${tmp}"' EXIT
mkdir -p "${tmp}/bin" "${tmp}/pods"
real_kubectl="$(command -v kubectl)"

# The Collector pods a failed rollout leaves, as the API server returns them: one whose
# env names a missing ConfigMap key, one crash-looping, one running again after it ran out
# of memory, one no node has room for.
cat >"${tmp}/pods/config-error.yaml" <<'EOF'
apiVersion: v1
kind: Pod
metadata: {name: otel-collector-7d9f}
spec: {containers: [{name: otel-collector, image: otel}]}
status:
  conditions:
    - {type: Ready, status: "False", reason: ContainersNotReady, message: "containers with unready status: [otel-collector]"}
  containerStatuses:
    - name: otel-collector
      state: {waiting: {reason: CreateContainerConfigError, message: "couldn't find key traces-endpoint in ConfigMap slipstream/grafana-cloud-endpoints"}}
EOF
cat >"${tmp}/pods/crash-loop.yaml" <<'EOF'
apiVersion: v1
kind: Pod
metadata: {name: otel-collector-8c2a}
spec: {containers: [{name: otel-collector, image: otel}]}
status:
  containerStatuses:
    - name: otel-collector
      state: {waiting: {reason: CrashLoopBackOff, message: "back-off 40s restarting failed container=otel-collector"}}
      lastState: {terminated: {reason: Error, exitCode: 1}}
EOF
cat >"${tmp}/pods/oom-restarted.yaml" <<'EOF'
apiVersion: v1
kind: Pod
metadata: {name: otel-collector-5f6d}
spec: {containers: [{name: otel-collector, image: otel}]}
status:
  containerStatuses:
    - name: otel-collector
      state: {running: {startedAt: "2026-10-28T10:00:00Z"}}
      lastState: {terminated: {reason: OOMKilled, exitCode: 137}}
EOF
cat >"${tmp}/pods/unschedulable.yaml" <<'EOF'
apiVersion: v1
kind: Pod
metadata: {name: otel-collector-1b3e}
spec: {containers: [{name: otel-collector, image: otel}]}
status:
  conditions:
    - {type: PodScheduled, status: "False", reason: Unschedulable, message: "0/2 nodes are available: 2 Insufficient memory."}
EOF

# Stub kubectl: every call's arguments are logged; KUBECTL_FAIL names the one call, by its
# first words after `-n slipstream`, that fails as the real one would, and how: "missing"
# as when the object or its kind is not there, "unreachable" as when the cluster is not;
# "rollout and get pods" fails the rollout and then the pods' read.
# `get pods -o jsonpath` renders its template with the real kubectl over the pods above,
# one at a time, as kubectl renders a List.
cat >"${tmp}/bin/kubectl" <<EOF
#!/usr/bin/env bash
printf '%s\n' "\$*" >>"${tmp}/kubectl-calls"
unreachable() { echo 'Unable to connect to the server: dial tcp: lookup example.eks.amazonaws.com: no such host' >&2; exit 1; }
[[ "\$*" != "config current-context" ]] || { echo arn:aws:eks:eu-west-1:123456789012:cluster/slipstream; exit 0; }
[[ "\$1 \$2" == "-n slipstream" ]] || { echo "unexpected call: \$*" >&2; exit 2; }
shift 2
case "\$*" in
  "get configmap grafana-cloud-endpoints -o name")
    case "\${KUBECTL_FAIL:-}" in
      "get configmap missing") echo 'Error from server (NotFound): configmaps "grafana-cloud-endpoints" not found' >&2; exit 1 ;;
      "get configmap unreachable") unreachable ;;
    esac
    echo configmap/grafana-cloud-endpoints ;;
  "wait --for=condition=Ready externalsecret/grafana-cloud-write --timeout=120s")
    case "\${KUBECTL_FAIL:-}" in
      "wait") echo 'error: timed out waiting for the condition on externalsecrets/grafana-cloud-write' >&2; exit 1 ;;
      "wait missing") echo 'error: the server doesn'"'"'t have a resource type "externalsecret"' >&2; exit 1 ;;
      "wait unreachable") unreachable ;;
    esac
    echo externalsecret.external-secrets.io/grafana-cloud-write condition met ;;
  "rollout status deploy/otel-collector --timeout=120s")
    [[ "\${KUBECTL_FAIL:-}" != rollout* ]] || { echo 'error: timed out waiting for the condition' >&2; exit 1; } ;;
  "get pods -l app=otel-collector -o jsonpath="*)
    [[ "\${KUBECTL_FAIL:-}" != "rollout and get pods" ]] || unreachable
    template="\${6#jsonpath=}"
    [[ "\${template}" == '{range .items[*]}'*'{end}' ]] || { echo "template not over .items: \${template}" >&2; exit 2; }
    template="\${template#'{range .items[*]}'}"
    for pod in "${tmp}"/pods/*.yaml; do
      KUBECONFIG=/dev/null "${real_kubectl}" annotate --local -f "\${pod}" probe=1 -o jsonpath="\${template%'{end}'}"
    done ;;
  *) echo "unexpected call: -n slipstream \$*" >&2; exit 2 ;;
esac
EOF
# Stub curl: `curl -sSf --max-time 5 <url>` prints the next scrape, scrape.1, scrape.2 and so on,
# repeating the last; CURL_EXIT fails it, with curl's error, as an unreachable
# port-forward does. Stub sleep: log each wait.
cat >"${tmp}/bin/curl" <<EOF
#!/usr/bin/env bash
[[ "\$*" == "-sSf --max-time 5 http://localhost:18888/metrics" ]] || { echo "unexpected call: \$*" >&2; exit 2; }
[[ -z "\${CURL_EXIT:-}" ]] || { echo "curl: (\${CURL_EXIT}) Failed to connect to localhost port 18888" >&2; exit "\${CURL_EXIT}"; }
n=\$((\$(cat "${tmp}/curl-count") + 1))
echo "\${n}" >"${tmp}/curl-count"
[[ -f "${tmp}/scrape.\${n}" ]] || n=\$(find "${tmp}" -name 'scrape.*' | wc -l | tr -d ' ')
cat "${tmp}/scrape.\${n}"
EOF
cat >"${tmp}/bin/sleep" <<EOF
#!/usr/bin/env bash
echo "\$1" >>"${tmp}/sleeps"
EOF
chmod +x "${tmp}/bin/"*

# A scrape of the Collector's :8888 as 0.160.0 renders it, from "sent_spans failed_spans
# sent_points failed_points" for the Grafana Cloud exporters; "-" leaves a counter out, as
# the Collector does before its first count. An optional fifth count is the spans the OTLP
# receiver accepted, 1 unless given. The debug exporter's counts are DEBUG_SENT.
# "not-collector" is a page from some other process holding the port. LABELS goes before
# the exporter label on the Grafana Cloud exporters' series, and EXTRA_SERIES ends every
# scrape.
scrape() {
  if [[ "$1" == not-collector ]]; then
    echo 'http_requests_total{code="200"} 3'
    return
  fi
  local sent_spans="$1" failed_spans="$2" sent_points="$3" failed_points="$4" received="${5:-1}"
  local spans="{${LABELS:-}exporter=\"${spans_exporter}\"}" points="{${LABELS:-}exporter=\"${points_exporter}\"}"
  echo 'otelcol_process_uptime 42.5'
  echo "otelcol_receiver_accepted_spans{receiver=\"otlp\",transport=\"http\"} ${received}"
  echo '# HELP otelcol_exporter_sent_spans Number of spans successfully sent to destination.'
  echo '# TYPE otelcol_exporter_sent_spans counter'
  echo "otelcol_exporter_sent_spans{exporter=\"debug\"} ${DEBUG_SENT:-1}"
  [[ "${sent_spans}" == - ]] || echo "otelcol_exporter_sent_spans${spans} ${sent_spans}"
  [[ "${failed_spans}" == - ]] || echo "otelcol_exporter_send_failed_spans${spans} ${failed_spans}"
  echo "otelcol_exporter_sent_metric_points{exporter=\"debug\"} ${DEBUG_SENT:-1}"
  [[ "${sent_points}" == - ]] || echo "otelcol_exporter_sent_metric_points${points} ${sent_points}"
  [[ "${failed_points}" == - ]] || echo "otelcol_exporter_send_failed_metric_points${points} ${failed_points}"
  echo "otelcol_exporter_queue_size{data_type=\"traces\",exporter=\"${spans_exporter}\"} 0"
  [[ -z "${EXTRA_SERIES:-}" ]] || echo "${EXTRA_SERIES}"
}
# The scrapes the stub curl serves, one argument each, in scrape's four counts.
serve() {
  local n=0 counts
  rm -f "${tmp}"/scrape.*
  for counts in "$@"; do
    n=$((n + 1))
    # shellcheck disable=SC2086 # the four counts split on purpose
    scrape ${counts} >"${tmp}/scrape.${n}"
  done
}

fail=0
# BASELINE is the scrape on stdin, in scrape's counts; its receiver count defaults to 0,
# below the served scrapes' 1.
run() {
  : >"${tmp}/kubectl-calls"
  : >"${tmp}/sleeps"
  echo 0 >"${tmp}/curl-count"
  # shellcheck disable=SC2086 # the four counts split on purpose
  scrape ${BASELINE:-- - - - 0} | PATH="${tmp}/bin:${PATH}" bash "${script}" "$@" >"${tmp}/stdout" 2>"${tmp}/stderr"
}
ok() { echo "ok   $1"; }
bad() {
  echo "FAIL $1" >&2
  fail=1
}
# Runs the script with the arguments after the label; it must exit 0.
check_passes() {
  local label="$1"
  shift
  if run "$@"; then ok "${label}"; else bad "${label}: $(cat "${tmp}/stderr")"; fi
}
# Runs the script with the arguments after the label and expected stderr; it must exit
# non-zero with that text on stderr, as a whole line when WHOLE_LINE is set.
check_fails() {
  local label="$1" want_err="$2"
  shift 2
  if run "$@"; then
    bad "${label}: script exited 0"
  elif ! grep -qF${WHOLE_LINE:+x} -- "${want_err}" "${tmp}/stderr"; then
    bad "${label}: stderr lacks '${want_err}'"
  else
    ok "${label}"
  fi
}

# Preflight passes with the ConfigMap present and the ExternalSecret Ready.
check_passes "preflight passes" preflight
if grep -qxF -- "-n slipstream wait --for=condition=Ready externalsecret/grafana-cloud-write --timeout=120s" "${tmp}/kubectl-calls"; then
  ok "preflight waits for the write token to sync"
else
  bad "preflight waits for the write token to sync"
fi
KUBECTL_FAIL="get configmap missing" check_fails "missing endpoints ConfigMap" "the grafana-cloud-endpoints ConfigMap is not in slipstream: run \`just cluster-up\`" preflight
if [[ "$(grep -c wait "${tmp}/kubectl-calls")" == 0 ]]; then ok "missing ConfigMap fails before the wait"; else bad "missing ConfigMap fails before the wait"; fi
KUBECTL_FAIL="wait" check_fails "unsynced write token" "kubectl -n slipstream describe externalsecret grafana-cloud-write" preflight
KUBECTL_FAIL="wait" check_fails "unsynced write token names the setup recipe" "just grafana-cloud-setup" preflight
KUBECTL_FAIL="wait missing" check_fails "missing ExternalSecret" "the grafana-cloud-write ExternalSecret is not in slipstream: run \`just cluster-up\`" preflight
# Any other kubectl failure is shown as kubectl gave it, with the context it ran against.
for call in "get configmap" wait; do
  KUBECTL_FAIL="${call} unreachable" check_fails "unreachable cluster at ${call}" "Unable to connect to the server: dial tcp: lookup example.eks.amazonaws.com: no such host" preflight
  KUBECTL_FAIL="${call} unreachable" check_fails "unreachable cluster at ${call} names the context" "kubectl's current context is arn:aws:eks:eu-west-1:123456789012:cluster/slipstream" preflight
  if grep -qF "just cluster-up" "${tmp}/stderr"; then bad "unreachable cluster at ${call} is not a missing object"; else ok "unreachable cluster at ${call} is not a missing object"; fi
done
# Rollout passes when the Deployment rolls out, and on a failed one shows why the pod waits.
check_passes "rollout passes" rollout
WHOLE_LINE=1
KUBECTL_FAIL="rollout" check_fails "failed rollout shows the pod's waiting reason" \
  "otel-collector-7d9f: waiting CreateContainerConfigError couldn't find key traces-endpoint in ConfigMap slipstream/grafana-cloud-endpoints" rollout
KUBECTL_FAIL="rollout" check_fails "failed rollout shows a crashed container's last exit" \
  "otel-collector-8c2a: waiting CrashLoopBackOff back-off 40s restarting failed container=otel-collector last exited 1 Error" rollout
KUBECTL_FAIL="rollout" check_fails "failed rollout shows a restarted container's last exit" \
  "otel-collector-5f6d: last exited 137 OOMKilled" rollout
KUBECTL_FAIL="rollout" check_fails "failed rollout shows why a pod is unscheduled" \
  "otel-collector-1b3e: unschedulable: 0/2 nodes are available: 2 Insufficient memory." rollout
unset WHOLE_LINE
KUBECTL_FAIL="rollout" check_fails "failed rollout names the describe command" "kubectl -n slipstream describe pod -l app=otel-collector" rollout
KUBECTL_FAIL="rollout" check_fails "failed rollout names the crashed container's log" "kubectl -n slipstream logs -l app=otel-collector --previous" rollout
KUBECTL_FAIL="rollout and get pods" check_fails "failed rollout says when the pods cannot be read" "(could not read the Collector pods' status)" rollout
KUBECTL_FAIL="rollout and get pods" check_fails "failed rollout shows why the pods cannot be read" "Unable to connect to the server" rollout
# Delivery passes once both exporters' sent counts rise, each at its own scrape.
url=http://localhost:18888/metrics
serve "- - - -" "1 - - -" "1 - 8 -"
check_passes "delivery passes" delivery "${url}"
if [[ "$(cat "${tmp}/stdout")" == "Grafana Cloud's endpoints accepted the Collector's spans and metric points" ]]; then
  ok "delivery says what Grafana Cloud accepted"
else
  bad "delivery says what Grafana Cloud accepted: $(cat "${tmp}/stdout")"
fi
if [[ "$(cat "${tmp}/curl-count")" == 3 ]]; then ok "delivery scrapes until both exporters sent"; else bad "delivery scrapes until both exporters sent"; fi
# Counts from before the baseline do not count, sent or failed.
serve "5 2 40 3" "6 2 40 3" "6 2 48 3"
BASELINE="5 2 40 3 0" check_passes "earlier counts do not count" delivery "${url}"
# A long-running Collector renders large counts in exponent form; one that has not moved
# is no send.
serve "1.234567e+06 - 2.5e+07 -"
BASELINE="1.234567e+06 - 2.5e+07 - 0" check_fails "an unchanged large count is not a send" "nothing from ${spans_exporter}" delivery "${url}"
serve "1.234567e+06 - 2.5e+07 -"
BASELINE="1.234566e+06 - 2.4e+07 - 0" check_passes "a risen large count is a send" delivery "${url}"
# A series carrying more labels than the exporter's still counts, and an exporter's
# count split across series is their sum.
LABELS='service_name="otelcol-contrib",' serve "- - - -" "1 - 8 -"
LABELS='service_name="otelcol-contrib",' check_passes "a series with more labels counts" delivery "${url}"
split="otelcol_exporter_sent_spans{exporter=\"${spans_exporter}\",pipeline=\"traces/b\"} 5"
EXTRA_SERIES="${split}" serve "1 - 8 -"
EXTRA_SERIES="${split}" check_passes "an exporter's split count is its series' sum" delivery "${url}"
# A count that is not a number fails, naming the series.
serve "1 - x -"
check_fails "a count that is not a number fails delivery" "the Collector's otelcol_exporter_sent_metric_points for ${points_exporter} reads 'x', not a count" delivery "${url}"
if [[ "$(cat "${tmp}/curl-count")" == 1 ]]; then ok "a count that is not a number fails at its scrape"; else bad "a count that is not a number fails at its scrape"; fi
# A failed send fails at the scrape it shows on, naming the exporter and what to check.
serve "- 1 - -"
check_fails "failed spans fail delivery" "${spans_exporter} failed to send 1 spans" delivery "${url}"
if [[ ! -s "${tmp}/sleeps" ]]; then ok "failed spans fail at the first scrape"; else bad "failed spans fail at the first scrape"; fi
check_fails "failed spans point at the token and endpoints" "grafana-cloud-endpoints" delivery "${url}"
check_fails "failed spans point at the Collector's log" "kubectl -n slipstream logs deploy/otel-collector | grep 'Exporting failed'" delivery "${url}"
serve "1 - - 2"
BASELINE="- - - 1 0" check_fails "failed metric points fail delivery" "${points_exporter} failed to send 1 metric points" delivery "${url}"
serve "- - - -" "1 - - -" "1 - - 1"
check_fails "a failure at a later scrape fails delivery" "${points_exporter} failed to send 1 metric points" delivery "${url}"
if [[ "$(cat "${tmp}/curl-count")" == 3 ]]; then ok "a later failure fails at the scrape it shows on"; else bad "a later failure fails at the scrape it shows on"; fi
# A failure fails delivery even when the same scrape shows sends.
serve "1 - 8 2"
check_fails "a failure beside sends fails delivery" "${points_exporter} failed to send 2 metric points" delivery "${url}"
# Nothing sent within the scrapes fails, naming only the exporter that sent nothing.
serve "- - - -" "1 - - -"
check_fails "undelivered metric points fail delivery" "nothing from ${points_exporter} reached Grafana Cloud" delivery "${url}"
if [[ "$(cat "${tmp}/curl-count")" == 20 && "$(wc -l <"${tmp}/sleeps" | tr -d ' ')" == 19 && "$(sort -u "${tmp}/sleeps")" == 5 ]]; then
  ok "delivery scrapes 20 times, 5 s apart"
else
  bad "delivery scrapes 20 times, 5 s apart: $(cat "${tmp}/curl-count") scrapes, $(wc -l <"${tmp}/sleeps" | tr -d ' ') waits"
fi
check_fails "undelivered sends name the retrying causes" "unreachable, or answering 429 or 5xx" delivery "${url}"
check_fails "undelivered sends point at the retry log" "kubectl -n slipstream logs deploy/otel-collector | grep 'will retry'" delivery "${url}"
if grep -qF "${spans_exporter}" "${tmp}/stderr"; then bad "a delivered exporter is not named undelivered"; else ok "a delivered exporter is not named undelivered"; fi
# Sends that do not follow a span the receiver accepted since the baseline are not the
# demo span's: delivery fails naming the port-forward to the receiver.
serve "1 - 8 - 0"
check_fails "no span received fails delivery" "no span reached the Collector's OTLP receiver in 95 s: check that the port-forward to svc/otel-collector holds local port 4318" delivery "${url}"
if grep -qF "nothing from" "${tmp}/stderr"; then bad "no span received does not blame the exporters"; else ok "no span received does not blame the exporters"; fi
# The debug exporter's counts never count as delivery.
DEBUG_SENT=9 serve "- - - -"
check_fails "the debug exporter's sends are not delivery" "nothing from ${spans_exporter}" delivery "${url}"
# A page without the Collector's uptime is some other process's, baseline or scrape.
serve "1 - 8 -"
BASELINE=not-collector check_fails "a baseline from another process fails delivery" "the baseline scrape on stdin is not the Collector's metrics" delivery "${url}"
serve "- - - -" not-collector
check_fails "a scrape from another process fails delivery" "${url} is not the Collector's metrics" delivery "${url}"
if [[ "$(cat "${tmp}/curl-count")" == 2 ]]; then ok "another process's page fails at the scrape it shows on"; else bad "another process's page fails at the scrape it shows on"; fi
CURL_EXIT=7 check_fails "an unreadable metrics endpoint fails delivery" "cannot read the Collector's metrics at ${url}" delivery "${url}"
CURL_EXIT=7 check_fails "an unreadable metrics endpoint shows curl's error" "curl: (7) Failed to connect" delivery "${url}"
check_fails "delivery without a url" "usage: otel-collector-check.sh" delivery
check_fails "unknown subcommand" "usage: otel-collector-check.sh" bogus

exit "${fail}"
