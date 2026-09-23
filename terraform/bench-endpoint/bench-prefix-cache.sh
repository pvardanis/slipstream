#!/usr/bin/env bash
# Run one cold/warm prefix-cache comparison from the ephemeral bench host. `just
# prefix-cache` invokes this over SSM after bench-proxy-up.sh has the mutual-TLS proxy
# listening on loopback. It drives the baked bench-client image against that proxy
# (which speaks mTLS to the ALB) for one bench cell twice — a cold run against
# never-cached prefixes, then a warm run replaying the same seed against the now-warm
# cache — bracketing each with a /metrics snapshot so the local join can compute each
# run's hit rate over its own window. The cell JSON and the four snapshots are copied
# to the results bucket where they outlive the host; the join itself runs on the
# operator's laptop against the synced files. The recipe passes the per-run values
# (image, bucket, model, run id, the cell's prefix-share/burstiness, extra flags) as
# environment assignments prefixed to the SSM command; the secret ARN, region and
# loopback port come from the proxy env the boot script drops.
#
# Unlike bench-sweep.sh there is no partial-grid salvage: a cold/warm comparison needs
# both runs, so a failed cell aborts before anything is copied rather than leaving a
# half result that reads like a measurement.
set -euo pipefail

# Secret ARN, region and the loopback port the proxy listens on, dropped at boot.
# shellcheck source=/dev/null
source /etc/bench-proxy/proxy.env

: "${IMAGE_REF:?prefix-cache needs IMAGE_REF (the ECR image reference to run)}"
: "${RESULTS_BUCKET:?prefix-cache needs RESULTS_BUCKET (where results are copied)}"
: "${MODEL:?prefix-cache needs MODEL (the served model id to measure)}"
: "${RUN_ID:?prefix-cache needs RUN_ID (the results-bucket prefix for this run)}"
: "${PREFIX_SHARE:?prefix-cache needs PREFIX_SHARE (the cell prefix-share percent)}"
: "${BURSTINESS:?prefix-cache needs BURSTINESS (the cell burstiness)}"
# The cold/warm cell's experiment config, base64-encoded by `just prefix-cache` so it
# crosses the SSM command line intact; it carries the single-cell grid, the unique seed,
# and the residency-isolating overrides (align_blocks, num_prefixes). Decoded to a file
# below, mounted into the container and passed as load-sweep's --config.
: "${PREFIX_CONFIG_B64:?prefix-cache needs PREFIX_CONFIG_B64 (the base64 load-sweep config)}"
# PREFIX_ARGS_B64 is optional: extra load-sweep flags the caller appended to
# `just prefix-cache`, base64-encoded so they cross the SSM command line without any
# quoting that the host shell (not necessarily bash) would misparse.
prefix_args_b64="${PREFIX_ARGS_B64:-}"

echo "prefix-cache: fetching the vLLM api-key" >&2
# vLLM enforces an api-key on /v1; load-sweep's openai backend sends it as the bearer
# token via OPENAI_API_KEY. Export it so `docker run -e OPENAI_API_KEY` passes it by
# name — the value stays out of docker's argv (it still shows in `docker inspect` on
# this single-tenant throwaway host, an accepted tradeoff for an env-based client).
# The reader takes the secret JSON on stdin (off argv) and names the missing field
# rather than dying on a raw traceback when the secret lacks an api_key.
OPENAI_API_KEY="$(aws secretsmanager get-secret-value \
  --secret-id "${SECRET_ARN}" --region "${REGION}" \
  --query SecretString --output text |
  python3 /usr/local/bin/bench_secret_field.py api_key)"
export OPENAI_API_KEY

base_url="http://127.0.0.1:${PROXY_PORT}"
results_dir="/tmp/prefix-cache-results/${RUN_ID}"
rm -rf "${results_dir}"
mkdir -p "${results_dir}"

# Decode the experiment config into its own dir, mounted read-only into the container
# and passed as load-sweep's --config for both the cold and the warm run. It carries the
# unique seed that makes the cold run miss and the warm run replay against the now-warm
# cache. base64 -d is the last stage of its own pipe, so invalid base64 fails the
# pipeline under pipefail and aborts the run before either cell, rather than proceeding
# with a config the runs would reject.
config_dir="/tmp/prefix-cache-config/${RUN_ID}"
rm -rf "${config_dir}"
mkdir -p "${config_dir}"
printf '%s' "${PREFIX_CONFIG_B64}" | base64 -d >"${config_dir}/sweep-config.yaml"

# Decode the optional flags and split on whitespace into an array; load-sweep flags
# carry no spaces, so word-splitting the decoded string reconstructs them. Decode on
# its own line so a corrupt PREFIX_ARGS_B64 aborts here: piping straight into the
# here-string would hide the failure, since read returns 0 whatever the pipe's status.
decoded_args="$(printf '%s' "${prefix_args_b64}" | base64 -d)"
read -ra prefix_args <<<"${decoded_args}"

# vLLM's /metrics counters are cumulative over the whole server life, so only the delta
# across a run window is that run's traffic; snapshot before and after each run. The
# scrape goes through the loopback proxy like the sweep, so /metrics is read over the
# same mTLS path to the ALB. -sf so an HTTP error is a non-zero exit rather than an
# empty snapshot the local join would misread as a zero window.
scrape() {
  curl -sf "${base_url}/metrics" >"$1"
}

# One bench cell (a single prefix-share/burstiness) run twice, cold then warm. The
# workload shape that makes cache residency the only variable in the gap — the single
# share/burstiness cell, the shared seed, and the align_blocks/num_prefixes isolation —
# lives in the mounted config `just prefix-cache` composed; only the --out-dir differs
# between the two runs so each writes its own cell JSON.
# --network host so the container reaches the proxy on 127.0.0.1; --rm for a one-shot;
# run as the invoking user so the JSON is not root-owned (a no-op under SSM's root).
# --entrypoint slipstream-bench overrides the base image's `vllm serve` entrypoint so
# the container runs the bench harness, not the server; load-sweep is then its arg.
run_cell() {
  docker run --rm --network host --user "$(id -u):$(id -g)" \
    --entrypoint slipstream-bench \
    -e OPENAI_API_KEY \
    -v "${results_dir}:/out" \
    -v "${config_dir}:/config:ro" \
    "${IMAGE_REF}" \
    load-sweep \
    --config /config/sweep-config.yaml \
    --base-url "${base_url}" --model "${MODEL}" \
    --out-dir "$1" "${prefix_args[@]}"
}

cell="pshare${PREFIX_SHARE}_burst${BURSTINESS}.json"

echo "prefix-cache: cold run (fresh prefixes, seed from config)" >&2
scrape "${results_dir}/cold_before.prom"
run_cell /out/cold
scrape "${results_dir}/cold_after.prom"
cp "${results_dir}/cold/${cell}" "${results_dir}/cold_${cell}"

echo "prefix-cache: warm run (same seed, now-warm cache)" >&2
scrape "${results_dir}/warm_before.prom"
run_cell /out/warm
scrape "${results_dir}/warm_after.prom"
cp "${results_dir}/warm/${cell}" "${results_dir}/warm_${cell}"

# Copy the two snapshots per run and the two cell JSONs (the per-run cold/ and warm/
# subdirs are working scratch and stay behind). --exclude '*/*' keeps the recursive
# copy to the top level so those subdirs are not re-uploaded.
echo "prefix-cache: copying results to s3://${RESULTS_BUCKET}/prefix-cache/${RUN_ID}/" >&2
aws s3 cp "${results_dir}" "s3://${RESULTS_BUCKET}/prefix-cache/${RUN_ID}/" \
  --recursive --exclude '*/*' --region "${REGION}"

echo "prefix-cache: done (cold + warm cells for ${cell})" >&2
