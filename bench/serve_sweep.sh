#!/usr/bin/env bash
# L0 benchmark harness: a thin wrapper over `vllm bench serve`, not a bespoke load
# generator. It fires vLLM's native prefix_repetition workload at the CPU replica
# across a grid of prefix-share % and burstiness, applies the platform SLO as
# --goodput ttft:1000 tpot:50, and saves the raw client JSON (per-request TTFT/ITL
# via --save-detailed) one file per grid cell. Everything downstream — the
# cost-per-1M post-processor, the prefix-cache-hit scraper — joins on that JSON.
#
# Prefix-share % is the knob the L3 routing sweep needs: it splits a fixed token
# budget between the shared prefix and the per-request suffix, so share 90 means a
# 900/100 prefix/suffix split of a 1000-token budget. --base-url defaults to
# localhost:8000 for a standalone run; `just bench` runs this from an in-cluster
# client pod and points it at the vLLM service DNS.
set -euo pipefail

base_url="http://localhost:8000"
model="Qwen/Qwen2.5-0.5B-Instruct"
prefix_shares="10 50 90"
burstiness_values="0.2 1.0"
total_len=1000
num_prompts=100
num_prefixes=5
output_len=128
request_rate="8"
out_dir="bench/results"
goodput=(ttft:1000 tpot:50)
dry_run=0

usage() {
  cat <<'USAGE'
Usage: serve_sweep.sh [options]
  --base-url URL             OpenAI-compatible endpoint (default http://localhost:8000)
  --model NAME               served model id
  --prefix-shares "A B C"    prefix-share percentages to sweep
  --burstiness-values "A B"  burstiness values to sweep (low = bursty, 1.0 = Poisson)
  --total-len N              prefix+suffix token budget, split by prefix-share
  --num-prompts N            requests per grid cell
  --num-prefixes N           distinct shared prefixes to generate
  --output-len N             output tokens per request
  --request-rate R           requests/sec (or "inf")
  --out-dir DIR              directory for the per-cell result JSON
  --goodput "ttft:MS tpot:MS"  SLO passed to the harness
  --dry-run                  print the vllm commands instead of running them
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
  --base-url)
    base_url="$2"
    shift 2
    ;;
  --model)
    model="$2"
    shift 2
    ;;
  --prefix-shares)
    prefix_shares="$2"
    shift 2
    ;;
  --burstiness-values)
    burstiness_values="$2"
    shift 2
    ;;
  --total-len)
    total_len="$2"
    shift 2
    ;;
  --num-prompts)
    num_prompts="$2"
    shift 2
    ;;
  --num-prefixes)
    num_prefixes="$2"
    shift 2
    ;;
  --output-len)
    output_len="$2"
    shift 2
    ;;
  --request-rate)
    request_rate="$2"
    shift 2
    ;;
  --out-dir)
    out_dir="$2"
    shift 2
    ;;
  --goodput)
    read -ra goodput <<<"$2"
    shift 2
    ;;
  --dry-run)
    dry_run=1
    shift
    ;;
  -h | --help)
    usage
    exit 0
    ;;
  *)
    echo "unknown option: $1" >&2
    usage >&2
    exit 2
    ;;
  esac
done

# Reject a bad arg before it reaches vLLM, where it would fail deep inside the
# harness with an opaque message (or, for the arithmetic below, silently as 0).
require_positive_int() {
  local name="$1" value="$2"
  [[ "${value}" =~ ^[0-9]+$ && "${value}" -gt 0 ]] || {
    echo "invalid ${name}: '${value}' (want a positive integer)" >&2
    exit 2
  }
}

require_positive_int --total-len "${total_len}"
require_positive_int --num-prompts "${num_prompts}"
require_positive_int --num-prefixes "${num_prefixes}"
require_positive_int --output-len "${output_len}"

for share in ${prefix_shares}; do
  [[ "${share}" =~ ^[0-9]+$ && "${share}" -le 100 ]] || {
    echo "invalid prefix-share: '${share}' (want an integer 0..100)" >&2
    exit 2
  }
done

for burst in ${burstiness_values}; do
  [[ "${burst}" =~ ^[0-9]+(\.[0-9]+)?$ ]] || {
    echo "invalid burstiness: '${burst}' (want a non-negative number)" >&2
    exit 2
  }
done

[[ "${request_rate}" == "inf" || "${request_rate}" =~ ^[0-9]+(\.[0-9]+)?$ ]] || {
  echo "invalid request-rate: '${request_rate}' (want a number or 'inf')" >&2
  exit 2
}

[[ "${#goodput[@]}" -gt 0 ]] || {
  echo "invalid goodput: empty (want e.g. 'ttft:1000 tpot:50')" >&2
  exit 2
}

[[ "${dry_run}" -eq 1 ]] || mkdir -p "${out_dir}"

# One `vllm bench serve` per (prefix-share, burstiness) cell. --goodput carries the
# SLO; --save-result/--save-detailed writes the raw per-request client JSON;
# --percentile-metrics + --metric-percentiles report p95 and p99 side by side on
# the metrics the SLO is expressed in.
run_cell() {
  local share="$1" burst="$2" prefix_len suffix_len result_file
  prefix_len=$((total_len * share / 100))
  suffix_len=$((total_len - prefix_len))
  result_file="${out_dir}/pshare${share}_burst${burst}.json"

  set -- vllm bench serve \
    --backend openai \
    --base-url "${base_url}" \
    --model "${model}" \
    --endpoint /v1/completions \
    --dataset-name prefix_repetition \
    --prefix-repetition-prefix-len "${prefix_len}" \
    --prefix-repetition-suffix-len "${suffix_len}" \
    --prefix-repetition-num-prefixes "${num_prefixes}" \
    --prefix-repetition-output-len "${output_len}" \
    --num-prompts "${num_prompts}" \
    --request-rate "${request_rate}" \
    --burstiness "${burst}" \
    --goodput "${goodput[@]}" \
    --percentile-metrics ttft,tpot,itl,e2el \
    --metric-percentiles 95,99 \
    --save-result \
    --save-detailed \
    --result-filename "${result_file}"

  if [[ "${dry_run}" -eq 1 ]]; then
    echo "$*"
  else
    echo "==> prefix-share ${share}% burstiness ${burst} -> ${result_file}"
    "$@"
  fi
}

# A cell failing (a transient vLLM error, say) should not discard the cells still
# to run: finish the grid, then report the tally and exit non-zero if any failed.
completed=0
failed=0
for share in ${prefix_shares}; do
  for burst in ${burstiness_values}; do
    if run_cell "${share}" "${burst}"; then
      completed=$((completed + 1))
    else
      failed=$((failed + 1))
      echo "!! cell prefix-share ${share}% burstiness ${burst} failed" >&2
    fi
  done
done

if [[ "${failed}" -gt 0 ]]; then
  echo "sweep finished: ${completed} cells ok, ${failed} failed" >&2
  exit 1
fi
