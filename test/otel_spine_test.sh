#!/usr/bin/env bash
# Integration test for the request-ID spine: runs the real OTel Collector
# (the same k8s/otel-collector-config.yaml the cluster deploys) in a local
# container, pushes one OTLP trace carrying a request_id plus shape-only fields
# and a sentinel prompt attribute, then asserts the debug exporter's stdout
# keeps the request_id and shape-only fields, drops the prompt content, and
# redacts without emitting the redacted key names (summary: info).
# The same trace carries vLLM-shaped llm_request spans (vLLM's gen_ai.* names
# and chatcmpl- prefix); the test asserts the Collector maps them onto the
# spine's names, adds model_id and sets slo_met per span at the goodput gates
# (ADR-0026). The spanmetrics connector's output is asserted too: the e2e latency
# histogram, with exemplars, and the request counts, both split by slo_met and
# the replica's k8s.pod.name. The Collector's own metrics on :8888 must count what the
# debug exporter sent, spans and metric points. The Grafana Cloud exporters are swapped for the
# debug exporter (test/otel-spine-sinks.yaml), so nothing leaves the laptop;
# before that, the cluster config alone must start without a warning or error.
# Manual only: needs Docker.
# No mocks: a real collector, real OTLP, the cluster's real config.
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
config="${repo_root}/k8s/otel-collector-config.yaml"
sinks="${repo_root}/test/otel-spine-sinks.yaml"
image="${OTEL_COLLECTOR_IMAGE:-otel/opentelemetry-collector-contrib:0.160.0}"
container="otel-spine-test-$$"
http_port=14318
metrics_port=18888

# The prompt content that must never reach the exporter. Shape-only fields that
# must survive. request_id is what the spine exists to carry.
prompt_sentinel="SECRET_PROMPT_CONTENT_do_not_leak"
request_id="req-abc-123"
model_id="Qwen/Qwen2.5-0.5B-Instruct"
prefix_hash="9f86d081"
# The model the Collector stamps on vLLM spans as model_id; the cluster sets it
# from model.yaml (just obs-up), the test from this value.
collector_model_id="test-org/test-model"
# Stands in for the vLLM replica's pod name, its resource's k8s.pod.name.
pod_name="vllm-gpu-test-pod"
# A resource attribute off the allowlist, which the guard must drop like a span's.
resource_sentinel="SECRET_RESOURCE_do_not_leak"

cleanup() {
  docker rm -f "${container}" >/dev/null 2>&1 || true
}
trap cleanup EXIT

# Runs the command given as arguments once a second until it succeeds, for up to 30 s.
wait_for() {
  local _
  for _ in $(seq 30); do
    if "$@"; then
      return 0
    fi
    sleep 1
  done
  return 1
}
# The collector has logged that its pipelines are running.
collector_started() {
  grep -qF "Everything is ready" <<<"$(docker logs "${container}" 2>&1)"
}
# The OTLP/HTTP receiver accepts connections. A 400 (bad body) still proves the
# port is live and serving; connection refused means not ready yet.
otlp_receiver_live() {
  curl -s -o /dev/null -X POST "http://localhost:${http_port}/v1/traces" \
    -H 'Content-Type: application/json' -d '{}'
}

# The Grafana Cloud exporters and auth extensions resolve their ${env:...} at
# start; placeholders let the config load, and nothing is sent to them.
collector_env=(
  -e "MODEL_ID=${collector_model_id}"
  -e "GRAFANA_CLOUD_TRACES_ENDPOINT=unused.invalid:443"
  -e "GRAFANA_CLOUD_TRACES_USERNAME=unused"
  -e "GRAFANA_CLOUD_METRICS_URL=https://unused.invalid/api/prom/push"
  -e "GRAFANA_CLOUD_METRICS_USERNAME=unused"
  -e "GRAFANA_CLOUD_WRITE_TOKEN=unused"
)

# The overlay below replaces the exporter and extension lists, so the cluster's
# own pipelines, auth extensions and Grafana Cloud exporters start only here: the
# config must come up and log no warning or error doing so.
echo "==> starting collector on the cluster config alone (${image})"
docker run -d --name "${container}" "${collector_env[@]}" \
  -v "${config}:/etc/otelcol-contrib/config.yaml:ro" \
  "${image}" --config=/etc/otelcol-contrib/config.yaml >/dev/null
started=1
wait_for collector_started || started=""
startup_logs="$(docker logs "${container}" 2>&1)"
if [[ -z "${started}" ]] || grep -qE $'\t(warn|error)\t' <<<"${startup_logs}"; then
  echo "FAIL: the cluster config did not start cleanly" >&2
  echo "${startup_logs}" >&2
  exit 1
fi
cleanup

echo "==> starting collector with the test sinks (${image})"
docker run -d --name "${container}" "${collector_env[@]}" \
  -p "${http_port}:4318" \
  -p "${metrics_port}:8888" \
  -v "${config}:/etc/otelcol-contrib/config.yaml:ro" \
  -v "${sinks}:/etc/otelcol-contrib/sinks.yaml:ro" \
  "${image}" \
  --config=/etc/otelcol-contrib/config.yaml \
  --config=/etc/otelcol-contrib/sinks.yaml >/dev/null

echo "==> waiting for OTLP/HTTP receiver"
if ! wait_for otlp_receiver_live; then
  echo "FAIL: collector OTLP/HTTP receiver never came up" >&2
  docker logs "${container}" >&2 || true
  exit 1
fi

# One vLLM llm_request span as vLLM v0.29.0 emits it: request id with the
# chatcmpl- prefix, latencies in seconds as doubles, token counts as ints.
# Args: X-Request-Id, span id, TTFT (s), e2e (s), completion tokens.
vllm_span() {
  cat <<JSON
{
  "traceId": "5b8efff798038103d269b633813fc60c",
  "spanId": "$2",
  "name": "llm_request",
  "kind": 2,
  "startTimeUnixNano": "1700000000000000000",
  "endTimeUnixNano": "1700000002000000000",
  "attributes": [
    {"key": "gen_ai.request.id", "value": {"stringValue": "chatcmpl-$1"}},
    {"key": "gen_ai.latency.time_to_first_token", "value": {"doubleValue": $3}},
    {"key": "gen_ai.latency.e2e", "value": {"doubleValue": $4}},
    {"key": "gen_ai.latency.time_in_queue", "value": {"doubleValue": 0.01}},
    {"key": "gen_ai.usage.prompt_tokens", "value": {"intValue": "42"}},
    {"key": "gen_ai.usage.completion_tokens", "value": {"intValue": "$5"}},
    {"key": "gen_ai.request.max_tokens", "value": {"intValue": "256"}}
  ]
}
JSON
}

# Goodput cases at the gates (TTFT <= 1.0 s and TPOT <= 0.05 s, TPOT =
# (e2e - TTFT) / (completion_tokens - 1)), each keyed by its X-Request-Id.
# Both gates exactly on the boundary: TTFT 1.0 s, TPOT 1.0 / 20 = 0.05 s.
vllm_met="vllm-met-001"
# TPOT 0.8 / 20 = 0.04 s holds; TTFT 1.2 s misses.
vllm_ttft_miss="vllm-ttft-miss-002"
# TTFT holds; TPOT 1.1 / 20 = 0.055 s misses.
vllm_tpot_miss="vllm-tpot-miss-003"
# One completion token, TPOT undefined: counts as 0, so only TTFT decides.
vllm_one_token_met="vllm-one-token-met-004"
vllm_one_token_miss="vllm-one-token-miss-005"
# No e2e latency, so TPOT cannot be computed: the gate fails closed.
vllm_no_e2e="vllm-no-e2e-006"
# No completion tokens counts as one: TPOT 0, TTFT 0.5 s holds.
vllm_zero_token_met="vllm-zero-token-met-007"
# One completion token and no TTFT latency: TTFT alone decides, and fails closed.
vllm_one_token_no_ttft="vllm-one-token-no-ttft-008"

echo "==> sending OTLP trace with request_id + shape-only fields + prompt sentinel + vLLM spans"
payload=$(
  cat <<JSON
{
  "resourceSpans": [{
    "resource": {"attributes": [
      {"key": "service.name", "value": {"stringValue": "vllm"}},
      {"key": "k8s.pod.name", "value": {"stringValue": "${pod_name}"}},
      {"key": "host.name", "value": {"stringValue": "${resource_sentinel}"}}
    ]},
    "scopeSpans": [{
      "spans": [{
        "traceId": "5b8efff798038103d269b633813fc60c",
        "spanId": "eee19b7ec3c1b174",
        "name": "chat.completion",
        "kind": 2,
        "startTimeUnixNano": "1700000000000000000",
        "endTimeUnixNano": "1700000000100000000",
        "attributes": [
          {"key": "request_id", "value": {"stringValue": "${request_id}"}},
          {"key": "model_id", "value": {"stringValue": "${model_id}"}},
          {"key": "prompt_tokens", "value": {"intValue": "42"}},
          {"key": "completion_tokens", "value": {"intValue": "128"}},
          {"key": "prefix_hash", "value": {"stringValue": "${prefix_hash}"}},
          {"key": "slo_met", "value": {"boolValue": true}},
          {"key": "prompt", "value": {"stringValue": "${prompt_sentinel}"}}
        ]
      },
      $(vllm_span "${vllm_met}" "0000000000000001" 1.0 2.0 21),
      $(vllm_span "${vllm_ttft_miss}" "0000000000000002" 1.2 2.0 21),
      $(vllm_span "${vllm_tpot_miss}" "0000000000000003" 0.5 1.6 21),
      $(vllm_span "${vllm_one_token_met}" "0000000000000004" 0.5 3.0 1),
      $(vllm_span "${vllm_one_token_miss}" "0000000000000005" 1.5 3.0 1),
      $(vllm_span "${vllm_no_e2e}" "0000000000000006" 0.5 2.0 21 | grep -vF '"gen_ai.latency.e2e"'),
      $(vllm_span "${vllm_zero_token_met}" "0000000000000007" 0.5 2.0 0),
      $(vllm_span "${vllm_one_token_no_ttft}" "0000000000000008" 0.5 2.0 1 | grep -vF '"gen_ai.latency.time_to_first_token"')]
    }]
  }]
}
JSON
)

curl -sf -X POST "http://localhost:${http_port}/v1/traces" \
  -H 'Content-Type: application/json' -d "${payload}" >/dev/null

# The batch processor flushes on its timeout, then spanmetrics on its 1 s
# interval; give both room before reading logs.
sleep 5
logs="$(docker logs "${container}" 2>&1)"

fail=0
assert_present() {
  if ! grep -qF "$1" <<<"${logs}"; then
    echo "FAIL: expected '$1' on the exporter output, not found" >&2
    fail=1
  fi
}
assert_absent() {
  if grep -qF "$1" <<<"${logs}"; then
    echo "FAIL: '$1' leaked to the exporter output (shape-only violated)" >&2
    fail=1
  fi
}
# The exported attributes of the span whose request_id is $1, one per line as
# the debug exporter renders them ("key: Type(value)"). The detailed output
# starts each span at a "Span #" line and lists its attributes as "-> " lines;
# a "Metric #" line ends one too, so span-metric attributes never join the last
# span's block.
span_attributes() {
  awk -v id="$1" '
    function flush() { if (found) printf "%s", block; block = ""; found = 0 }
    /Span #|Metric #/ { flush() }
    /-> / { line = $0; sub(/.*-> /, "", line); block = block line "\n" }
    index($0, "-> request_id: Str(" id ")") { found = 1 }
    END { flush() }
  ' <<<"${logs}"
}
# The span whose request_id is $1 carries the exported attribute $2 exactly.
assert_span_attribute() {
  if ! grep -qxF "$2" <<<"$(span_attributes "$1")"; then
    echo "FAIL: span ${1} lacks '$2' on the exporter output" >&2
    fail=1
  fi
}
# The span whose request_id is $1 carries no exported attribute named $2.
assert_span_lacks() {
  if grep -q "^$2: " <<<"$(span_attributes "$1")"; then
    echo "FAIL: span ${1} carries '$2' on the exporter output" >&2
    fail=1
  fi
}

# The resource attributes of the exported spans, one per line, as the debug
# exporter renders them between "Resource attributes:" and "ScopeSpans #". Only
# "ResourceSpans #" blocks count: the span metrics copy their spans' resource, so
# their blocks would answer for spans that lost an attribute.
resource_attributes() {
  awk '
    /ResourceSpans #/ { in_spans = 1 }
    /ResourceMetrics #/ { in_spans = 0 }
    in_spans && /Resource attributes:/ { in_resource = 1; next }
    /ScopeSpans #/ { in_resource = 0 }
    in_resource && /-> / { line = $0; sub(/.*-> /, "", line); print line }
  ' <<<"${logs}"
}

# The unit the debug exporter renders for metric $1, from the "-> Unit:" line
# that follows its "-> Name:" line.
metric_unit() {
  awk -v name="$1" '
    /-> Name: / { line = $0; sub(/.*-> Name: /, "", line); in_metric = (line == name) }
    in_metric && /-> Unit: / { line = $0; sub(/.*-> Unit: /, "", line); print line; exit }
  ' <<<"${logs}"
}

# One line per spanmetrics data point of metric $1, as the debug exporter renders
# it: its attributes, then its value (a sum's "Value:", a histogram's "Count:")
# and, last, "exemplars=<n>", all space-separated. Each data point starts at a
# "DataPoints #" line inside a "Metric #" block named by its "-> Name:" line.
metric_points() {
  awk -v name="$1" '
    function flush() { if (in_point) print point " exemplars=" exemplars; in_point = 0 }
    /Metric #|ResourceMetrics #|ResourceSpans #/ { flush(); metric = "" }
    /-> Name: / { line = $0; sub(/.*-> Name: /, "", line); metric = line }
    /DataPoints #/ { flush(); if (metric == name) { in_point = 1; point = ""; exemplars = 0 } ; next }
    in_point && /-> / { line = $0; sub(/.*-> /, "", line); point = point line " " }
    in_point && /^(Value|Count): / { point = point $0 " " }
    in_point && /Exemplar #/ { exemplars++ }
    END { flush() }
  ' <<<"${logs}"
}
# Some data point of metric $1 carries every term given as the remaining
# arguments, each matched whole.
assert_metric_point() {
  local metric="$1" point found=""
  shift
  while IFS= read -r point; do
    local term matched=1
    for term in "$@"; do
      [[ " ${point} " == *" ${term} "* ]] || matched=""
    done
    if [[ -n "${matched}" ]]; then
      found=1
      break
    fi
  done <<<"$(metric_points "${metric}")"
  if [[ -z "${found}" ]]; then
    echo "FAIL: no ${metric} data point carries: $*" >&2
    fail=1
  fi
}

# request_id present on the exporter output (acceptance criterion 2).
assert_present "${request_id}"
# Shape-only fields survive redaction — a string field and an int field, since
# redaction treats the two attribute types on separate paths.
assert_present "${model_id}"
assert_present "${prefix_hash}"
assert_present "prompt_tokens"
# No raw prompt content leaves the collector (acceptance criterion 3).
assert_absent "${prompt_sentinel}"
# Nor does a resource attribute off the allowlist.
assert_absent "${resource_sentinel}"
# The guard actually ran (it counted the disallowed keys it dropped) and does
# so without emitting the redacted key names — summary: info leaks counts only,
# never names. A regression to summary: debug would surface "redacted.keys" and
# fail here, before a key name like "prompt" could reach a sink.
assert_present "redaction.redacted.count"
assert_absent "redaction.redacted.keys"

# vLLM spans reach the exporter under the spine's names: request_id is the
# client's X-Request-Id with the chatcmpl- prefix stripped, model_id is the one
# the Collector was given, and the token counts keep their values.
assert_span_attribute "${vllm_met}" "model_id: Str(${collector_model_id})"
assert_span_attribute "${vllm_met}" "prompt_tokens: Int(42)"
assert_span_attribute "${vllm_met}" "completion_tokens: Int(21)"
# Every transform statement ran: error_mode: ignore logs a failing statement as
# a warning and carries on, which would otherwise pass unnoticed.
if grep -qF "failed to execute statement" <<<"${logs}"; then
  echo "FAIL: a transform statement failed on the collector (see its warn log)" >&2
  fail=1
fi
assert_absent "chatcmpl-"
assert_absent "gen_ai."
# slo_met per request at the joint gate, both gates inclusive.
assert_span_attribute "${vllm_met}" "slo_met: Bool(true)"
assert_span_attribute "${vllm_ttft_miss}" "slo_met: Bool(false)"
assert_span_attribute "${vllm_tpot_miss}" "slo_met: Bool(false)"
assert_span_attribute "${vllm_one_token_met}" "slo_met: Bool(true)"
assert_span_attribute "${vllm_one_token_miss}" "slo_met: Bool(false)"
assert_span_attribute "${vllm_no_e2e}" "slo_met: Bool(false)"
assert_span_attribute "${vllm_zero_token_met}" "slo_met: Bool(true)"
assert_span_attribute "${vllm_one_token_no_ttft}" "slo_met: Bool(false)"
# Spans already in the spine's names are not vLLM spans: they pass through
# unchanged, and an slo_met they arrive with is dropped, since only the
# Collector sets it.
assert_span_attribute "${request_id}" "prefix_hash: Str(${prefix_hash})"
assert_span_lacks "${request_id}" "slo_met"

# The service and replica survive redaction on the resource, so Grafana Cloud
# Traces finds a trace by its replica and spanmetrics splits by it.
for attribute in "service.name: Str(vllm)" "k8s.pod.name: Str(${pod_name})"; do
  if ! grep -qxF "${attribute}" <<<"$(resource_attributes)"; then
    echo "FAIL: the exported resource lacks '${attribute}'" >&2
    fail=1
  fi
done

# spanmetrics: the e2e latency histogram, in seconds, and the request counts,
# both split by slo_met and the replica. Of the eight vLLM spans three meet the
# SLO and five miss it. The histogram's data points carry exemplars, the trace
# a latency panel opens.
if [[ "$(metric_unit "traces.span.metrics.duration")" != "s" ]]; then
  echo "FAIL: the spanmetrics duration histogram is not in seconds" >&2
  fail=1
fi
assert_metric_point "traces.span.metrics.calls" "span.name: Str(llm_request)" \
  "k8s.pod.name: Str(${pod_name})" "slo_met: Bool(true)" "Value: 3"
assert_metric_point "traces.span.metrics.calls" "span.name: Str(llm_request)" \
  "k8s.pod.name: Str(${pod_name})" "slo_met: Bool(false)" "Value: 5"
assert_metric_point "traces.span.metrics.duration" "span.name: Str(llm_request)" \
  "k8s.pod.name: Str(${pod_name})" "slo_met: Bool(true)" "Count: 3"
assert_metric_point "traces.span.metrics.duration" "span.name: Str(llm_request)" \
  "k8s.pod.name: Str(${pod_name})" "slo_met: Bool(false)" "Count: 5"
if ! grep -q "slo_met: Bool(true) .*exemplars=[1-9]" <<<"$(metric_points "traces.span.metrics.duration")"; then
  echo "FAIL: the spanmetrics duration histogram carries no exemplars" >&2
  fail=1
fi

# The Collector serves its own metrics on :8888 to other hosts, as a port-forward
# and the cluster's Prometheus need, under the names `just obs-pivot` checks: each
# exporter's sent count, without a _total suffix.
self_metrics="$(curl -sf "http://localhost:${metrics_port}/metrics" || true)"
for counter in 'otelcol_exporter_sent_spans{exporter="debug"}' 'otelcol_exporter_sent_metric_points{exporter="debug"}'; do
  if [[ ! "$(awk -v c="${counter}" '$1 == c { print $2 }' <<<"${self_metrics}")" =~ ^[1-9] ]]; then
    echo "FAIL: the Collector's :8888 metrics lack a nonzero ${counter}" >&2
    fail=1
  fi
done

if [[ "${fail}" -ne 0 ]]; then
  echo "---- collector logs ----" >&2
  echo "${logs}" >&2
  exit 1
fi

echo "PASS: request_id + shape-only fields exported, prompt content dropped, vLLM spans mapped with slo_met, spanmetrics split by slo_met and pod, exporter counts on :8888"
