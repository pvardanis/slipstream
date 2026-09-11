# Task runner for the slipstream platform. `just up` conjures the cluster;
# `just down` returns spend to zero. Credentials come from your AWS_PROFILE.
# Run `just` with no args to list recipes.

eks_dir := "terraform/eks"
bootstrap_dir := "terraform/bootstrap"
manifests := "k8s/vllm.yaml"
bench_client := "k8s/bench-client.yaml"
bench_dockerfile := "bench/Dockerfile"
# Reused across rebuilds; the pod pulls it with imagePullPolicy: Always.
bench_image_tag := "latest"
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

# Show the cluster changes `just up` would apply, without provisioning anything.
plan:
    terraform -chdir={{ eks_dir }} init \
      -backend-config="bucket=$(terraform -chdir={{ bootstrap_dir }} output -raw state_bucket_name)"
    terraform -chdir={{ eks_dir }} plan

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

# Print the bench-client image reference (ECR repo URL from `just bootstrap`, plus the tag).
_bench-image-ref:
    #!/usr/bin/env bash
    set -euo pipefail
    # Bare assignment (not `local`/`export`) so `set -e` aborts on a terraform
    # failure — a missing bootstrap state or AWS auth error surfaces here instead
    # of collapsing to a bogus `:latest` ref that the caller would apply blindly.
    repo="$(terraform -chdir={{ bootstrap_dir }} output -raw bench_image_repo_url)"
    echo "${repo}:{{ bench_image_tag }}"

# Build the bench-client image and push it to its ECR repo (provisioned by `just bootstrap`).
bench-image:
    #!/usr/bin/env bash
    set -euo pipefail
    repo="$(terraform -chdir={{ bootstrap_dir }} output -raw bench_image_repo_url)"
    region="$(terraform -chdir={{ bootstrap_dir }} output -raw region)"
    # The registry host is the repo URL without its trailing repository path.
    registry="${repo%%/*}"
    aws ecr get-login-password --region "${region}" \
      | docker login --username AWS --password-stdin "${registry}"
    # Nodes are amd64; build for that arch regardless of the developer's host.
    # Drive the baked tokenizer from the one `model` var the sweep also uses, so
    # the two never diverge.
    docker build --platform linux/amd64 --build-arg MODEL={{ model }} \
      -t "${repo}:{{ bench_image_tag }}" -f {{ bench_dockerfile }} .
    docker push "${repo}:{{ bench_image_tag }}"

# Sweep `vllm bench serve` (prefix-share % x burstiness) from an in-cluster client pod, saving per-cell JSON to bench/results.
bench *args:
    #!/usr/bin/env bash
    set -euo pipefail
    kubectl -n slipstream rollout status deploy/vllm --timeout=600s
    # Clear a pod left behind by a prior run that was killed before its cleanup trap
    # fired, so `kubectl wait` doesn't block for the full timeout on a stale pod.
    kubectl -n slipstream delete -f {{ bench_client }} --ignore-not-found >/dev/null 2>&1 || true
    BENCH_IMAGE="$(just _bench-image-ref)"
    export BENCH_IMAGE
    envsubst '${BENCH_IMAGE}' <{{ bench_client }} | kubectl -n slipstream apply -f -
    trap 'kubectl -n slipstream delete -f {{ bench_client }} --ignore-not-found 2>/dev/null || true' EXIT
    if ! kubectl -n slipstream wait --for=condition=Ready pod/bench-client --timeout=180s; then
      echo "bench-client pod did not become Ready:" >&2
      kubectl -n slipstream describe pod/bench-client >&2 || true
      exit 1
    fi
    # serve-sweep writes one JSON per successful cell and only exits non-zero at
    # the end of the grid, so copy back whatever landed even on a partial failure,
    # then surface the sweep's own exit code. A dry run writes no results directory,
    # so the copy-back is skipped. The baked image carries the slipstream-bench
    # console script, so nothing is copied in.
    sweep_rc=0
    kubectl -n slipstream exec bench-client -- \
      slipstream-bench serve-sweep \
        --base-url http://vllm.slipstream.svc:8000 \
        --model {{ model }} \
        --out-dir /tmp/results {{ args }} || sweep_rc=$?
    if kubectl -n slipstream exec bench-client -- test -d /tmp/results; then
      mkdir -p bench/results
      kubectl -n slipstream exec bench-client -- tar cf - -C /tmp/results . | tar xf - -C bench/results
      count="$(kubectl -n slipstream exec bench-client -- sh -c 'ls -1 /tmp/results/*.json 2>/dev/null | wc -l' | tr -d ' ')"
      if [[ "${count}" -eq 0 ]]; then
        echo "sweep produced no result JSON in the client pod — check the exec output above" >&2
        # Preserve a config-error exit (e.g. 2) the sweep already reported; only
        # synthesize a failure code when the sweep itself claimed success.
        if [[ "${sweep_rc}" -ne 0 ]]; then
          exit "${sweep_rc}"
        fi
        exit 1
      fi
      echo "results copied to bench/results/ (${count} files)"
    fi
    exit "${sweep_rc}"

# Run the slipstream-bench Python test suite (no cluster).
cli-test:
    uv run pytest

# Assert the cost post-processor prices bench JSON into $/1M in/out (no cluster).
cost-test:
    bash test/cost_postprocessor_test.sh

# Assert the prefix-cache scraper computes the per-run delta hit rate and joins it (no cluster).
prefix-cache-test:
    bash test/prefix_cache_scrape_test.sh

# Scrape the prefix-cache hit rate for a cold and a warm run of one bench cell and join each to its client JSON (in bench/results/prefix-cache).
prefix-cache prefix_share="90" burstiness="1.0" *args="":
    #!/usr/bin/env bash
    set -euo pipefail
    base="http://vllm.slipstream.svc:8000"
    kubectl -n slipstream rollout status deploy/vllm --timeout=600s
    # Clear a pod left behind by a prior run that was killed before its cleanup trap
    # fired, so `kubectl wait` doesn't block for the full timeout on a stale pod.
    kubectl -n slipstream delete -f {{ bench_client }} --ignore-not-found >/dev/null 2>&1 || true
    BENCH_IMAGE="$(just _bench-image-ref)"
    export BENCH_IMAGE
    envsubst '${BENCH_IMAGE}' <{{ bench_client }} | kubectl -n slipstream apply -f -
    trap 'kubectl -n slipstream delete -f {{ bench_client }} --ignore-not-found 2>/dev/null || true' EXIT
    if ! kubectl -n slipstream wait --for=condition=Ready pod/bench-client --timeout=180s; then
      echo "bench-client pod did not become Ready:" >&2
      kubectl -n slipstream describe pod/bench-client >&2 || true
      exit 1
    fi
    out="bench/results/prefix-cache"
    mkdir -p "${out}"
    # A seed unique to this invocation makes prefix_repetition emit prefixes the server
    # has never cached, so the cold run genuinely misses. The cold and warm runs share
    # it, so the warm run replays the cold run's prefixes against the now-populated
    # cache. This build exposes no /reset_prefix_cache route to empty the cache instead.
    seed="$(date +%s)"
    # Snapshot the server's cumulative prefix-cache counters; only the delta across a
    # run window is that run's traffic, so we bracket each run with a snapshot.
    scrape() { kubectl -n slipstream exec bench-client -- curl -sf "${base}/metrics"; }
    # One bench cell, a single (prefix-share, burstiness) so the window holds one run.
    # This run is published as a cold/warm comparison, so its workload shape is picked
    # to make cache residency the only variable in the gap:
    #   --align-blocks 16 floors the prefix to whole 16-token blocks (vLLM's prefix
    #     cache reuses whole blocks only; a ragged tail recomputes every time in both
    #     regimes and dilutes the gap). 16 is vLLM's default block_size — revisit it
    #     if the served backend runs a different block size, or the alignment is wrong.
    #   --num-prefixes 16 raises the share of the cold run that is a genuine first
    #     exposure rather than a self-hit on a prefix the run itself just planted,
    #     widening the cold/warm gap (full isolation would need num-prefixes near
    #     num-prompts, which serve-sweep defaults to 100).
    # A caller can override either by appending its own flag after `just prefix-cache`.
    run_cell() {
      kubectl -n slipstream exec bench-client -- slipstream-bench serve-sweep \
        --base-url "${base}" --model {{ model }} \
        --prefix-share "{{ prefix_share }}" --burstiness "{{ burstiness }}" \
        --align-blocks 16 --num-prefixes 16 \
        --seed "${seed}" --out-dir "$1" {{ args }}
    }
    cell="pshare{{ prefix_share }}_burst{{ burstiness }}.json"
    # Cold: fresh, never-cached prefixes, so the run misses.
    scrape >"${out}/cold_before.prom"
    run_cell /tmp/results-cold
    scrape >"${out}/cold_after.prom"
    kubectl -n slipstream cp "bench-client:/tmp/results-cold/${cell}" "${out}/cold_${cell}"
    # Warm: the same seed, so the same prefixes hit the cache the cold run populated.
    scrape >"${out}/warm_before.prom"
    run_cell /tmp/results-warm
    scrape >"${out}/warm_after.prom"
    kubectl -n slipstream cp "bench-client:/tmp/results-warm/${cell}" "${out}/warm_${cell}"
    bash bench/prefix_cache_scrape.sh --cache-state cold \
      --metrics-before "${out}/cold_before.prom" --metrics-after "${out}/cold_after.prom" \
      --result "${out}/cold_${cell}" | tee "${out}/cold_hit_rate.json"
    bash bench/prefix_cache_scrape.sh --cache-state warm \
      --metrics-before "${out}/warm_before.prom" --metrics-after "${out}/warm_after.prom" \
      --result "${out}/warm_${cell}" | tee "${out}/warm_hit_rate.json"

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
