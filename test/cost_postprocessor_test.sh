#!/usr/bin/env bash
# Unit test for the L0 cost post-processor (bench/cost_per_million.sh). Feeds it a
# synthetic `vllm bench serve` result JSON with known token counts and duration,
# then pins the arithmetic: run cost = price/hr x wall-clock, and the whole machine
# cost split across input/output tokens by a pinned output:input ratio into
# $/1M-input and $/1M-output. It also pins the provenance the figure is only
# meaningful with — weight checksum, vLLM version, quant recipe — and the fail-fast
# guards on missing pins, missing metrics, and a zero-token denominator. The
# reproducibility acceptance criterion is pinned by running twice and diffing.
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
proc="${repo_root}/bench/cost_per_million.sh"

work="$(mktemp -d)"
trap 'rm -rf "${work}"' EXIT

# A minimal result file with the fields the post-processor joins on. Duration is
# one hour so the run cost equals the hourly price exactly, making the split math
# checkable by hand. 1,000,000 input tokens and 0 output tokens: with ratio 1 the
# whole $2.00 lands on input, so $/1M-input is $2.00 and $/1M-output tracks it.
result="${work}/cell.json"
cat >"${result}" <<'JSON'
{
  "model_id": "Qwen/Qwen2.5-0.5B-Instruct",
  "duration": 3600.0,
  "completed": 100,
  "total_input_tokens": 1000000,
  "total_output_tokens": 0
}
JSON

fail=0
out=""
# jq-extracted numeric field must equal an expected value.
assert_field() {
  local desc="$1" filter="$2" want="$3" got
  got="$(jq -r "${filter}" <<<"${out}")"
  if [[ "${got}" != "${want}" ]]; then
    echo "FAIL: ${desc} — ${filter} is '${got}', want '${want}'" >&2
    fail=1
  fi
}
assert_exit() {
  local desc="$1" want="$2"
  shift 2
  local got=0
  "${proc}" "$@" >/dev/null 2>&1 || got=$?
  if [[ "${got}" -ne "${want}" ]]; then
    echo "FAIL: ${desc} — expected exit ${want}, got ${got}" >&2
    fail=1
  fi
}
assert_stderr() {
  local desc="$1" needle="$2"
  shift 2
  local err
  err="$("${proc}" "$@" 2>&1 >/dev/null || true)"
  if ! grep -qF -- "${needle}" <<<"${err}"; then
    echo "FAIL: ${desc} — expected stderr to mention '${needle}'" >&2
    fail=1
  fi
}

pins=(--weight-checksum "sha256:deadbeef" --vllm-version "0.6.3" --quant-recipe "awq_marlin+fp8-kv")

# --- Happy path: one hour at $2/hr, 1M input tokens, ratio 1 ------------------
out="$(
  "${proc}" --price-per-hour 2.00 --output-input-ratio 1 "${pins[@]}" "${result}"
)"

# Output is a JSON array, one cost record per input file.
assert_field "one record emitted" 'length' 1
assert_field "run cost is one hour at the hourly price" '.[0].run_cost_usd' 2
# Whole $2 over 1M input tokens (ratio 1, no output tokens) -> $2.00 per 1M input.
assert_field "cost per 1M input" '.[0].cost_per_1m_input_usd' 2
assert_field "cost per 1M output tracks ratio" '.[0].cost_per_1m_output_usd' 2

# Provenance pins ride on every record (acceptance criterion 2).
assert_field "weight checksum pinned" '.[0].weight_checksum' "sha256:deadbeef"
assert_field "vllm version pinned" '.[0].vllm_version' "0.6.3"
assert_field "quant recipe pinned" '.[0].quant_recipe' "awq_marlin+fp8-kv"
assert_field "ratio pinned" '.[0].output_input_ratio' 1
assert_field "price pinned" '.[0].price_per_hour_usd' 2

# Bench metrics echoed through so the figure is self-describing.
assert_field "model echoed" '.[0].model_id' "Qwen/Qwen2.5-0.5B-Instruct"
assert_field "input tokens echoed" '.[0].total_input_tokens' 1000000
assert_field "source file recorded" '.[0].source' "${result}"

# --- Ratio splits the machine cost: output priced 3x input -------------------
# 1M input + 1M output tokens, one hour at $4/hr, ratio 3. Denominator is
# I + r*O = 1e6 + 3e6 = 4e6 token-equivalents; p_in = $4 / 4e6 = $1e-6/token, so
# $1.00 per 1M input and $3.00 per 1M output. The two numbers sum-weight back to
# the whole run cost, which is the honesty the separate pricing buys.
split="${work}/split.json"
cat >"${split}" <<'JSON'
{
  "model_id": "m",
  "duration": 3600.0,
  "completed": 10,
  "total_input_tokens": 1000000,
  "total_output_tokens": 1000000
}
JSON
out="$("${proc}" --price-per-hour 4.00 --output-input-ratio 3 "${pins[@]}" "${split}")"
assert_field "input priced at 1x share" '.[0].cost_per_1m_input_usd' 1
assert_field "output priced at 3x share" '.[0].cost_per_1m_output_usd' 3

# --- Fractional figures: a half-hour at $1.50/hr, non-integer results ---------
# Prices and durations that don't divide evenly are where an integer-truncation or
# precision bug would hide. Half an hour at $1.50 is a $0.75 run; 500k input + 250k
# output at ratio 2 is a 1,000,000 token-equivalent denominator, so $0.75/1M-input
# and $1.50/1M-output. Every number here is non-integer on purpose.
frac="${work}/frac.json"
cat >"${frac}" <<'JSON'
{
  "model_id": "m",
  "duration": 1800.0,
  "completed": 50,
  "total_input_tokens": 500000,
  "total_output_tokens": 250000
}
JSON
out="$("${proc}" --price-per-hour 1.50 --output-input-ratio 2 "${pins[@]}" "${frac}")"
assert_field "half-hour run costs $0.75" '.[0].run_cost_usd' 0.75
assert_field "fractional cost per 1M input" '.[0].cost_per_1m_input_usd' 0.75
assert_field "fractional cost per 1M output" '.[0].cost_per_1m_output_usd' 1.5

# --- Multiple files: one record per file, order preserved --------------------
out="$("${proc}" --price-per-hour 2.00 --output-input-ratio 1 "${pins[@]}" "${result}" "${split}")"
assert_field "a record per input file" 'length' 2
assert_field "first record is the first file" '.[0].source' "${result}"
assert_field "second record is the second file" '.[1].source' "${split}"

# --- Reproducibility: same inputs, byte-identical output (criterion 3) --------
run_a="$("${proc}" --price-per-hour 2.00 --output-input-ratio 1 "${pins[@]}" "${result}")"
run_b="$("${proc}" --price-per-hour 2.00 --output-input-ratio 1 "${pins[@]}" "${result}")"
if [[ "${run_a}" != "${run_b}" ]]; then
  echo "FAIL: re-run does not reproduce the figure" >&2
  fail=1
fi

# --- Fail-fast guards --------------------------------------------------------
assert_exit "help exits 0" 0 --help
assert_exit "unknown option exits 2" 2 --nope "${result}"
assert_exit "missing price rejected" 2 "${pins[@]}" "${result}"
assert_stderr "missing price diagnosed" "price-per-hour" "${pins[@]}" "${result}"
assert_exit "missing checksum rejected" 2 --price-per-hour 2 --output-input-ratio 1 --vllm-version 0.6.3 --quant-recipe q "${result}"
assert_stderr "missing checksum diagnosed" "weight-checksum" --price-per-hour 2 --output-input-ratio 1 --vllm-version 0.6.3 --quant-recipe q "${result}"
assert_exit "missing vllm version rejected" 2 --price-per-hour 2 --output-input-ratio 1 --weight-checksum s --quant-recipe q "${result}"
assert_exit "missing quant recipe rejected" 2 --price-per-hour 2 --output-input-ratio 1 --weight-checksum s --vllm-version 0.6.3 "${result}"
# The blend ratio is pinned deliberately, not defaulted: leaving it off is an error,
# so a run can never silently report input and output priced equally.
assert_exit "missing ratio rejected" 2 --price-per-hour 2 "${pins[@]}" "${result}"
assert_stderr "missing ratio diagnosed" "output-input-ratio" --price-per-hour 2 "${pins[@]}" "${result}"
assert_exit "no input files rejected" 2 --price-per-hour 2 --output-input-ratio 1 "${pins[@]}"
assert_stderr "no input files diagnosed" "no result files" --price-per-hour 2 --output-input-ratio 1 "${pins[@]}"
assert_exit "non-numeric price rejected" 2 --price-per-hour cheap --output-input-ratio 1 "${pins[@]}" "${result}"
assert_exit "zero price rejected" 2 --price-per-hour 0 --output-input-ratio 1 "${pins[@]}" "${result}"
assert_exit "negative ratio rejected" 2 --price-per-hour 2 --output-input-ratio -1 "${pins[@]}" "${result}"
assert_exit "missing file rejected" 2 --price-per-hour 2 --output-input-ratio 1 "${pins[@]}" "${work}/nope.json"
assert_stderr "missing file diagnosed" "not found" --price-per-hour 2 --output-input-ratio 1 "${pins[@]}" "${work}/nope.json"

# A result missing the token metrics the cost joins on must not silently price as 0.
notok="${work}/notok.json"
echo '{"model_id":"m","duration":3600.0,"completed":1}' >"${notok}"
assert_exit "missing token metrics rejected" 2 --price-per-hour 2 --output-input-ratio 1 "${pins[@]}" "${notok}"
assert_stderr "missing metric named" "total_input_tokens" --price-per-hour 2 --output-input-ratio 1 "${pins[@]}" "${notok}"

# A metric present but null passes a bare key check yet jq reads it as 0 — reject it,
# or the input side is silently dropped and the figure inflates.
nulltok="${work}/nulltok.json"
echo '{"model_id":"m","duration":3600.0,"completed":1,"total_input_tokens":null,"total_output_tokens":100}' >"${nulltok}"
assert_exit "null token metric rejected" 2 --price-per-hour 2 --output-input-ratio 1 "${pins[@]}" "${nulltok}"
assert_stderr "null metric named" "total_input_tokens" --price-per-hour 2 --output-input-ratio 1 "${pins[@]}" "${nulltok}"

# A non-positive duration prices the whole run at $0 — the same silent-$0 the metrics guard.
zerodur="${work}/zerodur.json"
echo '{"model_id":"m","duration":0,"completed":1,"total_input_tokens":1000,"total_output_tokens":10}' >"${zerodur}"
assert_exit "zero duration rejected" 2 --price-per-hour 2 --output-input-ratio 1 "${pins[@]}" "${zerodur}"
assert_stderr "zero duration diagnosed" "non-positive duration" --price-per-hour 2 --output-input-ratio 1 "${pins[@]}" "${zerodur}"

# Zero tokens on both sides is a zero denominator, not a $0 figure — reject it.
zerotok="${work}/zerotok.json"
echo '{"model_id":"m","duration":3600.0,"completed":0,"total_input_tokens":0,"total_output_tokens":0}' >"${zerotok}"
assert_exit "zero-token denominator rejected" 2 --price-per-hour 2 --output-input-ratio 1 "${pins[@]}" "${zerotok}"

if [[ "${fail}" -ne 0 ]]; then
  echo "---- last output ----" >&2
  echo "${out}" >&2
  exit 1
fi

echo "PASS: cost post-processor splits run cost into \$/1M in/out, pins provenance, and rejects bad input"
