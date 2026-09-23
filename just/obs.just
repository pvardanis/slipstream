# Observability lifecycle: the OTel Collector spine stub deploy, teardown, and trace pivot.

# Deploy the OTel Collector spine stub to the cluster.
obs-up:
    kubectl create namespace slipstream --dry-run=client -o yaml | kubectl apply -f -
    kubectl create configmap otel-collector-config -n slipstream \
      --from-file=config.yaml={{ otel_config }} \
      --dry-run=client -o yaml | kubectl apply -f -
    kubectl apply -f {{ otel_manifests }}
    kubectl -n slipstream rollout status deploy/otel-collector --timeout=120s

# Remove the OTel Collector (leaves the cluster running).
obs-down:
    kubectl delete configmap otel-collector-config -n slipstream --ignore-not-found
    kubectl delete -f {{ otel_manifests }} --ignore-not-found

# Send one shape-only OTLP trace through the cluster collector and show its request_id log line.
obs-pivot:
    #!/usr/bin/env bash
    set -euo pipefail
    kubectl -n slipstream rollout status deploy/otel-collector --timeout=120s
    kubectl -n slipstream port-forward svc/otel-collector 4318:4318 >/dev/null 2>&1 &
    pf_pid=$!
    trap 'kill "${pf_pid}" 2>/dev/null || true' EXIT
    ready=""
    for _ in $(seq 30); do
      if curl -s -o /dev/null -X POST http://localhost:4318/v1/traces \
        -H 'Content-Type: application/json' -d '{}'; then
        ready=1
        break
      fi
      sleep 1
    done
    if [[ -z "${ready}" ]]; then
      echo "port-forward to svc/otel-collector never came up" >&2
      exit 1
    fi
    # The demo trace's model_id names the served model; read it from model.yaml, the
    # source of truth the serving manifest is held to.
    model="$({{ image_tag_tool }} hf-id)"
    curl -sf -X POST http://localhost:4318/v1/traces \
      -H 'Content-Type: application/json' \
      -d '{"resourceSpans":[{"resource":{"attributes":[{"key":"service.name","value":{"stringValue":"vllm"}}]},"scopeSpans":[{"spans":[{"traceId":"5b8efff798038103d269b633813fc60c","spanId":"eee19b7ec3c1b174","name":"chat.completion","kind":2,"startTimeUnixNano":"1700000000000000000","endTimeUnixNano":"1700000000100000000","attributes":[{"key":"request_id","value":{"stringValue":"req-demo-001"}},{"key":"model_id","value":{"stringValue":"'"${model}"'"}},{"key":"prompt_tokens","value":{"intValue":"42"}},{"key":"completion_tokens","value":{"intValue":"128"}},{"key":"prefix_hash","value":{"stringValue":"9f86d081"}}]}]}]}]}'
    sleep 3
    kubectl -n slipstream logs deploy/otel-collector | grep -A12 'request_id'
