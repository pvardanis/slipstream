# Task runner for the slipstream platform. `just up` conjures the cluster;
# `just down` returns spend to zero. Credentials come from your AWS_PROFILE.
# Run `just` with no args to list recipes.

eks_dir := "terraform/eks"
bootstrap_dir := "terraform/bootstrap"
manifests := "k8s/vllm.yaml"
model := "Qwen/Qwen2.5-0.5B-Instruct"
otel_manifests := "k8s/otel-collector.yaml"
otel_config := "k8s/otel-collector-config.yaml"

# List available recipes.
default:
    @just --list

# Create the cluster and point kubectl at it.
up:
    terraform -chdir={{ eks_dir }} init \
      -backend-config="bucket=$(terraform -chdir={{ bootstrap_dir }} output -raw state_bucket_name)"
    terraform -chdir={{ eks_dir }} apply -auto-approve
    aws eks update-kubeconfig \
      --name $(terraform -chdir={{ eks_dir }} output -raw cluster_name) \
      --region $(terraform -chdir={{ eks_dir }} output -raw region)
    kubectl get nodes

# Deploy the CPU vLLM replica and wait for it to serve.
deploy:
    kubectl apply -f {{ manifests }}
    kubectl -n slipstream rollout status deploy/vllm --timeout=600s

# Remove the vLLM workload (leaves the cluster running).
undeploy:
    kubectl delete -f {{ manifests }} --ignore-not-found

# Port-forward the service and curl a completion out of it.
completion:
    #!/usr/bin/env bash
    set -euo pipefail
    kubectl -n slipstream rollout status deploy/vllm --timeout=600s
    kubectl -n slipstream port-forward svc/vllm 8000:8000 >/dev/null 2>&1 &
    pf_pid=$!
    trap 'kill "${pf_pid}" 2>/dev/null || true' EXIT
    for _ in $(seq 30); do
      curl -sf http://localhost:8000/health >/dev/null 2>&1 && break
      sleep 1
    done
    curl -sf http://localhost:8000/v1/completions \
      -H 'Content-Type: application/json' \
      -d '{"model":"{{ model }}","prompt":"The slipstream platform serves","max_tokens":32}'
    echo

# Assert the pre-PR review guard blocks an unreviewed `gh pr create` (no session).
guard-test:
    bash test/pr_review_guard_test.sh

# Run the request-ID spine stub against a local collector (real OTLP, no cluster).
obs-test:
    bash test/otel_spine_test.sh

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
    curl -sf -X POST http://localhost:4318/v1/traces \
      -H 'Content-Type: application/json' \
      -d '{"resourceSpans":[{"resource":{"attributes":[{"key":"service.name","value":{"stringValue":"vllm"}}]},"scopeSpans":[{"spans":[{"traceId":"5b8efff798038103d269b633813fc60c","spanId":"eee19b7ec3c1b174","name":"chat.completion","kind":2,"startTimeUnixNano":"1700000000000000000","endTimeUnixNano":"1700000000100000000","attributes":[{"key":"request_id","value":{"stringValue":"req-demo-001"}},{"key":"model_id","value":{"stringValue":"{{ model }}"}},{"key":"prompt_tokens","value":{"intValue":"42"}},{"key":"completion_tokens","value":{"intValue":"128"}},{"key":"prefix_hash","value":{"stringValue":"9f86d081"}}]}]}]}]}'
    sleep 3
    kubectl -n slipstream logs deploy/otel-collector | grep -A12 'request_id'

# Destroy the cluster (the bootstrap state bucket is left intact).
down:
    terraform -chdir={{ eks_dir }} destroy -auto-approve

# Apply the state-bootstrap stack once, before the first `just up`.
bootstrap:
    terraform -chdir={{ bootstrap_dir }} init
    terraform -chdir={{ bootstrap_dir }} apply -auto-approve
