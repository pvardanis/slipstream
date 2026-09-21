# Task runner for the slipstream platform. `just cluster-up` conjures the cluster;
# `just cluster-down` returns spend to zero. Credentials come from your AWS_PROFILE.
# Run `just` with no args to list recipes.

eks_dir := "terraform/eks"
bootstrap_dir := "terraform/bootstrap"
bench_endpoint_dir := "terraform/bench-endpoint"
manifests := "k8s/vllm.yaml"
gpu_pool_manifests := "k8s/gpu-node-pool.yaml"
gpu_manifests := "k8s/vllm-gpu.yaml"
bench_dockerfile := "bench/Dockerfile"
# Reused across rebuilds; the pod pulls it with imagePullPolicy: Always.
bench_image_tag := "latest"
cpu_model := "Qwen/Qwen2.5-0.5B-Instruct"
# The model id the GPU replica serves (k8s/vllm-gpu.yaml --model); the gpu-completion
# smoke sends it as the request's model field.
gpu_model := "Qwen/Qwen3-8B-AWQ"
# The model the external bench path measures — the bench-image tokenizer, `bench`
# and `prefix-cache` all read this one var so the baked tokenizer and the swept
# token counts never diverge. Defaults to the GPU rig's model, since the mTLS bench
# path exists to measure that rig (ADR-0004); override for a CPU-replica sweep with
# `just bench_model="..." bench-image bench`.
bench_model := gpu_model
otel_manifests := "k8s/otel-collector.yaml"
otel_config := "k8s/otel-collector-config.yaml"

# List available recipes.
default:
    @just --list

# Connect the bootstrap stack to its remote state (idempotent) so its outputs are
# readable — needed on a fresh checkout, where no local .terraform exists yet.
# Init progress goes to stderr so a recipe that captures a dependent's stdout
# (e.g. `image="$(just _bench-image-ref)"`) gets only the value, not this banner.
_bootstrap-init:
    terraform -chdir={{ bootstrap_dir }} init -input=false >&2

# Create the cluster and point kubectl at it.
cluster-up: _bootstrap-init
    #!/usr/bin/env bash
    set -euo pipefail
    # Bare assignments so a failed `terraform output` aborts the recipe instead of
    # collapsing to an empty backend/name the next command would use blindly.
    bucket="$(terraform -chdir={{ bootstrap_dir }} output -raw state_bucket_name)"
    terraform -chdir={{ eks_dir }} init -input=false -backend-config="bucket=${bucket}"
    terraform -chdir={{ eks_dir }} apply -auto-approve
    name="$(terraform -chdir={{ eks_dir }} output -raw cluster_name)"
    region="$(terraform -chdir={{ eks_dir }} output -raw region)"
    aws eks update-kubeconfig --name "${name}" --region "${region}"
    kubectl get nodes

# Show the cluster changes `just cluster-up` would apply, without provisioning anything.
cluster-plan: _bootstrap-init
    #!/usr/bin/env bash
    set -euo pipefail
    # Bare assignment so a failed `terraform output` aborts instead of passing an
    # empty bucket into the eks backend (see `cluster-up`).
    bucket="$(terraform -chdir={{ bootstrap_dir }} output -raw state_bucket_name)"
    terraform -chdir={{ eks_dir }} init -input=false -backend-config="bucket=${bucket}"
    terraform -chdir={{ eks_dir }} plan

# Ensure the vLLM api-key Secret exists. Generated once and left stable across
# deploys: vLLM enforces it on its API routes, and the bench endpoint stack reads the
# same value into Secrets Manager for the bench client. Regenerating it would
# lock out an api-key already published to a running bench endpoint.
_ensure-api-key:
    #!/usr/bin/env bash
    set -euo pipefail
    kubectl create namespace slipstream --dry-run=client -o yaml | kubectl apply -f -
    # `--ignore-not-found` prints nothing when the Secret is absent but still errors
    # on a real failure (unreachable API server, wrong context, RBAC), so a
    # connection problem can't masquerade as "absent" and mint a fresh key against
    # the wrong cluster — diverging from a key already published to a bench endpoint.
    if [[ -z "$(kubectl -n slipstream get secret vllm-api-key --ignore-not-found -o name)" ]]; then
      # Bare assignment so a failed openssl aborts: in argument position a failed
      # $(...) does not trip set -e, which would create a Secret with an empty key.
      key="$(openssl rand -hex 32)"
      [[ -n "${key}" ]] || { echo "openssl produced no api-key" >&2; exit 1; }
      kubectl -n slipstream create secret generic vllm-api-key --from-literal=api-key="${key}"
    fi

# Read the vLLM api-key out of the cluster Secret, failing loudly if the Secret
# exists but carries no api-key value rather than emitting an empty credential
# (an empty key would then authenticate nothing and be published to a bench endpoint).
_read-api-key:
    #!/usr/bin/env bash
    set -euo pipefail
    key="$(kubectl -n slipstream get secret vllm-api-key -o jsonpath='{.data.api-key}' | base64 -d)"
    [[ -n "${key}" ]] || { echo "vllm-api-key Secret has no api-key value" >&2; exit 1; }
    printf '%s' "${key}"

# Deploy the CPU vLLM replica and wait for it to serve.
cpu-deploy: _ensure-api-key
    kubectl apply -f {{ manifests }}
    kubectl -n slipstream rollout status deploy/vllm --timeout=600s

# Remove the vLLM workload (leaves the cluster running).
undeploy:
    kubectl delete -f {{ manifests }} --ignore-not-found

# Apply the GPU node pool (NodePool + EC2NodeClass) and the NVIDIA device plugin.
# Karpenter provisions no node until a pod tolerates the GPU taint and requests
# nvidia.com/gpu — the GPU replica (`just gpu-deploy`, #91) does that.
gpu-pool-up:
    #!/usr/bin/env bash
    set -euo pipefail
    command -v envsubst >/dev/null || { echo "envsubst required (brew install gettext)" >&2; exit 1; }
    # Bare assignments so a failed `terraform output` aborts the recipe (see `cluster-up`);
    # `terraform output -raw` exits 0 with empty stdout when an output resolves to
    # nothing, so the non-empty checks catch that separately — otherwise an empty
    # role and discovery tag would apply an EC2NodeClass that matches no subnet or
    # security group and never provisions. The eks stack is initialised by `just cluster-up`.
    cluster_name="$(terraform -chdir={{ eks_dir }} output -raw cluster_name)"
    [[ -n "${cluster_name}" ]] || { echo "eks output cluster_name is empty" >&2; exit 1; }
    node_role="$(terraform -chdir={{ eks_dir }} output -raw karpenter_node_iam_role_name)"
    [[ -n "${node_role}" ]] || { echo "eks output karpenter_node_iam_role_name is empty" >&2; exit 1; }
    # Substitute only the two known placeholders, so nothing else in the manifest
    # (e.g. a shell-like token in an image tag) is touched.
    CLUSTER_NAME="${cluster_name}" KARPENTER_NODE_ROLE="${node_role}" \
      envsubst '${CLUSTER_NAME} ${KARPENTER_NODE_ROLE}' <{{ gpu_pool_manifests }} \
      | kubectl apply -f -
    kubectl -n kube-system rollout status ds/nvidia-device-plugin-daemonset --timeout=120s

# Remove the GPU node pool and device plugin. Deleting the NodePool makes Karpenter
# drain and terminate any GPU instance it launched — instances that live outside
# Terraform state, so reaping them here keeps `just cluster-down` from racing the controller
# and orphaning a billing g5. Objects delete by name, so the manifest's unsubstituted
# placeholders are irrelevant and no terraform lookup is needed — teardown never
# blocks on eks state being reachable.
gpu-pool-down:
    kubectl delete -f {{ gpu_pool_manifests }} --ignore-not-found

# Deploy the GPU vLLM replica and wait for it to serve. Karpenter provisions the
# g5 on demand once the pod requests a GPU, so the wait covers node bring-up, the
# ~10 GB image pull, and the AWQ weight load — hence the long timeout. Reuses the
# vllm-api-key Secret (_ensure-api-key), the same key the CPU replica uses.
gpu-deploy: _ensure-api-key
    kubectl apply -f {{ gpu_manifests }}
    kubectl -n slipstream rollout status deploy/vllm-gpu --timeout=1200s

# Scale the GPU replica up to one and wait for it to serve. Karpenter brings a g5
# node up to place it.
gpu-up:
    kubectl -n slipstream scale deploy/vllm-gpu --replicas=1
    kubectl -n slipstream rollout status deploy/vllm-gpu --timeout=1200s

# Scale the GPU replica to zero. The g5 node goes empty and Karpenter reaps it
# after its consolidation window, returning GPU spend to zero (ADR-0006).
gpu-down:
    kubectl -n slipstream scale deploy/vllm-gpu --replicas=0

# Bring the whole benchmark stack up and leave it running: cluster, GPU node pool,
# GPU replica, and the ephemeral mTLS bench endpoint. Everything `just bench`
# and `just prefix-cache` need — run them against it as often as you like, then
# `just stack-down` at the end of the day to return spend to zero.
stack-up: cluster-up gpu-pool-up gpu-deploy bench-endpoint-up

# Tear the whole benchmark stack down, returning GPU and cluster spend to zero,
# then sweep AWS to confirm nothing tagged Project=slipstream is still billing.
# Bootstrap state and the ECR repo survive (see `just cluster-down`). Order matters:
# drop the bench endpoint and scale the GPU replica to zero, then delete the
# NodePool so Karpenter reaps the g5 before `just cluster-down` destroys the VPC it
# lives in. `cluster-down` is gated on `bench-endpoint-down` succeeding: the endpoint's
# load balancer and host sit in the cluster VPC, so destroying the VPC under a live
# endpoint wedges the destroy on a DependencyViolation and orphans a billing load
# balancer. The GPU scale/pool steps are in-cluster and orthogonal, so they run
# regardless to stop the g5. The leak sweep always runs to report whatever survived.
stack-down:
    #!/usr/bin/env bash
    set -euo pipefail
    overall=0
    # Capture the region while the eks stack still has outputs; `cluster-down`
    # destroys them, and the sweep needs a region to target afterwards.
    region="$(terraform -chdir={{ eks_dir }} output -raw region 2>/dev/null || true)"
    # Tear the endpoint down first and gate the VPC destroy on it: a failed
    # endpoint teardown leaves the load balancer and host in the VPC, so running
    # cluster-down anyway would wedge on a DependencyViolation and orphan billing.
    if ! just bench-endpoint-down; then
      echo "bench-endpoint-down failed; skipping cluster-down so the VPC destroy does not wedge under a live endpoint. Resolve the endpoint teardown, then rerun stack-down." >&2
      overall=1
    fi
    just gpu-down || true
    just gpu-pool-down || true
    # cluster-down only when the endpoint is gone. A failed cluster-down is escalated —
    # it means resources may remain billing for the sweep to catch.
    if [[ "${overall}" -eq 0 ]] && ! just cluster-down; then overall=1; fi
    if ! just _zero-leak-sweep "${region}"; then overall=1; fi
    exit "${overall}"

# Port-forward the service and curl a completion out of it.
cpu-completion:
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
    # vLLM enforces the api-key on its API routes; read it from the Secret.
    key="$(just _read-api-key)"
    curl -sf http://localhost:8000/v1/completions \
      -H 'Content-Type: application/json' \
      -H "Authorization: Bearer ${key}" \
      -d '{"model":"{{ cpu_model }}","prompt":"The slipstream platform serves","max_tokens":32}'
    echo

# Smoke the GPU replica: assert HTTP 200 and a well-formed, non-empty completion
# from the live engine (ADR-0002 item 5, for the GPU path). Proves the deploy →
# service → engine → token path end-to-end; it does not judge answer quality
# (out of scope, spec §5). This is the "harness green" step of `just cloud-verify`.
gpu-completion:
    #!/usr/bin/env bash
    set -euo pipefail
    kubectl -n slipstream rollout status deploy/vllm-gpu --timeout=1200s
    kubectl -n slipstream port-forward svc/vllm-gpu 8000:8000 >/dev/null 2>&1 &
    pf_pid=$!
    trap 'kill "${pf_pid}" 2>/dev/null || true' EXIT
    ready=""
    for _ in $(seq 60); do
      curl -sf http://localhost:8000/health >/dev/null 2>&1 && { ready=1; break; }
      sleep 1
    done
    [[ -n "${ready}" ]] || { echo "vllm-gpu /health never came up" >&2; exit 1; }
    # vLLM enforces the api-key on its API routes; read it from the Secret.
    key="$(just _read-api-key)"
    body="$(mktemp)"
    trap 'kill "${pf_pid}" 2>/dev/null || true; rm -f "${body}"' EXIT
    code="$(curl -s -o "${body}" -w '%{http_code}' http://localhost:8000/v1/completions \
      -H 'Content-Type: application/json' \
      -H "Authorization: Bearer ${key}" \
      -d '{"model":"{{ gpu_model }}","prompt":"The slipstream platform serves","max_tokens":32}')"
    [[ "${code}" == "200" ]] || { echo "completion returned HTTP ${code}:" >&2; cat "${body}" >&2; exit 1; }
    # Assert the body carries a non-empty completion — shape and liveness, not
    # answer quality. A 200 with an empty choices/text still means the engine
    # served nothing, so it must fail here.
    uv run python -c 'import json,sys; d=json.load(open(sys.argv[1])); c=d.get("choices") or []; t=(c[0].get("text","") if c else ""); sys.exit(0 if t.strip() else "completion had no non-empty text: "+json.dumps(d))' "${body}"
    echo "gpu completion ok"

# Print the bench-client image reference (ECR repo URL from `just bootstrap`, plus the tag).
_bench-image-ref: _bootstrap-init
    #!/usr/bin/env bash
    set -euo pipefail
    # Bare assignment (not `local`/`export`) so `set -e` aborts on a terraform
    # failure — a missing bootstrap state or AWS auth error surfaces here instead
    # of collapsing to a bogus `:latest` ref that the caller would apply blindly.
    repo="$(terraform -chdir={{ bootstrap_dir }} output -raw bench_image_repo_url)"
    echo "${repo}:{{ bench_image_tag }}"

# Build the bench-client image and push it to its ECR repo (provisioned by `just bootstrap`).
bench-image: _bootstrap-init
    #!/usr/bin/env bash
    set -euo pipefail
    repo="$(terraform -chdir={{ bootstrap_dir }} output -raw bench_image_repo_url)"
    region="$(terraform -chdir={{ bootstrap_dir }} output -raw region)"
    # The registry host is the repo URL without its trailing repository path.
    registry="${repo%%/*}"
    aws ecr get-login-password --region "${region}" \
      | docker login --username AWS --password-stdin "${registry}"
    # Nodes are amd64; build for that arch regardless of the developer's host.
    # Drive the baked tokenizer from the one `bench_model` var the sweep also uses,
    # so the two never diverge.
    docker build --platform linux/amd64 --build-arg MODEL={{ bench_model }} \
      -t "${repo}:{{ bench_image_tag }}" -f {{ bench_dockerfile }} .
    docker push "${repo}:{{ bench_image_tag }}"

# Sweep `vllm bench serve` (prefix-share % x burstiness) from the external bench host through the mutual-TLS ALB over SSM, saving per-cell JSON to bench/results. Requires a live bench endpoint (`just bench-endpoint-up`).
bench *args:
    #!/usr/bin/env bash
    set -euo pipefail
    # The host drives the sweep over SSM, which starts the loopback mTLS proxy then
    # runs the sweep against it, so the latency recorded is what an off-cluster client
    # sees rather than an in-cluster hop. Results outlive the host in the results
    # bucket and are synced back here at the end.
    #
    # Bare assignments so a failed output lookup aborts rather than driving SSM at an
    # empty instance id or copying from an empty bucket (see `cluster-up`).
    region="$(terraform -chdir={{ eks_dir }} output -raw region)"
    instance="$(terraform -chdir={{ bench_endpoint_dir }} output -raw bench_host_instance_id)"
    bucket="$(terraform -chdir={{ bench_endpoint_dir }} output -raw results_bucket_name)"
    image="$(just _bench-image-ref)"
    # A UTC timestamp is the run's prefix in the bucket and the local results subdir,
    # so concurrent or repeated runs never overwrite each other.
    run_id="$(date -u +%Y%m%dT%H%M%SZ)"

    # run_on_host sends one command to the host over SSM, polls to completion and
    # surfaces its stderr; it reads the region and instance set above.
    source bench/run-on-host.sh

    # The proxy must be listening before the sweep runs; a failed handshake here is a
    # hard stop, not a partial result to salvage. The ceiling sits above the up-script's
    # own ~210s smoke-retry budget so its clear failure message wins over a generic wait.
    run_on_host "proxy-up" "/usr/local/bin/bench-proxy-up.sh" 240

    # Pass the per-run values as environment prefixed to the host command. The plain
    # values carry no shell-special characters, so single-quoting suffices; the free-form
    # extra flags are base64-encoded so they cross the SSM command line without quoting
    # the host shell (not necessarily bash) could misparse. The api-key is fetched
    # host-side from Secrets Manager, never sent from here. serve-sweep only exits
    # non-zero at the end of the grid, so a partial failure still leaves cells worth
    # keeping: sync whatever landed regardless, then surface the sweep's status.
    args_b64="$(printf '%s' '{{ args }}' | base64 | tr -d '\n')"
    sweep_env="IMAGE_REF='${image}' RESULTS_BUCKET='${bucket}' MODEL='{{ bench_model }}'"
    sweep_env="${sweep_env} RUN_ID='${run_id}' SWEEP_ARGS_B64='${args_b64}'"
    sweep_rc=0
    run_on_host "sweep" "${sweep_env} /usr/local/bin/bench-sweep.sh" 3600 || sweep_rc=$?

    # Sync just this run's objects into a per-run subdir so repeated runs never clobber
    # each other locally, matching the bucket's per-run prefix and bench-results-sync.
    mkdir -p "bench/results/${run_id}"
    aws s3 sync "s3://${bucket}/sweeps/${run_id}" "bench/results/${run_id}" --region "${region}"
    echo "results synced to bench/results/${run_id}/"
    exit "${sweep_rc}"

# Sync every sweep and prefix-cache run's results from the bench endpoint results bucket to bench/results for local reporting; use to pull runs made from another machine.
bench-results-sync:
    #!/usr/bin/env bash
    set -euo pipefail
    region="$(terraform -chdir={{ eks_dir }} output -raw region)"
    bucket="$(terraform -chdir={{ bench_endpoint_dir }} output -raw results_bucket_name)"
    mkdir -p bench/results/prefix-cache
    aws s3 sync "s3://${bucket}/sweeps" bench/results --region "${region}"
    aws s3 sync "s3://${bucket}/prefix-cache" bench/results/prefix-cache --region "${region}"
    echo "synced sweep and prefix-cache results to bench/results/"

# Run the slipstream-bench Python test suite (no cluster).
cli-test:
    uv run pytest

# Measure the prefix-cache hit rate for a cold and a warm run of one bench cell from the external bench host over SSM, and join each to its client JSON locally (in bench/results/prefix-cache/<run-id>). Requires a live bench endpoint (`just bench-endpoint-up`).
prefix-cache prefix_share="90" burstiness="1.0" *args="":
    #!/usr/bin/env bash
    set -euo pipefail
    # The host runs the cold and warm cells over SSM against the loopback mTLS proxy,
    # bracketing each with a /metrics snapshot, so the hit rate is measured on the same
    # off-cluster path `just bench` uses. The snapshots and cell JSON outlive the host
    # in the results bucket; the join runs here against the synced files.
    #
    # Bare assignments so a failed output lookup aborts rather than driving SSM at an
    # empty instance id or copying from an empty bucket (see `cluster-up`).
    region="$(terraform -chdir={{ eks_dir }} output -raw region)"
    instance="$(terraform -chdir={{ bench_endpoint_dir }} output -raw bench_host_instance_id)"
    bucket="$(terraform -chdir={{ bench_endpoint_dir }} output -raw results_bucket_name)"
    image="$(just _bench-image-ref)"
    # A UTC timestamp is the run's prefix in the bucket and the local results subdir,
    # so concurrent or repeated runs never overwrite each other.
    run_id="$(date -u +%Y%m%dT%H%M%SZ)"

    # run_on_host sends one command to the host over SSM, polls to completion and
    # surfaces its stderr; it reads the region and instance set above.
    source bench/run-on-host.sh

    # The proxy must be listening before the cells run; a failed handshake is a hard
    # stop, not a partial result. The ceiling sits above the up-script's own retry
    # budget so its clear failure message wins over a generic wait.
    run_on_host "proxy-up" "/usr/local/bin/bench-proxy-up.sh" 240

    # Pass the per-run values as environment prefixed to the host command. The plain
    # values carry no shell-special characters, so single-quoting suffices; the free-form
    # extra flags are base64-encoded so they cross the SSM command line without quoting
    # the host shell (not necessarily bash) could misparse. The api-key is fetched
    # host-side from Secrets Manager, never sent from here. A cold/warm comparison needs
    # both cells, so the host script hard-fails on a cell failure rather than leaving a
    # half result — a non-zero here means nothing worth joining was produced.
    args_b64="$(printf '%s' '{{ args }}' | base64 | tr -d '\n')"
    prefix_env="IMAGE_REF='${image}' RESULTS_BUCKET='${bucket}' MODEL='{{ bench_model }}'"
    prefix_env="${prefix_env} RUN_ID='${run_id}' PREFIX_SHARE='{{ prefix_share }}'"
    prefix_env="${prefix_env} BURSTINESS='{{ burstiness }}' PREFIX_ARGS_B64='${args_b64}'"
    run_on_host "prefix-cache" "${prefix_env} /usr/local/bin/bench-prefix-cache.sh" 3600

    # Sync just this run's objects into a per-run subdir, then join locally. The join is
    # a pure function of the synced snapshots and cell JSON (slipstream_bench.prefix_cache).
    out="bench/results/prefix-cache/${run_id}"
    mkdir -p "${out}"
    aws s3 sync "s3://${bucket}/prefix-cache/${run_id}" "${out}" --region "${region}"

    cell="pshare{{ prefix_share }}_burst{{ burstiness }}.json"
    uv run slipstream-bench prefix-cache --cache-state cold \
      --metrics-before "${out}/cold_before.prom" --metrics-after "${out}/cold_after.prom" \
      --result "${out}/cold_${cell}" | tee "${out}/cold_hit_rate.json"
    uv run slipstream-bench prefix-cache --cache-state warm \
      --metrics-before "${out}/warm_before.prom" --metrics-after "${out}/warm_after.prom" \
      --result "${out}/warm_${cell}" | tee "${out}/warm_hit_rate.json"
    echo "prefix-cache results in ${out}/"

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
      -d '{"resourceSpans":[{"resource":{"attributes":[{"key":"service.name","value":{"stringValue":"vllm"}}]},"scopeSpans":[{"spans":[{"traceId":"5b8efff798038103d269b633813fc60c","spanId":"eee19b7ec3c1b174","name":"chat.completion","kind":2,"startTimeUnixNano":"1700000000000000000","endTimeUnixNano":"1700000000100000000","attributes":[{"key":"request_id","value":{"stringValue":"req-demo-001"}},{"key":"model_id","value":{"stringValue":"{{ gpu_model }}"}},{"key":"prompt_tokens","value":{"intValue":"42"}},{"key":"completion_tokens","value":{"intValue":"128"}},{"key":"prefix_hash","value":{"stringValue":"9f86d081"}}]}]}]}]}'
    sleep 3
    kubectl -n slipstream logs deploy/otel-collector | grep -A12 'request_id'

# Destroy the cluster (the bootstrap state bucket is left intact).
cluster-down:
    terraform -chdir={{ eks_dir }} destroy -auto-approve

# Sweep AWS for a Project=slipstream instance or volume still billing after a
# teardown and exit non-zero if any survives (test/zero-leak-sweep.sh, classified
# by `slipstream-bench zero-leak`). The caller passes the region it captured
# before `cluster-down`, since the eks stack has no outputs left to read it from
# afterwards; an empty region is a hard stop, since the sweep cannot be targeted.
_zero-leak-sweep region:
    #!/usr/bin/env bash
    set -euo pipefail
    # The caller passes the region it captured before cluster-down. When teardown
    # already destroyed the eks outputs (an interrupted cluster-up or a partial
    # cluster-down), that capture is empty exactly when the sweep matters most, so
    # fall back to the ambient AWS region — AWS_REGION, then the profile default —
    # rather than skip the money-safety check.
    region="{{ region }}"
    if [[ -z "${region}" ]]; then
      region="${AWS_REGION:-$(aws configure get region 2>/dev/null || true)}"
    fi
    if [[ -z "${region}" ]]; then
      echo "region unavailable (no argument, AWS_REGION, or profile default); cannot confirm zero leftovers" >&2
      exit 1
    fi
    {{ justfile_directory() }}/test/zero-leak-sweep.sh "${region}"

# End-to-end cloud verification of the GPU path (ADR-0002 item 4, #92). Real GPU
# spend, hand-triggered. Brings the cluster and GPU replica up, asserts the engine
# serves a well-formed completion, then tears everything down and sweeps AWS for a
# Project=slipstream instance or volume still billing — asserting zero. Teardown
# and the sweep run even when the smoke fails, so a failed check never orbits a
# live g5. Exits non-zero if the smoke failed, teardown failed, or a leftover
# survived. Every fallible step is guarded so `set -e` cannot skip the teardown.
cloud-verify:
    #!/usr/bin/env bash
    set -euo pipefail
    overall=0
    region=""

    # Pre-flight: cloud-verify is a one-shot create -> verify -> destroy money check
    # and must start from a clean slate. Against a pre-existing cluster, cluster-up's
    # create becomes a multi-minute node-group roll, and the teardown below would then
    # destroy a cluster the operator did not stand up here. Abort if the eks stack
    # already holds any resource, pointing at `stack-down` to clear it first.
    bucket="$(terraform -chdir={{ bootstrap_dir }} output -raw state_bucket_name)"
    terraform -chdir={{ eks_dir }} init -input=false -backend-config="bucket=${bucket}"
    if terraform -chdir={{ eks_dir }} state list 2>/dev/null | grep -q .; then
      echo "eks stack is not empty; cloud-verify expects a clean slate. Run 'just stack-down' first, then rerun." >&2
      exit 1
    fi

    # Bring the path up. On any failure, stop climbing but still fall through to the
    # teardown + sweep below — a partial apply may already be billing.
    if ! just cluster-up; then overall=1; fi
    # Capture the region while the eks stack still has outputs; after `just cluster-down`
    # they are gone. When cluster-up succeeded the region output must be readable — a
    # missing one there is a hard stop, since falling back to the ambient region could
    # sweep a different one and false-green while a g5 bills. In the partial-apply case
    # the fallback is safe and lives in `_zero-leak-sweep`, which resolves an empty
    # region from AWS_REGION / the profile default.
    region="$(terraform -chdir={{ eks_dir }} output -raw region 2>/dev/null || true)"
    if [[ "${overall}" -eq 0 && -z "${region}" ]]; then
      echo "eks region output unavailable after cluster-up; cannot target the sweep" >&2
      overall=1
    fi
    if [[ "${overall}" -eq 0 ]] && ! just gpu-pool-up; then overall=1; fi
    if [[ "${overall}" -eq 0 ]] && ! just gpu-deploy; then overall=1; fi
    if [[ "${overall}" -eq 0 ]] && ! just gpu-completion; then
      echo "gpu smoke failed" >&2
      overall=1
    fi

    # Teardown always runs, in reaping order. Scale the replica to zero and delete
    # the NodePool (Karpenter reaps the g5) before destroying the VPC it sits in;
    # tolerate the scale/pool steps failing so `cluster-down` still runs. A failed `cluster-down`
    # is escalated — it means resources may remain for the sweep to catch.
    just gpu-down || true
    just gpu-pool-down || true
    if ! just cluster-down; then overall=1; fi

    # Money-safety backstop: assert no tagged resource outlived the teardown.
    if ! just _zero-leak-sweep "${region}"; then overall=1; fi

    exit "${overall}"

# Stand up the ephemeral public bench endpoint: a mutual-TLS load balancer
# fronting vLLM, so both arms can be measured from one host outside the cluster.
# It exists only for the run; `bench-endpoint-down` tears it down. Requires the cluster
# up and vLLM deployed (`just cluster-up && just cpu-deploy`).
bench-endpoint-up: _bootstrap-init _ensure-api-key
    #!/usr/bin/env bash
    set -euo pipefail
    # Bare assignments so a failed lookup aborts rather than feeding empty values
    # into terraform (see `cluster-up`).
    bucket="$(terraform -chdir={{ bootstrap_dir }} output -raw state_bucket_name)"
    # Read the cluster's network topology from the eks outputs and pass it in as
    # variables (bare assignments abort on a missing output, see `cluster-up`).
    # The bench endpoint holds these in its own state, so `bench-endpoint-down` destroys
    # from state alone and never needs the eks stack — an interrupted cluster-up
    # can no longer strand this endpoint billing.
    vpc_id="$(terraform -chdir={{ eks_dir }} output -raw vpc_id)"
    node_sg="$(terraform -chdir={{ eks_dir }} output -raw node_security_group_id)"
    subnets="$(terraform -chdir={{ eks_dir }} output -json public_subnets)"
    asgs="$(terraform -chdir={{ eks_dir }} output -json node_autoscaling_groups)"
    # Pass the api-key through the environment, not `-var`, so the secret never
    # lands in the terraform process argv (visible via `ps`) or shell history.
    TF_VAR_vllm_api_key="$(just _read-api-key)"
    export TF_VAR_vllm_api_key
    # Restrict the load balancer to this host as a cheap extra layer; mutual TLS
    # is the real gate.
    cidr="$(curl -sf https://checkip.amazonaws.com)" \
      || { echo "could not determine operator IP from checkip.amazonaws.com" >&2; exit 1; }
    cidr="${cidr}/32"
    [[ "${cidr}" =~ ^[0-9.]+/32$ ]] || { echo "unexpected operator IP from checkip: ${cidr}" >&2; exit 1; }
    terraform -chdir={{ bench_endpoint_dir }} init -input=false -backend-config="bucket=${bucket}"
    terraform -chdir={{ bench_endpoint_dir }} apply -auto-approve \
      -var="state_bucket=${bucket}" \
      -var="operator_cidr=${cidr}" \
      -var="vpc_id=${vpc_id}" \
      -var="node_security_group_id=${node_sg}" \
      -var="public_subnets=${subnets}" \
      -var="node_autoscaling_groups=${asgs}"
    terraform -chdir={{ bench_endpoint_dir }} output
    # cloud-init runs asynchronously after `apply` returns — installing Docker,
    # pulling the bench image from ECR, writing the proxy scripts — so the host is
    # not ready the instant terraform finishes. Block on the boot sentinel the host
    # writes to the results bucket (boot-status/<instance-id>: "ok" on success, or
    # "failed: <step>") so a following `just bench`/`prefix-cache` cannot race a host
    # whose /usr/local/bin scripts are not written yet (exit 127 on proxy-up).
    region="$(terraform -chdir={{ bootstrap_dir }} output -raw region)"
    results_bucket="$(terraform -chdir={{ bench_endpoint_dir }} output -raw results_bucket_name)"
    instance="$(terraform -chdir={{ bench_endpoint_dir }} output -raw bench_host_instance_id)"
    key="s3://${results_bucket}/boot-status/${instance}"
    echo "waiting for bench host ${instance} to finish boot..." >&2
    deadline=$((SECONDS + 420))
    while :; do
      # Missing key (boot not far enough to report) is a not-ready, not an error:
      # swallow the copy failure and keep polling until "ok", a "failed:" report, or
      # the ceiling. A "failed:" sentinel is a hard stop with the host's failing step.
      status="$(aws s3 cp "${key}" - --region "${region}" 2>/dev/null || true)"
      case "${status}" in
      ok)
        echo "bench host boot: ok" >&2
        break
        ;;
      failed:*)
        echo "bench host boot ${status}" >&2
        exit 1
        ;;
      esac
      ((SECONDS < deadline)) || {
        echo "bench host ${instance} did not report ready within 420s (last: '${status:-<no sentinel yet>}')" >&2
        exit 1
      }
      sleep 5
    done

# Destroy the ephemeral bench endpoint (load balancer, trust store, certs,
# client secret). The cluster and its vLLM workload stay up.
bench-endpoint-down: _bootstrap-init
    #!/usr/bin/env bash
    set -euo pipefail
    bucket="$(terraform -chdir={{ bootstrap_dir }} output -raw state_bucket_name)"
    terraform -chdir={{ bench_endpoint_dir }} init -input=false -backend-config="bucket=${bucket}"
    # Destroy needs no live cluster or IP lookup: the api-key and operator CIDR only
    # shape resources being torn down. Placeholders keep teardown of a public
    # endpoint from being blocked by an already-deleted Secret or an offline network,
    # which would otherwise orphan an internet-facing load balancer.
    TF_VAR_vllm_api_key="unused" \
      terraform -chdir={{ bench_endpoint_dir }} destroy -auto-approve \
      -var="state_bucket=${bucket}" \
      -var="operator_cidr=0.0.0.0/32"

# Apply the state-bootstrap stack once, before the first `just cluster-up`. Assumes the
# state bucket already exists (state lives in it, see backend.tf). A brand-new
# environment bootstraps the bucket first with the two-step in ADR-0005.
bootstrap:
    terraform -chdir={{ bootstrap_dir }} init -input=false
    terraform -chdir={{ bootstrap_dir }} apply -auto-approve
