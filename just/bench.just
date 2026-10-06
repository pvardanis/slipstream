# Benchmark lifecycle: the bench-client image, the per-point client-load sweep, the prefix-cache measurement, results sync, and the ephemeral mTLS bench endpoint.

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
[group('bench')]
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

# Run one engine point's Tier-2 client-load ladder — the cells bench/sweep-grid.yaml defines for that point — resumably through Prefect, against the currently-deployed engine over the mutual-TLS bench endpoint. Each cell is one resumable task keyed by the deep config digest, so a re-run skips cells that already hold a valid measurement (ADR-0012). POINT_SLUG names the grid point the live engine serves (e.g. mns64_kvfp8_pcon). Requires a live bench endpoint (`just bench-endpoint-up`).
[group('bench')]
bench point_slug *args:
    #!/usr/bin/env bash
    set -euo pipefail
    # Bare assignments so a failed output lookup aborts rather than driving SSM at an
    # empty instance id or copying from an empty bucket (see `cluster-up`).
    region="$(terraform -chdir={{ eks_dir }} output -raw region)"
    instance="$(terraform -chdir={{ bench_endpoint_dir }} output -raw bench_host_instance_id)"
    bucket="$(terraform -chdir={{ bench_endpoint_dir }} output -raw results_bucket_name)"
    # Forward bench_image_tag into the nested call: a command-line override does not
    # cross into a child `just` invocation, so an unforwarded pin would silently fall
    # back to the floating main tag and run the cell on the wrong image.
    image="$(just bench_image_tag='{{ bench_image_tag }}' _bench-image-ref)"
    # A UTC timestamp is the run's prefix in the bucket and the local results subdir, so
    # concurrent or repeated runs never overwrite each other. A set BENCH_RUN_ID nests
    # this point's ladder under that shared run id (marking it non-standalone); when it is
    # unset this is a standalone point sweep that mints and announces its own id, so a
    # failed run can be resumed by passing that id back as BENCH_RUN_ID.
    if [[ -n "${BENCH_RUN_ID:-}" ]]; then
      run_id="${BENCH_RUN_ID}"
      standalone=0
    else
      run_id="$(date -u +%Y%m%dT%H%M%SZ)"
      standalone=1
      echo "==> bench run ${run_id}" >&2
    fi

    # The cell cache index and flow-run history live in a Prefect API; bring one up
    # (a local server by default, or an operator-set PREFECT_API_URL) for the run.
    source bench/prefect-server.sh
    prefect_server_up

    # Extra load-cell flags (--dry-run, --api-key-env) appended to `just bench`, base64-
    # encoded so they cross the SSM command line to the host without quoting the host
    # shell could misparse.
    args_b64="$(printf '%s' '{{ args }}' | base64 | tr -d '\n')"

    # The orchestrator brings the loopback proxy up once, enumerates the point's cells
    # from the grid (the single source of the swept axes and load knobs — no --config and
    # no --model here), and runs each as a resumable cell task. The served model id and
    # the digest read from --model-yaml; the served image ref from --vllm-manifest. Its
    # status is captured, not left to abort the recipe, so the sync below always runs.
    rc=0
    uv run slipstream-orchestrate point-sweep \
      --run-id "${run_id}/{{ point_slug }}" \
      --point-slug "{{ point_slug }}" \
      --instance-id "${instance}" \
      --region "${region}" \
      --bucket "${bucket}" \
      --image-ref "${image}" \
      --model-yaml model.yaml \
      --sweep-grid bench/sweep-grid.yaml \
      --vllm-manifest {{ gpu_manifests }} \
      --sweep-args-b64 "${args_b64}" || rc=$?

    # Sync regardless of the sweep's status: a resumed cell is served from S3 and never
    # re-downloaded locally, and a partially-failed point (a permanent cell fault raises
    # through the flow after earlier cells landed) still leaves valid cells worth pulling
    # into the per-run subdir. Surface the sweep's status only after the copy is complete.
    mkdir -p "bench/results/${run_id}"
    aws s3 sync "s3://${bucket}/sweeps/${run_id}" "bench/results/${run_id}" --region "${region}"
    echo "results synced to bench/results/${run_id}/"
    # A standalone point sweep that ended non-zero left cells unmeasured; surface the id
    # to resume it. A nested run (BENCH_RUN_ID set, standalone=0) leaves the resume hint
    # to its caller, so the two never print conflicting resume commands.
    if ((rc != 0)) && ((standalone == 1)); then
      echo "point sweep ${run_id} incomplete. Resume (re-runs only unmeasured cells):" >&2
      echo "  BENCH_RUN_ID=${run_id} just bench {{ point_slug }}" >&2
    fi
    exit "${rc}"

# Sync every sweep and prefix-cache run's results from the bench endpoint results bucket to bench/results for local reporting; use to pull runs made from another machine.
[group('bench')]
bench-results-sync:
    #!/usr/bin/env bash
    set -euo pipefail
    region="$(terraform -chdir={{ eks_dir }} output -raw region)"
    bucket="$(terraform -chdir={{ bench_endpoint_dir }} output -raw results_bucket_name)"
    mkdir -p bench/results/prefix-cache
    aws s3 sync "s3://${bucket}/sweeps" bench/results --region "${region}"
    aws s3 sync "s3://${bucket}/prefix-cache" bench/results/prefix-cache --region "${region}"
    echo "synced sweep and prefix-cache results to bench/results/"

# Measure the prefix-cache hit rate for a cold and a warm run of one bench cell from the external bench host over SSM, and join each to its client JSON locally (in bench/results/prefix-cache/<run-id>). Requires a live bench endpoint (`just bench-endpoint-up`).
[group('bench')]
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
    # Forward bench_image_tag into the nested call: a command-line override does not
    # cross into a child `just` invocation, so an unforwarded pin would silently fall
    # back to the floating main tag and run the cell on the wrong image.
    image="$(just bench_image_tag='{{ bench_image_tag }}' _bench-image-ref)"
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

# Stand up the ephemeral public bench endpoint: a mutual-TLS load balancer
# fronting vLLM, so both arms can be measured from one host outside the cluster.
# It exists only for the run; `bench-endpoint-down` tears it down. Requires the cluster
# up and vLLM deployed (`just cluster-up && just cpu-deploy`).
[group('bench')]
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
[group('bench')]
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
