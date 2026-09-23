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
# Derives the bench image's served model, tokenizer slug, and content tags from
# model.yaml + git; the build, the pull ref, and the completion smokes all read
# the served model through it, so the one definition drives them all.
image_tag_tool := "bench/image-tag.sh"
# Builds the image from model.yaml, tagging it with each ref passed; shared with
# the CI workflows so the one build command never diverges.
build_image_tool := "bench/build-image.sh"
# Empty pulls the floating `<slug>-main` tag that CI publishes on every merge to
# main; set a `<slug>-<sha>` tag to pin a reproducible run.
bench_image_tag := ""
cpu_model := "Qwen/Qwen2.5-0.5B-Instruct"
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

# Render the GPU manifest's swept engine knobs from the environment, defaulting to
# the committed rig config (max-num-seqs 16, FP8 KV, prefix caching on) so a plain
# `just gpu-deploy` deploys the committed rig config. `just knob-sweep` sets
# MAX_NUM_SEQS / KV_CACHE_DTYPE / PREFIX_CACHING_FLAG per sweep point (#33, ADR-0009).
# Only these three placeholders are substituted, so nothing else in the manifest
# (an image tag, a shell-like token) is touched. Prints to stdout — pipe to
# `kubectl apply -f -` (or `kubectl diff -f -` to preview before applying).
_render-gpu-manifest:
    #!/usr/bin/env bash
    set -euo pipefail
    command -v envsubst >/dev/null || { echo "envsubst required (brew install gettext)" >&2; exit 1; }
    MAX_NUM_SEQS="${MAX_NUM_SEQS:-16}" \
    KV_CACHE_DTYPE="${KV_CACHE_DTYPE:-fp8}" \
    PREFIX_CACHING_FLAG="${PREFIX_CACHING_FLAG:---enable-prefix-caching}" \
      envsubst '${MAX_NUM_SEQS} ${KV_CACHE_DTYPE} ${PREFIX_CACHING_FLAG}' <{{ gpu_manifests }}

# Deploy the GPU vLLM replica and wait for it to serve. Karpenter provisions the
# g5 on demand once the pod requests a GPU, so the wait covers node bring-up, the
# ~10 GB image pull, and the AWQ weight load — hence the long timeout. Reuses the
# vllm-api-key Secret (_ensure-api-key), the same key the CPU replica uses. The
# manifest is rendered (see `_render-gpu-manifest`) so the swept engine knobs
# default to the committed rig unless the knob sweep overrides them.
gpu-deploy: _ensure-api-key
    #!/usr/bin/env bash
    set -euo pipefail
    just _render-gpu-manifest | kubectl apply -f -
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
    # The request's model field must name what the GPU replica serves; read it from
    # model.yaml, the source of truth the manifest is held to.
    model="$({{ image_tag_tool }} hf-id)"
    body="$(mktemp)"
    trap 'kill "${pf_pid}" 2>/dev/null || true; rm -f "${body}"' EXIT
    code="$(curl -s -o "${body}" -w '%{http_code}' http://localhost:8000/v1/completions \
      -H 'Content-Type: application/json' \
      -H "Authorization: Bearer ${key}" \
      -d "{\"model\":\"${model}\",\"prompt\":\"The slipstream platform serves\",\"max_tokens\":32}")"
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
    # of collapsing to a bogus `repo:tag` ref that the caller would apply blindly.
    repo="$(terraform -chdir={{ bootstrap_dir }} output -raw bench_image_repo_url)"
    # An empty bench_image_tag follows the floating main build; a set value pins it.
    tag='{{ bench_image_tag }}'
    [[ -n "${tag}" ]] || tag="$({{ image_tag_tool }} main-tag)"
    echo "${repo}:${tag}"

# Build the bench-client image from model.yaml and push it under its immutable
# content tag. The tokenizer baked in is the served model at its pinned revision;
# the image is published under the immutable `<slug>-<sha>` tag only. The floating
# `<slug>-main` pointer belongs to the trusted branch and is published solely by CI
# on a merge to main (#103), never from a branch build here — a laptop push would
# clobber the shared pointer with unmerged code. Pin a run to a branch build with
# `-var bench_image_tag=<slug>-<sha>`. The ECR repo is provisioned by `just bootstrap`.
bench-image: _bootstrap-init
    #!/usr/bin/env bash
    set -euo pipefail
    repo="$(terraform -chdir={{ bootstrap_dir }} output -raw bench_image_repo_url)"
    region="$(terraform -chdir={{ bootstrap_dir }} output -raw region)"
    # The content tag derives from model.yaml and git, so the baked tokenizer and
    # the image name that advertises it never diverge.
    sha_tag="$({{ image_tag_tool }} sha-tag)"
    # The registry host is the repo URL without its trailing repository path.
    registry="${repo%%/*}"
    aws ecr get-login-password --region "${region}" \
      | docker login --username AWS --password-stdin "${registry}"
    {{ build_image_tool }} "${repo}:${sha_tag}"
    docker push "${repo}:${sha_tag}"

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
    # so concurrent or repeated runs never overwrite each other. The knob sweep
    # (#33) injects BENCH_RUN_ID to nest one Tier-2 ladder per engine-knob point
    # under a shared run (e.g. <run>/mns16_kvfp8_pcon); RUN_ID is only ever a path
    # segment host-side (bench-sweep.sh), so a slash gives the nested subdir for free.
    run_id="${BENCH_RUN_ID:-$(date -u +%Y%m%dT%H%M%SZ)}"

    # run_on_host sends one command to the host over SSM, polls to completion and
    # surfaces its stderr; it reads the region and instance set above.
    source bench/run-on-host.sh

    # The proxy must be listening before the sweep runs; a failed handshake here is a
    # hard stop, not a partial result to salvage. The ceiling sits above the up-script's
    # own ~210s smoke-retry budget so its clear failure message wins over a generic wait.
    run_on_host "proxy-up" "/usr/local/bin/bench-proxy-up.sh" 240

    # The experiment definition crosses to the host as a file: knob-sweep composes a
    # per-point config and points BENCH_CONFIG at it, a standalone run uses the checked-in
    # default. Its bytes are base64-encoded so the YAML (newlines, quotes) crosses the SSM
    # command line intact, decoded host-side into a file bench-sweep.sh mounts and passes
    # as --config. The api-key is fetched host-side from Secrets Manager, never sent from
    # here. load-sweep only exits non-zero at the end of the grid, so a partial failure
    # still leaves cells worth keeping: sync whatever landed regardless, then surface the
    # sweep's status.
    config="${BENCH_CONFIG:-bench/load-sweep.yaml}"
    config_b64="$(base64 <"${config}" | tr -d '\n')"
    # Any extra load-sweep flags (--dry-run, --api-key-env) appended to `just bench`, also
    # base64-encoded so they cross SSM without quoting the host shell could misparse.
    args_b64="$(printf '%s' '{{ args }}' | base64 | tr -d '\n')"
    # The sweep counts tokens under the served model's name; read it from the same
    # model.yaml the pulled image baked its tokenizer from, so the two agree.
    model="$({{ image_tag_tool }} hf-id)"
    sweep_env="IMAGE_REF='${image}' RESULTS_BUCKET='${bucket}' MODEL='${model}'"
    sweep_env="${sweep_env} RUN_ID='${run_id}' SWEEP_CONFIG_B64='${config_b64}'"
    sweep_env="${sweep_env} SWEEP_ARGS_B64='${args_b64}'"
    sweep_rc=0
    run_on_host "sweep" "${sweep_env} /usr/local/bin/bench-sweep.sh" 3600 || sweep_rc=$?

    # Sync just this run's objects into a per-run subdir so repeated runs never clobber
    # each other locally, matching the bucket's per-run prefix and bench-results-sync.
    mkdir -p "bench/results/${run_id}"
    aws s3 sync "s3://${bucket}/sweeps/${run_id}" "bench/results/${run_id}" --region "${region}"
    echo "results synced to bench/results/${run_id}/"
    exit "${sweep_rc}"

# Two-tier engine-knob sweep (#33, ADR-0009).
#
# Every swept value lives in bench/sweep-grid.yaml, read here through
# `slipstream-bench sweep-grid`, which validates the whole grid before the first
# deploy — the recipe holds no knob values or knob logic of its own.
#
# Tier 1 = engine knobs, one GPU redeploy per point. The knobs live in the vLLM
# launch args, so every combination needs a fresh `just gpu-deploy`:
#   max-num-seqs (mns) {16,32,64,128,256} x KV dtype {fp8,fp16} x prefix caching
#   {on,off} = 20 points, one grid row each.
# Tier 2 = client load ladder, no redeploy. Against each already-running Tier-1
# point, `just bench` walks the concurrency ladder {8..256} to find the highest rung
# that holds goodput at the SLO — the sustained concurrency ceiling for that point.
# The prefix-share axis rides along, but only where it can matter: {10,50,90} when
# prefix caching is on, a single 0 baseline when off. With caching off vLLM reuses
# no prefix KV whatever the share, so a swept share there measures a definitional
# null (ADR-0009).
#
# For each Tier-1 point: render + redeploy the GPU replica, scrape vLLM's predicted
# concurrency ceiling from the startup log, then drive the Tier-2 ladder against it.
# Each point's ladder JSON lands under bench/results/<run>/mns{N}_kv{fp8|fp16}_pc{on|off}/,
# and the predicted ceilings are tabulated alongside. FP8 is the committed rig; fp16
# is swept only as a counterfactual baseline. Burstiness is pinned to one value: the
# Tier-2 ladder is closed-loop (the concurrency cap limits in-flight requests and releases
# a new one as each completes), not open-loop (the request rate drives arrivals on a
# clock, and burstiness shapes their inter-arrival gaps). Under closed-loop there is no
# arrival process for burstiness to shape, so it is inert here. A point that fails to
# deploy or whose ladder reports failures is recorded and the sweep continues, exiting
# non-zero at the end. Requires a live stack (`just stack-up`).
knob-sweep:
    #!/usr/bin/env bash
    set -euo pipefail
    # Read every swept value from the validated grid before touching a GPU. Each
    # value is captured into a variable so the CLI's exit code is checked and an
    # invalid grid aborts the sweep here, not mid-run — never `< <(...)`, which
    # would run the CLI in a subshell and hide its failure under `set -e`.
    engine_points="$(uv run slipstream-bench sweep-grid engine-points)"
    concurrency_ladder="$(uv run slipstream-bench sweep-grid concurrency-ladder)"
    burstiness="$(uv run slipstream-bench sweep-grid burstiness)"
    # The ladder rungs and the pinned burstiness are the same for every point, so fold
    # them to YAML flow lists once, up front, to override into each point's config below.
    ladder_yaml="[$(paste -sd, - <<<"${concurrency_ladder}")]"
    burst_yaml="[${burstiness}]"
    run_id="$(date -u +%Y%m%dT%H%M%SZ)"
    run_dir="bench/results/${run_id}"
    mkdir -p "${run_dir}"
    ledger="${run_dir}/predicted-ceilings.tsv"
    printf 'point\tpredicted_ceiling\n' >"${ledger}"
    overall=0
    # One grid row per Tier-1 point: slug, max-num-seqs, KV engine token, prefix-
    # caching flag, and the CSV of prefix shares to sweep under it. The grid already
    # mapped the KV label to the engine token (fp16 -> float16) and picked the shares
    # per caching arm, so the recipe just deploys the point and drives the ladder.
    while IFS=$'\t' read -r point mns kv_dtype pc_flag shares_csv; do
      echo "==> knob-sweep point ${point}" >&2
      # Render + redeploy the GPU replica for this engine-knob point. A failed
      # deploy leaves the point unmeasurable; record it and move on rather than
      # abandon the remaining points.
      if ! MAX_NUM_SEQS="${mns}" KV_CACHE_DTYPE="${kv_dtype}" PREFIX_CACHING_FLAG="${pc_flag}" just gpu-deploy; then
        echo "!! ${point}: gpu-deploy failed; skipping point" >&2
        printf '%s\t%s\n' "${point}" "<deploy-failed>" >>"${ledger}"
        overall=1
        continue
      fi
      # Scrape vLLM's predicted concurrency ceiling from the startup log. It is a
      # VRAM/KV-budget upper bound, not a measured ceiling (ADR-0009): a cross-check
      # against the Tier-2 goodput result, never reported as the ceiling. The log
      # read and the ceiling grep are split so a lost log (kubectl failed) records a
      # different sentinel than a log that simply carried no ceiling line.
      # replicas=1, so `logs deploy/vllm-gpu` reads the one pod just rolled out.
      if ! logs="$(kubectl -n slipstream logs deploy/vllm-gpu 2>/dev/null)"; then
        echo "!! ${point}: could not read vLLM startup log for predicted ceiling" >&2
        predicted="<logs-unavailable>"
      else
        predicted="$(printf '%s\n' "${logs}" \
          | grep -oE 'Maximum concurrency for [0-9,]+ tokens per request: [0-9.]+x' \
          | tail -1)"
        predicted="${predicted:-<none>}"
      fi
      printf '%s\t%s\n' "${point}" "${predicted}" >>"${ledger}"
      echo "    predicted: ${predicted}" >&2
      # Compose this point's Tier-2 experiment config: the base experiment definition
      # (token budget, SLO, seed) from bench/load-sweep.yaml, with the swept axes this
      # point varies overridden in — its prefix shares, the pinned burstiness, and the
      # concurrency ladder. Written into the point's result dir as the record of what was
      # swept, then handed to `just bench` as its --config.
      point_dir="${run_dir}/${point}"
      mkdir -p "${point_dir}"
      point_config="${point_dir}/sweep-config.yaml"
      SHARES="[${shares_csv}]" BURST="${burst_yaml}" LADDER="${ladder_yaml}" \
        yq '.prefix_shares = env(SHARES) | .burstiness_values = env(BURST) | .max_concurrency_values = env(LADDER)' \
        bench/load-sweep.yaml >"${point_config}"
      # Tier 2: client load ladder against this point, no redeploy. The nested
      # BENCH_RUN_ID lands the per-point JSON under ${run_dir}/${point}/ (see
      # `bench`). load-sweep survives per-cell failures and only exits non-zero at
      # the end, so a non-zero here means some cells failed — keep the partial
      # results and flag the run.
      if ! BENCH_CONFIG="${point_config}" BENCH_RUN_ID="${run_id}/${point}" just bench; then
        echo "!! ${point}: Tier-2 ladder reported failures (partial results kept)" >&2
        overall=1
      fi
    done <<<"${engine_points}"
    echo "knob sweep ${run_id} complete; results in ${run_dir}/ (ceilings: ${ledger})" >&2
    exit "${overall}"

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

    # Compose the cold/warm cell's experiment config: the base definition from
    # bench/load-sweep.yaml, narrowed to this one cell — a single prefix share and
    # burstiness, no concurrency ladder — with the residency-isolating overrides. A seed
    # unique to this invocation makes the cold run emit prefixes the server has never
    # cached, so it genuinely misses; the cold and warm runs share it, so the warm run
    # replays against the now-warm cache (this build exposes no cache-reset route).
    #   align_blocks 16 floors the prefix to whole 16-token blocks — vLLM's prefix cache
    #     reuses whole blocks only, so a ragged tail recomputes every time in both regimes
    #     and dilutes the gap. 16 is vLLM's default block_size; revisit if the served
    #     backend runs a different one.
    #   num_prefixes 16 raises the share of the cold run that is a genuine first exposure
    #     rather than a self-hit on a prefix the run just planted, widening the cold/warm
    #     gap (full isolation would need num_prefixes near num_prompts).
    out="bench/results/prefix-cache/${run_id}"
    mkdir -p "${out}"
    seed="$(date -u +%s)"
    prefix_config="${out}/sweep-config.yaml"
    SHARE="[{{ prefix_share }}]" BURST="[{{ burstiness }}]" SEED="${seed}" \
      yq '.prefix_shares = env(SHARE) | .burstiness_values = env(BURST) | .max_concurrency_values = [] | .align_blocks = 16 | .num_prefixes = 16 | .seed = env(SEED)' \
      bench/load-sweep.yaml >"${prefix_config}"

    # Pass the per-run values as environment prefixed to the host command. The plain
    # values carry no shell-special characters, so single-quoting suffices; the config
    # bytes and the free-form extra flags are base64-encoded so they cross the SSM command
    # line without quoting the host shell (not necessarily bash) could misparse. The
    # api-key is fetched host-side from Secrets Manager, never sent from here. A cold/warm
    # comparison needs both cells, so the host script hard-fails on a cell failure rather
    # than leaving a half result — a non-zero here means nothing worth joining was produced.
    config_b64="$(base64 <"${prefix_config}" | tr -d '\n')"
    args_b64="$(printf '%s' '{{ args }}' | base64 | tr -d '\n')"
    # Count tokens under the served model's name, read from the same model.yaml the
    # pulled image baked its tokenizer from, so the cold and warm cells agree with it.
    model="$({{ image_tag_tool }} hf-id)"
    prefix_env="IMAGE_REF='${image}' RESULTS_BUCKET='${bucket}' MODEL='${model}'"
    prefix_env="${prefix_env} RUN_ID='${run_id}' PREFIX_SHARE='{{ prefix_share }}'"
    prefix_env="${prefix_env} BURSTINESS='{{ burstiness }}' PREFIX_CONFIG_B64='${config_b64}'"
    prefix_env="${prefix_env} PREFIX_ARGS_B64='${args_b64}'"
    run_on_host "prefix-cache" "${prefix_env} /usr/local/bin/bench-prefix-cache.sh" 3600

    # Sync just this run's objects into the per-run subdir, then join locally. The join is
    # a pure function of the synced snapshots and cell JSON (slipstream_bench.prefix_cache).
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

# Assert the bench image's slug and content tags derive from model.yaml as expected (no cluster).
image-tag-test:
    bash test/image-tag-test.sh

# Assert build-image.sh assembles the docker build with the model.yaml args and one -t per ref (docker stubbed, no build).
build-image-test:
    bash test/build-image-test.sh

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
