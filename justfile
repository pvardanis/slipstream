# Task runner for the slipstream platform. `just up` conjures the cluster;
# `just down` returns spend to zero. Credentials come from your AWS_PROFILE.
# Run `just` with no args to list recipes.

eks_dir := "terraform/eks"
bootstrap_dir := "terraform/bootstrap"
manifests := "k8s/vllm.yaml"
model := "Qwen/Qwen2.5-0.5B-Instruct"

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

# Destroy the cluster (the bootstrap state bucket is left intact).
down:
    terraform -chdir={{ eks_dir }} destroy -auto-approve

# Apply the state-bootstrap stack once, before the first `just up`.
bootstrap:
    terraform -chdir={{ bootstrap_dir }} init
    terraform -chdir={{ bootstrap_dir }} apply -auto-approve
