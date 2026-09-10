#!/usr/bin/env bash
# Unit test for the L0 benchmark wrapper (bench/serve_sweep.sh). Exercises the
# command-construction seam through --dry-run: no vLLM server, no cluster, just an
# assertion that the sweep over prefix-share % and burstiness emits one correctly
# flagged `vllm bench serve` invocation per grid cell. The real end-to-end run
# needs a live CPU replica (driven by `just bench`); the bugs live in the flag
# assembly, and that is what this test pins.
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
wrapper="${repo_root}/bench/serve_sweep.sh"

# A 3x2 grid: three prefix-share buckets, two burstiness values. The dry run must
# print exactly six invocations, one per cell.
out="$(
  "${wrapper}" --dry-run \
    --base-url http://localhost:8000 \
    --model Qwen/Qwen2.5-0.5B-Instruct \
    --prefix-shares "25 50 90" \
    --burstiness-values "0.2 1.0" \
    --total-len 1000 \
    --num-prompts 40 \
    --num-prefixes 4 \
    --output-len 64 \
    --request-rate 8 \
    --out-dir /tmp/slipstream-bench
)"

fail=0
assert() {
  local desc="$1" needle="$2"
  if ! grep -qF -- "${needle}" <<<"${out}"; then
    echo "FAIL: ${desc} — expected '${needle}' in dry-run output, not found" >&2
    fail=1
  fi
}
assert_count() {
  local desc="$1" needle="$2" want="$3" got
  got="$(grep -cF -- "${needle}" <<<"${out}")"
  if [[ "${got}" -ne "${want}" ]]; then
    echo "FAIL: ${desc} — expected ${want} occurrences of '${needle}', got ${got}" >&2
    fail=1
  fi
}

# One `vllm bench serve` invocation per grid cell (3 shares x 2 burst = 6).
assert_count "one invocation per grid cell" "vllm bench serve" 6

# The SLO reaches the harness verbatim on every invocation (acceptance criterion 3).
assert_count "goodput on every cell" "--goodput ttft:1000 tpot:50" 6

# Prefix-share % splits a fixed token budget into prefix vs suffix. Share 25 of
# 1000 is a 250/750 split; share 90 is 900/100. This is the sweep's spine.
assert "share 25 -> prefix 250" "--prefix-repetition-prefix-len 250"
assert "share 25 -> suffix 750" "--prefix-repetition-suffix-len 750"
assert "share 90 -> prefix 900" "--prefix-repetition-prefix-len 900"
assert "share 90 -> suffix 100" "--prefix-repetition-suffix-len 100"
assert "prefix_repetition dataset" "--dataset-name prefix_repetition"

# Burst sweep: both burstiness values appear, three cells each.
assert_count "bursty cells" "--burstiness 0.2" 3
assert_count "smooth cells" "--burstiness 1.0" 3

# Raw client JSON, per request. --save-detailed is what carries per-request TTFT
# and ITL arrays (TPOT derives from ITL); one distinct file per grid cell.
assert_count "save result" "--save-result" 6
assert_count "per-request detail" "--save-detailed" 6
assert "distinct file for share25/burst0.2" "pshare25_burst0.2.json"
assert "distinct file for share90/burst1.0" "pshare90_burst1.0.json"

# Report p95 and p99 side by side, on the metrics the SLO is expressed in.
assert "percentile metrics include tpot" "--percentile-metrics ttft,tpot,itl,e2el"
assert "p95 and p99" "--metric-percentiles 95,99"

# Fixed request parameters flow through unchanged.
assert "num prompts" "--num-prompts 40"
assert "num prefixes" "--prefix-repetition-num-prefixes 4"
assert "output len" "--prefix-repetition-output-len 64"
assert "request rate" "--request-rate 8"
assert "base url" "--base-url http://localhost:8000"

if [[ "${fail}" -ne 0 ]]; then
  echo "---- dry-run output ----" >&2
  echo "${out}" >&2
  exit 1
fi

echo "PASS: sweep emits one correctly flagged vllm bench serve per grid cell"
