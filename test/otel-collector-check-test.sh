#!/usr/bin/env bash
# Unit test for scripts/otel-collector-check.sh: kubectl, curl and sleep are stubbed on PATH,
# so no cluster or Collector is read and no wait is slept. It checks that preflight passes only once the
# grafana-cloud-endpoints ConfigMap exists in slipstream and the grafana-cloud-write
# ExternalSecret is Ready, and otherwise fails naming the recipe or command that fixes it;
# and that rollout passes once the Deployment rolls out, and otherwise fails printing each
# Collector pod's waiting reason and message, which name a missing key or Secret; and that
# delivery passes once both Grafana Cloud exporters' sent counts rise above the baseline
# scrape on stdin, fails at the first scrape where either's failed count rises, naming
# what to check, and fails when one still has sent nothing after its 20 scrapes, the
# debug exporter's counts never standing in for theirs.
set -euo pipefail
unset KUBECTL_FAIL CURL_EXIT BASELINE DEBUG_SENT

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
script="${repo_root}/scripts/otel-collector-check.sh"
# The Grafana Cloud exporters as the Collector's config names them, so the scrapes below
# carry the IDs the Collector counts under.
config="${repo_root}/k8s/otel-collector-config.yaml"
spans_exporter="$(yq '.service.pipelines.traces.exporters[] | select(test("grafana-cloud"))' "${config}")"
points_exporter="$(yq '.service.pipelines.metrics.exporters[] | select(test("grafana-cloud"))' "${config}")"
tmp="$(mktemp -d)"
trap 'rm -rf "${tmp}"' EXIT
mkdir -p "${tmp}/bin"

# Stub kubectl: every call's arguments are logged; KUBECTL_FAIL names the one call, by its
# first words after `-n slipstream`, that fails as the real one would.
cat >"${tmp}/bin/kubectl" <<EOF
#!/usr/bin/env bash
printf '%s\n' "\$*" >>"${tmp}/kubectl-calls"
[[ "\$1 \$2" == "-n slipstream" ]] || { echo "unexpected call: \$*" >&2; exit 2; }
shift 2
case "\$*" in
  "get configmap grafana-cloud-endpoints -o name")
    [[ "\${KUBECTL_FAIL:-}" != "get configmap" ]] || { echo 'Error from server (NotFound): configmaps "grafana-cloud-endpoints" not found' >&2; exit 1; }
    echo configmap/grafana-cloud-endpoints ;;
  "wait --for=condition=Ready externalsecret/grafana-cloud-write --timeout=120s")
    [[ "\${KUBECTL_FAIL:-}" != "wait" ]] || { echo 'error: timed out waiting for the condition on externalsecrets/grafana-cloud-write' >&2; exit 1; }
    echo externalsecret.external-secrets.io/grafana-cloud-write condition met ;;
  "rollout status deploy/otel-collector --timeout=120s")
    [[ "\${KUBECTL_FAIL:-}" != "rollout" ]] || { echo 'error: timed out waiting for the condition' >&2; exit 1; } ;;
  "get pods -l app=otel-collector -o jsonpath="*)
    echo "otel-collector-7d9f: CreateContainerConfigError couldn't find key traces-endpoint in ConfigMap slipstream/grafana-cloud-endpoints" ;;
  *) echo "unexpected call: -n slipstream \$*" >&2; exit 2 ;;
esac
EOF
# Stub curl: `curl -sf <url>` prints the next scrape, scrape.1, scrape.2 and so on,
# repeating the last; CURL_EXIT fails it as an unreachable port-forward does. Stub sleep:
# log each wait.
cat >"${tmp}/bin/curl" <<EOF
#!/usr/bin/env bash
[[ "\$*" == "-sf http://localhost:8888/metrics" ]] || { echo "unexpected call: \$*" >&2; exit 2; }
[[ -z "\${CURL_EXIT:-}" ]] || exit "\${CURL_EXIT}"
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
# the Collector does before its first count. The debug exporter's counts are DEBUG_SENT.
scrape() {
  local sent_spans="$1" failed_spans="$2" sent_points="$3" failed_points="$4"
  local spans="{exporter=\"${spans_exporter}\"}" points="{exporter=\"${points_exporter}\"}"
  echo '# HELP otelcol_exporter_sent_spans Number of spans successfully sent to destination.'
  echo '# TYPE otelcol_exporter_sent_spans counter'
  echo "otelcol_exporter_sent_spans{exporter=\"debug\"} ${DEBUG_SENT:-1}"
  [[ "${sent_spans}" == - ]] || echo "otelcol_exporter_sent_spans${spans} ${sent_spans}"
  [[ "${failed_spans}" == - ]] || echo "otelcol_exporter_send_failed_spans${spans} ${failed_spans}"
  echo "otelcol_exporter_sent_metric_points{exporter=\"debug\"} ${DEBUG_SENT:-1}"
  [[ "${sent_points}" == - ]] || echo "otelcol_exporter_sent_metric_points${points} ${sent_points}"
  [[ "${failed_points}" == - ]] || echo "otelcol_exporter_send_failed_metric_points${points} ${failed_points}"
  echo "otelcol_exporter_queue_size{data_type=\"traces\",exporter=\"${spans_exporter}\"} 0"
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
# BASELINE is the scrape on stdin, in scrape's four counts.
run() {
  : >"${tmp}/kubectl-calls"
  : >"${tmp}/sleeps"
  echo 0 >"${tmp}/curl-count"
  # shellcheck disable=SC2086 # the four counts split on purpose
  scrape ${BASELINE:-- - - -} | PATH="${tmp}/bin:${PATH}" bash "${script}" "$@" >"${tmp}/stdout" 2>"${tmp}/stderr"
}
ok() { echo "ok   $1"; }
bad() {
  echo "FAIL $1" >&2
  fail=1
}
# Runs the script with the arguments after the label and expected stderr; it must exit
# non-zero with that text on stderr.
check_fails() {
  local label="$1" want_err="$2"
  shift 2
  if run "$@"; then
    bad "${label}: script exited 0"
  elif ! grep -qF -- "${want_err}" "${tmp}/stderr"; then
    bad "${label}: stderr lacks '${want_err}'"
  else
    ok "${label}"
  fi
}

# Preflight passes with the ConfigMap present and the ExternalSecret Ready.
if run preflight; then ok "preflight passes"; else bad "preflight passes: $(cat "${tmp}/stderr")"; fi
if grep -qxF -- "-n slipstream wait --for=condition=Ready externalsecret/grafana-cloud-write --timeout=120s" "${tmp}/kubectl-calls"; then
  ok "preflight waits for the write token to sync"
else
  bad "preflight waits for the write token to sync"
fi
KUBECTL_FAIL="get configmap" check_fails "missing endpoints ConfigMap" "run \`just cluster-up\`" preflight
if [[ "$(grep -c wait "${tmp}/kubectl-calls")" == 0 ]]; then ok "missing ConfigMap fails before the wait"; else bad "missing ConfigMap fails before the wait"; fi
KUBECTL_FAIL="wait" check_fails "unsynced write token" "kubectl -n slipstream describe externalsecret grafana-cloud-write" preflight
KUBECTL_FAIL="wait" check_fails "unsynced write token names the setup recipe" "just grafana-cloud-setup" preflight
# Rollout passes when the Deployment rolls out, and on a failed one shows why the pod waits.
if run rollout; then ok "rollout passes"; else bad "rollout passes: $(cat "${tmp}/stderr")"; fi
KUBECTL_FAIL="rollout" check_fails "failed rollout shows the pod's waiting reason" \
  "otel-collector-7d9f: CreateContainerConfigError couldn't find key traces-endpoint in ConfigMap slipstream/grafana-cloud-endpoints" rollout
KUBECTL_FAIL="rollout" check_fails "failed rollout names the describe command" "kubectl -n slipstream describe pod -l app=otel-collector" rollout
# Delivery passes once both exporters' sent counts rise, each at its own scrape.
url=http://localhost:8888/metrics
serve "- - - -" "1 - - -" "1 - 8 -"
if run delivery "${url}"; then ok "delivery passes"; else bad "delivery passes: $(cat "${tmp}/stderr")"; fi
if [[ "$(cat "${tmp}/curl-count")" == 3 ]]; then ok "delivery scrapes until both exporters sent"; else bad "delivery scrapes until both exporters sent"; fi
# Counts from before the baseline do not count, sent or failed.
serve "5 2 40 3" "6 2 40 3" "6 2 48 3"
if BASELINE="5 2 40 3" run delivery "${url}"; then ok "earlier counts do not count"; else bad "earlier counts do not count: $(cat "${tmp}/stderr")"; fi
# A long-running Collector renders large counts in exponent form; one that has not moved
# is no send.
serve "1.234567e+06 - 2.5e+07 -"
BASELINE="1.234567e+06 - 2.5e+07 -" check_fails "an unchanged large count is not a send" "nothing from ${spans_exporter}" delivery "${url}"
serve "1.234567e+06 - 2.5e+07 -"
if BASELINE="1.234566e+06 - 2.4e+07 -" run delivery "${url}"; then ok "a risen large count is a send"; else bad "a risen large count is a send: $(cat "${tmp}/stderr")"; fi
# A failed send fails at the scrape it shows on, naming the exporter and what to check.
serve "- 1 - -"
check_fails "failed spans fail delivery" "${spans_exporter} failed to send 1 spans" delivery "${url}"
if [[ ! -s "${tmp}/sleeps" ]]; then ok "failed spans fail at the first scrape"; else bad "failed spans fail at the first scrape"; fi
check_fails "failed spans point at the token and endpoints" "grafana-cloud-endpoints" delivery "${url}"
check_fails "failed spans point at the Collector's log" "kubectl -n slipstream logs deploy/otel-collector | grep 'Exporting failed'" delivery "${url}"
serve "1 - - 2"
BASELINE="- - - 1" check_fails "failed metric points fail delivery" "${points_exporter} failed to send 1 metric points" delivery "${url}"
serve "- - - -" "1 - - -" "1 - - 1"
check_fails "a failure at a later scrape fails delivery" "${points_exporter} failed to send 1 metric points" delivery "${url}"
if [[ "$(cat "${tmp}/curl-count")" == 3 ]]; then ok "a later failure fails at the scrape it shows on"; else bad "a later failure fails at the scrape it shows on"; fi
# A failure fails delivery even when the same scrape shows sends.
serve "1 - 8 2"
check_fails "a failure beside sends fails delivery" "${points_exporter} failed to send 2 metric points" delivery "${url}"
# Nothing sent within the scrapes fails, naming only the exporter that sent nothing.
serve "- - - -" "1 - - -"
check_fails "undelivered metric points fail delivery" "nothing from ${points_exporter} reached Grafana Cloud" delivery "${url}"
if [[ "$(cat "${tmp}/curl-count")" == 20 && "$(sort -u "${tmp}/sleeps")" == 5 ]]; then
  ok "delivery scrapes 20 times, 5 s apart"
else
  bad "delivery scrapes 20 times, 5 s apart: $(cat "${tmp}/curl-count") scrapes"
fi
if grep -qF "${spans_exporter}" "${tmp}/stderr"; then bad "a delivered exporter is not named undelivered"; else ok "a delivered exporter is not named undelivered"; fi
# The debug exporter's counts never count as delivery.
serve "- - - -"
DEBUG_SENT=9 check_fails "the debug exporter's sends are not delivery" "nothing from ${spans_exporter}" delivery "${url}"
CURL_EXIT=7 check_fails "an unreadable metrics endpoint fails delivery" "cannot read the Collector's metrics at ${url}" delivery "${url}"
check_fails "delivery without a url" "usage: otel-collector-check.sh" delivery
check_fails "unknown subcommand" "usage: otel-collector-check.sh" bogus

exit "${fail}"
