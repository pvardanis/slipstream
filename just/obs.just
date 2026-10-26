# Observability lifecycle: the persistent grafana Terraform stack, and the OTel Collector spine stub deploy, teardown, and trace pivot.

# Deploy the OTel Collector spine stub to the cluster.
[group('obs')]
obs-up:
    #!/usr/bin/env bash
    set -euo pipefail
    kubectl create namespace slipstream --dry-run=client -o yaml | kubectl apply -f -
    # The Collector stamps model_id on vLLM spans from model-id; model.yaml is its
    # source of truth, as for the serving manifest.
    model="$({{ image_tag_tool }} hf-id)"
    kubectl create configmap otel-collector-config -n slipstream \
      --from-file=config.yaml={{ otel_config }} \
      --from-literal=model-id="${model}" \
      --dry-run=client -o yaml | kubectl apply -f -
    kubectl apply -f {{ otel_manifests }}
    # The Collector reads config.yaml and MODEL_ID only at start, and an unchanged
    # Deployment does not roll; restart so a changed config or model takes effect.
    kubectl -n slipstream rollout restart deploy/otel-collector
    kubectl -n slipstream rollout status deploy/otel-collector --timeout=120s

# Remove the OTel Collector (leaves the cluster running).
[group('obs')]
obs-down:
    kubectl delete configmap otel-collector-config -n slipstream --ignore-not-found
    kubectl delete -f {{ otel_manifests }} --ignore-not-found

# Send one shape-only OTLP trace through the cluster collector and show its request_id log line.
[group('obs')]
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

# The Terraform stack outlives `just cluster-down`, so it has no down recipe. Requires
# `just grafana-cloud-setup` done.
# Apply the persistent grafana Terraform stack: its data sources (ADR-0026).
[group('obs')]
grafana-up: _bootstrap-init
    #!/usr/bin/env bash
    set -euo pipefail
    # Bare assignment so a failed lookup aborts rather than feeding an empty bucket
    # into terraform (see `cluster-up`).
    bucket="$(terraform -chdir={{ bootstrap_dir }} output -raw state_bucket_name)"
    [[ -n "${bucket}" ]] || { echo "bootstrap output state_bucket_name is empty" >&2; exit 1; }
    terraform -chdir={{ grafana_dir }} init -input=false -backend-config="bucket=${bucket}"
    # No -auto-approve: the Terraform stack persists across sessions, so its plan is
    # read before it changes what Grafana Cloud holds.
    terraform -chdir={{ grafana_dir }} apply -var="state_bucket=${bucket}"
