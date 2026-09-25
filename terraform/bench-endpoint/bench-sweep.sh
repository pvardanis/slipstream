#!/usr/bin/env bash
# Run one L0 benchmark cell from the ephemeral bench host. The orchestration layer
# invokes this once per grid cell over SSM after bench-proxy-up.sh has the mutual-TLS
# proxy listening on loopback (ADR-0012 §Amendment: the per-cell loop lives in the
# orchestration layer, the container runs one cell). It drives the baked bench-client
# image against that proxy (which speaks mTLS to the ALB), so the latency it records is
# what an off-cluster client sees, then copies the one result JSON to the results
# bucket where it outlives the host. The caller passes the per-run values (image,
# bucket, model, run id, the cell's share/burstiness/max-concurrency, extra flags) as
# environment assignments prefixed to the SSM command; the secret ARN, region and
# loopback port come from the proxy env the boot script drops. Re-running a RUN_ID with
# the same cell overwrites that cell's object under the prefix.
set -euo pipefail

# Secret ARN, region and the loopback port the proxy listens on, dropped at boot.
# shellcheck source=/dev/null
source /etc/bench-proxy/proxy.env

: "${IMAGE_REF:?bench cell needs IMAGE_REF (the ECR image reference to run)}"
: "${RESULTS_BUCKET:?bench cell needs RESULTS_BUCKET (where results are copied)}"
: "${MODEL:?bench cell needs MODEL (the served model id to measure)}"
: "${RUN_ID:?bench cell needs RUN_ID (the results-bucket prefix for this run)}"
# The cell's grid coordinate: prefix-share percent and burstiness are required; the
# max-concurrency cap is optional (its absence runs the cell open-loop).
: "${SHARE:?bench cell needs SHARE (the cell prefix-share percent)}"
: "${BURSTINESS:?bench cell needs BURSTINESS (the cell burstiness)}"
max_concurrency="${MAX_CONCURRENCY:-}"
# The experiment-definition YAML, base64-encoded by the caller so it crosses the SSM
# command line intact; decoded to a file below, mounted into the container and passed as
# load-cell's --config. It carries the shared knobs (lengths, SLO, seed); the cell's
# coordinate is passed explicitly above, not read from its grid axes. Required — the
# caller always sends one (a per-point config from knob-sweep, else the checked-in
# default).
: "${SWEEP_CONFIG_B64:?bench cell needs SWEEP_CONFIG_B64 (the base64 load-cell config)}"
# SWEEP_ARGS_B64 is optional: extra load-cell flags the caller appended, base64-encoded
# so they cross the SSM command line without any quoting that the host shell (not
# necessarily bash) would misparse.
sweep_args_b64="${SWEEP_ARGS_B64:-}"

echo "bench-cell: fetching the vLLM api-key" >&2
# vLLM enforces an api-key on /v1; load-cell's openai backend sends it as the bearer
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

# load-cell writes one JSON for the cell it is handed and exits 0 on success, 1 when the
# cell fails to run or its result cannot be stamped, 2 on a config/setup error (a bad
# config, an out-of-range coordinate, an unset key). Bind-mount a host dir as the cell's
# output; run the container as the invoking user so the JSON is not root-owned if this is
# ever run as non-root (it runs as root under SSM today, where id -u is 0 and the mapping
# is a no-op).
results_dir="/tmp/bench-results/${RUN_ID}"
rm -rf "${results_dir}"
mkdir -p "${results_dir}"

# Decode the experiment config into its own dir, mounted read-only into the container
# and passed as load-cell's --config. base64 -d is the last stage of its own pipe, so
# invalid base64 fails the pipeline under pipefail and aborts the run before the cell
# starts, rather than proceeding with a config the cell would reject.
config_dir="/tmp/bench-config/${RUN_ID}"
rm -rf "${config_dir}"
mkdir -p "${config_dir}"
printf '%s' "${SWEEP_CONFIG_B64}" | base64 -d >"${config_dir}/sweep-config.yaml"

# Decode the optional flags and split on whitespace into an array; load-cell flags
# carry no spaces, so word-splitting the decoded string reconstructs them. The decode
# is its own statement, not a command substitution feeding the read here-string: a
# substitution's failure does not trip set -e, so a truncated SWEEP_ARGS_B64 would
# otherwise be swallowed and the caller's extra flags silently dropped. Guarding it
# aborts before the cell runs, mirroring the config decode above.
sweep_args=()
if [[ -n "${sweep_args_b64}" ]]; then
  if ! decoded_args="$(printf '%s' "${sweep_args_b64}" | base64 -d)"; then
    echo "bench-cell: SWEEP_ARGS_B64 is not valid base64" >&2
    exit 1
  fi
  read -ra sweep_args <<<"${decoded_args}"
fi

# A set max-concurrency caps in-flight requests (closed-loop); an empty one omits the
# flag so the cell runs open-loop, arrival-rate bound.
cap_args=()
if [[ -n "${max_concurrency}" ]]; then
  cap_args=(--max-concurrency "${max_concurrency}")
fi

echo "bench-cell: running the cell against the loopback proxy" >&2
# --network host so the container reaches the proxy on 127.0.0.1; --rm for a one-shot.
# --entrypoint slipstream-bench overrides the base image's `vllm serve` entrypoint so
# the container runs the bench harness, not the server; load-cell is then its arg.
cell_rc=0
docker run --rm --network host --user "$(id -u):$(id -g)" \
  --entrypoint slipstream-bench \
  -e OPENAI_API_KEY \
  -v "${results_dir}:/out" \
  -v "${config_dir}:/config:ro" \
  "${IMAGE_REF}" \
  load-cell \
  --config /config/sweep-config.yaml \
  --base-url "http://127.0.0.1:${PROXY_PORT}" \
  --model "${MODEL}" \
  --share "${SHARE}" \
  --burstiness "${BURSTINESS}" \
  "${cap_args[@]}" \
  --out-dir /out "${sweep_args[@]}" || cell_rc=$?

# No JSON landed: either a clean dry run (cell exited 0) or a real failure — a failed
# image pull, a daemon-down docker (125/126/127), or the cell erroring before it wrote.
# Distinguish them so a docker/cell failure is not misread as a benign dry run.
count="$(find "${results_dir}" -maxdepth 1 -name '*.json' | wc -l | tr -d ' ')"
if [[ "${count}" -eq 0 ]]; then
  if [[ "${cell_rc}" -eq 0 ]]; then
    echo "bench-cell: no result JSON produced (dry run)" >&2
  else
    echo "bench-cell: no result JSON produced; cell/docker failed (exit ${cell_rc})" >&2
  fi
  exit "${cell_rc}"
fi

echo "bench-cell: copying the result to s3://${RESULTS_BUCKET}/sweeps/${RUN_ID}/" >&2
# Copy the cell's JSON, then surface the cell's exit code so the caller sees a stamp
# failure after the salvageable result is safely uploaded.
aws s3 cp "${results_dir}" "s3://${RESULTS_BUCKET}/sweeps/${RUN_ID}/" \
  --recursive --region "${REGION}"

echo "bench-cell: done (${count} cell, exit ${cell_rc})" >&2
exit "${cell_rc}"
