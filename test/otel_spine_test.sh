#!/usr/bin/env bash
# Integration test for the request-ID spine stub: runs the real OTel Collector
# (the same k8s/otel-collector-config.yaml the cluster deploys) in a local
# container, pushes one OTLP trace carrying a request_id plus shape-only fields
# and a sentinel prompt attribute, then asserts the debug exporter's stdout
# keeps the request_id and shape-only fields, drops the prompt content, and
# redacts without emitting the redacted key names (summary: info).
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

cleanup() {
  docker rm -f "${container}" >/dev/null 2>&1 || true
}
trap cleanup EXIT

echo "==> starting collector (${image})"
docker run -d --name "${container}" \
  -p "${http_port}:4318" \
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

echo "==> sending OTLP trace with request_id + shape-only fields + prompt sentinel"
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
      }]
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

if [[ "${fail}" -ne 0 ]]; then
  echo "---- collector logs ----" >&2
  echo "${logs}" >&2
  exit 1
fi

echo "PASS: request_id + shape-only fields exported, prompt content dropped"
