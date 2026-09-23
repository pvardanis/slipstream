#!/usr/bin/env bash
# Run one L0 benchmark sweep from the ephemeral bench host. `just bench` invokes this
# over SSM after bench-proxy-up.sh has the mutual-TLS proxy listening on loopback. It
# drives the baked bench-client image against that proxy (which speaks mTLS to the
# ALB), so the latency it records is what an off-cluster client sees, then copies the
# result JSON to the results bucket where it outlives the host. The recipe passes the
# per-run values (image, bucket, model, run id, extra flags) as environment
# assignments prefixed to the SSM command; the secret ARN, region and loopback port
# come from the proxy env the boot script drops. Re-running a RUN_ID overwrites each
# cell's object under that prefix (stale objects from a different grid are not purged).
set -euo pipefail

# Secret ARN, region and the loopback port the proxy listens on, dropped at boot.
# shellcheck source=/dev/null
source /etc/bench-proxy/proxy.env

: "${IMAGE_REF:?bench sweep needs IMAGE_REF (the ECR image reference to run)}"
: "${RESULTS_BUCKET:?bench sweep needs RESULTS_BUCKET (where results are copied)}"
: "${MODEL:?bench sweep needs MODEL (the served model id to sweep)}"
: "${RUN_ID:?bench sweep needs RUN_ID (the results-bucket prefix for this run)}"
# The experiment-definition YAML, base64-encoded by `just bench` so it crosses the SSM
# command line intact; decoded to a file below, mounted into the container and passed as
# load-sweep's --config. Required — the recipe always sends one (a per-point config from
# knob-sweep, else the checked-in default).
: "${SWEEP_CONFIG_B64:?bench sweep needs SWEEP_CONFIG_B64 (the base64 load-sweep config)}"
# SWEEP_ARGS_B64 is optional: extra load-sweep flags the caller appended to
# `just bench`, base64-encoded so they cross the SSM command line without any quoting
# that the host shell (not necessarily bash) would misparse.
sweep_args_b64="${SWEEP_ARGS_B64:-}"

echo "bench-sweep: fetching the vLLM api-key" >&2
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

# load-sweep writes one JSON per successful cell into the results dir and only exits
# non-zero at the end of the grid, so a partial failure still leaves cells worth
# keeping. Bind-mount a host dir as the run's output; run the container as the invoking
# user so the JSON is not root-owned if this is ever run as non-root (it runs as root
# under SSM today, where id -u is 0 and the mapping is a no-op).
results_dir="/tmp/bench-results/${RUN_ID}"
rm -rf "${results_dir}"
mkdir -p "${results_dir}"

# Decode the experiment config into its own dir, mounted read-only into the container
# and passed as load-sweep's --config. Decode on its own line so a corrupt
# SWEEP_CONFIG_B64 aborts here rather than writing a truncated config the sweep would
# then reject cell by cell.
config_dir="/tmp/bench-config/${RUN_ID}"
rm -rf "${config_dir}"
mkdir -p "${config_dir}"
printf '%s' "${SWEEP_CONFIG_B64}" | base64 -d >"${config_dir}/sweep-config.yaml"

# Decode the optional flags and split on whitespace into an array; load-sweep flags
# carry no spaces, so word-splitting the decoded string reconstructs them.
read -ra sweep_args <<<"$(printf '%s' "${sweep_args_b64}" | base64 -d)"

echo "bench-sweep: running the sweep against the loopback proxy" >&2
# --network host so the container reaches the proxy on 127.0.0.1; --rm for a one-shot.
# --entrypoint slipstream-bench overrides the base image's `vllm serve` entrypoint so
# the container runs the bench harness, not the server; load-sweep is then its arg.
sweep_rc=0
docker run --rm --network host --user "$(id -u):$(id -g)" \
  --entrypoint slipstream-bench \
  -e OPENAI_API_KEY \
  -v "${results_dir}:/out" \
  -v "${config_dir}:/config:ro" \
  "${IMAGE_REF}" \
  load-sweep \
  --config /config/sweep-config.yaml \
  --base-url "http://127.0.0.1:${PROXY_PORT}" \
  --model "${MODEL}" \
  --out-dir /out "${sweep_args[@]}" || sweep_rc=$?

# No JSON landed: either a clean dry run (sweep exited 0) or a real failure before any
# cell — a failed image pull, a daemon-down docker (125/126/127), or an early sweep
# error. Distinguish them so a docker/sweep failure is not misread as a benign dry run.
count="$(find "${results_dir}" -maxdepth 1 -name '*.json' | wc -l | tr -d ' ')"
if [[ "${count}" -eq 0 ]]; then
  if [[ "${sweep_rc}" -eq 0 ]]; then
    echo "bench-sweep: no result JSON produced (dry run)" >&2
  else
    echo "bench-sweep: no result JSON produced; sweep/docker failed (exit ${sweep_rc})" >&2
  fi
  exit "${sweep_rc}"
fi

echo "bench-sweep: copying ${count} result files to s3://${RESULTS_BUCKET}/sweeps/${RUN_ID}/" >&2
# Copy whatever landed even on a partial sweep failure, then surface the sweep's exit
# code so the caller sees the failure after the salvageable cells are safely uploaded.
aws s3 cp "${results_dir}" "s3://${RESULTS_BUCKET}/sweeps/${RUN_ID}/" \
  --recursive --region "${REGION}"

echo "bench-sweep: done (${count} cells, sweep exit ${sweep_rc})" >&2
exit "${sweep_rc}"
