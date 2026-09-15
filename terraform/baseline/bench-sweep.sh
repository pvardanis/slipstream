#!/usr/bin/env bash
# Run one L0 benchmark sweep from the ephemeral bench host. `just bench` invokes this
# over SSM after bench-proxy-up.sh has the mutual-TLS proxy listening on loopback. It
# drives the baked bench-client image against that proxy (which speaks mTLS to the
# ALB), so the latency it records is what an off-cluster client sees, then copies the
# result JSON to the results bucket where it outlives the host. The recipe passes the
# per-run values (image, bucket, model, run id, extra flags) as SSM parameters; the
# secret ARN, region and loopback port come from the proxy env the boot script drops.
# Idempotent per RUN_ID: re-running overwrites that run's prefix in the bucket.
set -euo pipefail

# Secret ARN, region and the loopback port the proxy listens on, dropped at boot.
# shellcheck source=/dev/null
source /etc/bench-proxy/proxy.env

: "${IMAGE_REF:?bench sweep needs IMAGE_REF (the ECR image reference to run)}"
: "${RESULTS_BUCKET:?bench sweep needs RESULTS_BUCKET (where results are copied)}"
: "${MODEL:?bench sweep needs MODEL (the served model id to sweep)}"
: "${RUN_ID:?bench sweep needs RUN_ID (the results-bucket prefix for this run)}"
# SWEEP_ARGS is optional: extra serve-sweep flags the caller appended to `just bench`.
sweep_args_raw="${SWEEP_ARGS:-}"

echo "bench-sweep: fetching the vLLM api-key" >&2
# vLLM enforces an api-key on /v1; serve-sweep's openai backend sends it as the bearer
# token via OPENAI_API_KEY. Export it so `docker run -e OPENAI_API_KEY` passes it by
# name — the value stays out of docker's argv (it still shows in `docker inspect` on
# this single-tenant throwaway host, an accepted tradeoff for an env-based client).
OPENAI_API_KEY="$(aws secretsmanager get-secret-value \
  --secret-id "${SECRET_ARN}" --region "${REGION}" \
  --query SecretString --output text |
  python3 -c 'import json,sys; print(json.load(sys.stdin)["api_key"])')"
export OPENAI_API_KEY

# serve-sweep writes one JSON per successful cell into the results dir and only exits
# non-zero at the end of the grid, so a partial failure still leaves cells worth
# keeping. Bind-mount a host dir as the run's output and run the container as the host
# user so the JSON is host-owned and the copy-up below can read it.
results_dir="/tmp/bench-results/${RUN_ID}"
rm -rf "${results_dir}"
mkdir -p "${results_dir}"

# Split optional flags on whitespace into an array; serve-sweep flags carry no spaces.
read -ra sweep_args <<<"${sweep_args_raw}"

echo "bench-sweep: running the sweep against the loopback proxy" >&2
# --network host so the container reaches the proxy on 127.0.0.1; --rm for a one-shot.
sweep_rc=0
docker run --rm --network host --user "$(id -u):$(id -g)" \
  -e OPENAI_API_KEY \
  -v "${results_dir}:/out" \
  "${IMAGE_REF}" \
  slipstream-bench serve-sweep \
  --base-url "http://127.0.0.1:${PROXY_PORT}" \
  --model "${MODEL}" \
  --out-dir /out "${sweep_args[@]}" || sweep_rc=$?

# A dry run (or a sweep that failed before any cell) writes no JSON, so there is
# nothing to copy; surface the sweep's own exit code without an empty upload.
count="$(find "${results_dir}" -maxdepth 1 -name '*.json' | wc -l | tr -d ' ')"
if [[ "${count}" -eq 0 ]]; then
  echo "bench-sweep: no result JSON produced (dry run or early failure)" >&2
  exit "${sweep_rc}"
fi

echo "bench-sweep: copying ${count} result files to s3://${RESULTS_BUCKET}/sweeps/${RUN_ID}/" >&2
# Copy whatever landed even on a partial sweep failure, then surface the sweep's exit
# code so the caller sees the failure after the salvageable cells are safely uploaded.
aws s3 cp "${results_dir}" "s3://${RESULTS_BUCKET}/sweeps/${RUN_ID}/" \
  --recursive --region "${REGION}"

echo "bench-sweep: done (${count} cells, sweep exit ${sweep_rc})" >&2
exit "${sweep_rc}"
