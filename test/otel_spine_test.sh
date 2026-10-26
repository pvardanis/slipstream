#!/usr/bin/env bash
# Integration test for the request-ID spine stub: runs the real OTel Collector
# (the same k8s/otel-collector-config.yaml the cluster deploys) in a local
# container, pushes one OTLP trace carrying a request_id plus shape-only fields
# and a sentinel prompt attribute, then asserts the debug exporter's stdout
# keeps the request_id and shape-only fields, drops the prompt content, and
# redacts without emitting the redacted key names (summary: info).
# The same trace carries vLLM-shaped llm_request spans (vLLM's gen_ai.* names
# and chatcmpl- prefix); the test asserts the Collector maps them onto the
# spine's names, adds model_id and sets slo_met per span at the goodput gates
# (ADR-0026). Manual only: needs Docker.
# No mocks: a real collector, real OTLP, the cluster's real config.
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
config="${repo_root}/k8s/otel-collector-config.yaml"
image="${OTEL_COLLECTOR_IMAGE:-otel/opentelemetry-collector-contrib:0.160.0}"
container="otel-spine-test-$$"
http_port=14318

# The prompt content that must never reach the exporter. Shape-only fields that
# must survive. request_id is the spine the whole stub exists to carry.
prompt_sentinel="SECRET_PROMPT_CONTENT_do_not_leak"
request_id="req-abc-123"
model_id="Qwen/Qwen2.5-0.5B-Instruct"
prefix_hash="9f86d081"
# The model the Collector stamps on vLLM spans as model_id; the cluster sets it
# from model.yaml (just obs-up), the test from this value.
collector_model_id="test-org/test-model"

cleanup() {
  docker rm -f "${container}" >/dev/null 2>&1 || true
}
trap cleanup EXIT

echo "==> starting collector (${image})"
docker run -d --name "${container}" \
  -p "${http_port}:4318" \
  -e "MODEL_ID=${collector_model_id}" \
  -v "${config}:/etc/otelcol-contrib/config.yaml:ro" \
  "${image}" >/dev/null

# Wait for the OTLP/HTTP receiver to accept connections. A 400 (bad body) still
# proves the port is live and serving; connection refused means not ready yet.
echo "==> waiting for OTLP/HTTP receiver"
ready=""
for _ in $(seq 30); do
  if curl -s -o /dev/null -X POST "http://localhost:${http_port}/v1/traces" \
    -H 'Content-Type: application/json' -d '{}'; then
    ready=1
    break
  fi
  sleep 1
done
if [[ -z "${ready}" ]]; then
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
# TPOT holds at 0.05 s; TTFT 1.2 s misses.
vllm_ttft_miss="vllm-ttft-miss-002"
# TTFT holds; TPOT 1.1 / 20 = 0.055 s misses.
vllm_tpot_miss="vllm-tpot-miss-003"
# One completion token, TPOT undefined: counts as 0, so only TTFT decides.
vllm_one_token_met="vllm-one-token-met-004"
vllm_one_token_miss="vllm-one-token-miss-005"
# No e2e latency, so TPOT cannot be computed: the gate fails closed.
vllm_no_e2e="vllm-no-e2e-006"

echo "==> sending OTLP trace with request_id + shape-only fields + prompt sentinel + vLLM spans"
payload=$(
  cat <<JSON
{
  "resourceSpans": [{
    "resource": {"attributes": [
      {"key": "service.name", "value": {"stringValue": "vllm"}}
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
          {"key": "prompt", "value": {"stringValue": "${prompt_sentinel}"}}
        ]
      },
      $(vllm_span "${vllm_met}" "0000000000000001" 1.0 2.0 21),
      $(vllm_span "${vllm_ttft_miss}" "0000000000000002" 1.2 2.2 21),
      $(vllm_span "${vllm_tpot_miss}" "0000000000000003" 0.5 1.6 21),
      $(vllm_span "${vllm_one_token_met}" "0000000000000004" 0.5 3.0 1),
      $(vllm_span "${vllm_one_token_miss}" "0000000000000005" 1.5 3.0 1),
      $(vllm_span "${vllm_no_e2e}" "0000000000000006" 0.5 2.0 21 | grep -vF '"gen_ai.latency.e2e"')]
    }]
  }]
}
JSON
)

curl -sf -X POST "http://localhost:${http_port}/v1/traces" \
  -H 'Content-Type: application/json' -d "${payload}" >/dev/null

# The batch processor flushes on its timeout; give it room before reading logs.
sleep 3
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
# starts each span at a "Span #" line and lists its attributes as "-> " lines.
span_attributes() {
  awk -v id="$1" '
    function flush() { if (found) printf "%s", block; block = ""; found = 0 }
    /Span #/ { flush() }
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

# request_id present on the exporter output (acceptance criterion 2).
assert_present "${request_id}"
# Shape-only fields survive redaction — a string field and an int field, since
# redaction treats the two attribute types on separate paths.
assert_present "${model_id}"
assert_present "${prefix_hash}"
assert_present "prompt_tokens"
# No raw prompt content leaves the collector (acceptance criterion 3).
assert_absent "${prompt_sentinel}"
# The guard actually ran (it redacted exactly the one disallowed key) and does
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
assert_absent "chatcmpl-"
assert_absent "gen_ai."
# slo_met per request at the joint gate, both gates inclusive.
assert_span_attribute "${vllm_met}" "slo_met: Bool(true)"
assert_span_attribute "${vllm_ttft_miss}" "slo_met: Bool(false)"
assert_span_attribute "${vllm_tpot_miss}" "slo_met: Bool(false)"
assert_span_attribute "${vllm_one_token_met}" "slo_met: Bool(true)"
assert_span_attribute "${vllm_one_token_miss}" "slo_met: Bool(false)"
assert_span_attribute "${vllm_no_e2e}" "slo_met: Bool(false)"
# Spans already in the spine's names are not vLLM spans: they pass through
# unchanged and get no slo_met.
assert_span_attribute "${request_id}" "prefix_hash: Str(${prefix_hash})"
assert_span_lacks "${request_id}" "slo_met"

if [[ "${fail}" -ne 0 ]]; then
  echo "---- collector logs ----" >&2
  echo "${logs}" >&2
  exit 1
fi

echo "PASS: request_id + shape-only fields exported, prompt content dropped, vLLM spans mapped with slo_met"
