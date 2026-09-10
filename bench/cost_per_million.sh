#!/usr/bin/env bash
# L0 cost post-processor: turns raw `vllm bench serve` client JSON into a $/1M-token
# figure. It reads the token counts and wall-clock the harness saved (--save-result),
# multiplies the wall-clock by the instance hourly price to get the run's cost, then
# splits that whole machine cost across input and output tokens by a pinned output:input
# ratio into $/1M-input and $/1M-output reported separately (ratio 1 prices them
# equally; a commercial-style ratio like 3 weights decode tokens heavier). The figure
# is only meaningful pinned to the artifact that produced it, so the weight-blob
# checksum, vLLM version, and quant recipe are required and ride on every record. The
# output is a pure function of its inputs — a re-run reproduces the figure.
#
# Input token count I and output token count O, hourly price P, wall-clock D seconds,
# ratio r: run cost C = P * D / 3600; per-input-token price p_in = C / (I + r*O); then
# $/1M-input = p_in * 1e6 and $/1M-output = r * p_in * 1e6. Emits a JSON array to
# stdout, one cost record per result file, in the order given.
set -euo pipefail

price_per_hour=""
weight_checksum=""
vllm_version=""
quant_recipe=""
output_input_ratio=""
files=()

usage() {
  cat <<'USAGE'
Usage: cost_per_million.sh --price-per-hour P --output-input-ratio r \
         --weight-checksum SHA --vllm-version V --quant-recipe R FILE [FILE...]
  --price-per-hour P         instance price in USD/hour (on-demand or spot)
  --weight-checksum SHA      checksum of the weight blob the run served
  --vllm-version V           vLLM version that produced the result
  --quant-recipe R           quantization recipe (e.g. awq_marlin+fp8-kv)
  --output-input-ratio r     price weight of an output token vs an input token (1 = equal)
  FILE...                    `vllm bench serve --save-result` JSON files to price
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
  --price-per-hour)
    price_per_hour="$2"
    shift 2
    ;;
  --weight-checksum)
    weight_checksum="$2"
    shift 2
    ;;
  --vllm-version)
    vllm_version="$2"
    shift 2
    ;;
  --quant-recipe)
    quant_recipe="$2"
    shift 2
    ;;
  --output-input-ratio)
    output_input_ratio="$2"
    shift 2
    ;;
  -h | --help)
    usage
    exit 0
    ;;
  --*)
    echo "unknown option: $1" >&2
    usage >&2
    exit 2
    ;;
  *)
    files+=("$1")
    shift
    ;;
  esac
done

# A cost figure detached from what produced it is a lie waiting to happen: refuse to
# emit one without the price and the full provenance triple.
require_flag() {
  local name="$1" value="$2"
  [[ -n "${value}" ]] || {
    echo "missing required ${name}" >&2
    exit 2
  }
}
# The ratio is required, not defaulted: leaving it to default 1 would silently price
# input and output equally and report the very blended figure the separation exists to
# avoid. Pinning the blend convention is a deliberate act, like the provenance pins.
require_flag --price-per-hour "${price_per_hour}"
require_flag --output-input-ratio "${output_input_ratio}"
require_flag --weight-checksum "${weight_checksum}"
require_flag --vllm-version "${vllm_version}"
require_flag --quant-recipe "${quant_recipe}"

# Price and ratio feed a division; a zero price is a free-GPU fiction and a
# non-positive ratio inverts the split, so reject both before the arithmetic.
require_positive_number() {
  local name="$1" value="$2"
  if [[ ! "${value}" =~ ^[0-9]+(\.[0-9]+)?$ ]] ||
    [[ "$(jq -n --argjson v "${value}" '$v > 0')" != "true" ]]; then
    echo "invalid ${name}: '${value}' (want a positive number)" >&2
    exit 2
  fi
}
require_positive_number --price-per-hour "${price_per_hour}"
require_positive_number --output-input-ratio "${output_input_ratio}"

[[ "${#files[@]}" -gt 0 ]] || {
  echo "no result files given" >&2
  usage >&2
  exit 2
}

# Price one result file into a single JSON cost record. A file missing the token
# metrics the cost joins on, or one whose input/output tokens are both zero (a zero
# denominator), is an error — never a silent $0.
price_file() {
  local file="$1"
  [[ -f "${file}" ]] || {
    echo "result file not found: ${file}" >&2
    exit 2
  }

  # A metric present but null (or a string) passes a bare has() yet prices as 0 — jq
  # reads null as 0 in arithmetic — so check the value is actually a number, not just
  # that the key exists. Otherwise the cost joins on a silent 0.
  local metric
  for metric in duration total_input_tokens total_output_tokens; do
    [[ "$(jq -r --arg m "${metric}" '(.[$m] | type) == "number"' "${file}")" == "true" ]] || {
      echo "result ${file} missing or non-numeric metric ${metric}" >&2
      exit 2
    }
  done

  # A non-positive duration prices the whole run at $0, and zero tokens on both sides is
  # a zero denominator — both are the silent-$0 the token/duration metrics exist to catch.
  [[ "$(jq -r '.duration > 0' "${file}")" == "true" ]] || {
    echo "result ${file} has non-positive duration (nothing to price)" >&2
    exit 2
  }

  [[ "$(jq -r '(.total_input_tokens + .total_output_tokens) > 0' "${file}")" == "true" ]] || {
    echo "result ${file} has zero input and output tokens (nothing to price)" >&2
    exit 2
  }

  jq \
    --arg source "${file}" \
    --argjson price "${price_per_hour}" \
    --argjson ratio "${output_input_ratio}" \
    --arg checksum "${weight_checksum}" \
    --arg version "${vllm_version}" \
    --arg recipe "${quant_recipe}" \
    '
    ($price * .duration / 3600) as $cost
    | ($cost / (.total_input_tokens + $ratio * .total_output_tokens)) as $p_in
    | {
        source: $source,
        model_id: .model_id,
        duration_s: .duration,
        completed: .completed,
        total_input_tokens: .total_input_tokens,
        total_output_tokens: .total_output_tokens,
        price_per_hour_usd: ($price + 0), # + 0 normalizes e.g. "2.00" to 2 for stable output

        output_input_ratio: $ratio,
        run_cost_usd: $cost,
        cost_per_1m_input_usd: ($p_in * 1000000),
        cost_per_1m_output_usd: ($ratio * $p_in * 1000000),
        weight_checksum: $checksum,
        vllm_version: $version,
        quant_recipe: $recipe
      }
    ' "${file}"
}

# Collect one record per file into a JSON array, order preserved.
records=()
for file in "${files[@]}"; do
  records+=("$(price_file "${file}")")
done

printf '%s\n' "${records[@]}" | jq -s '.'
