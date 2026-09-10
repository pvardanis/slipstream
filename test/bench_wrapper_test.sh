#!/usr/bin/env bash
# Unit test for the L0 benchmark wrapper (bench/serve_sweep.sh). Exercises the
# command-construction seam through --dry-run: no vLLM server, no cluster, just an
# assertion that the sweep over prefix-share % and burstiness emits one correctly
# flagged `vllm bench serve` invocation per grid cell. It also pins the input
# validation — the wrapper rejects a bad arg before it reaches vLLM — and the
# defaults contract the docstring advertises. The real end-to-end run needs a live
# CPU replica (driven by `just bench`); the bugs live in the flag assembly and the
# arg guards, and that is what this test pins.
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
wrapper="${repo_root}/bench/serve_sweep.sh"

fail=0
# The scenario under assertion. Each block reassigns `out` to the dry-run output it
# is checking; assert/assert_count read whatever `out` currently holds.
out=""
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
# A bad arg must fail before vLLM is ever built: exit 2 with a diagnostic naming
# the offending value.
assert_exit() {
  local desc="$1" want="$2"
  shift 2
  local got=0
  "${wrapper}" "$@" >/dev/null 2>&1 || got=$?
  if [[ "${got}" -ne "${want}" ]]; then
    echo "FAIL: ${desc} — expected exit ${want}, got ${got}" >&2
    fail=1
  fi
}
assert_stderr() {
  local desc="$1" needle="$2"
  shift 2
  local err
  err="$("${wrapper}" "$@" 2>&1 >/dev/null || true)"
  if ! grep -qF -- "${needle}" <<<"${err}"; then
    echo "FAIL: ${desc} — expected stderr to mention '${needle}'" >&2
    fail=1
  fi
}

# --- Command assembly: a 3x2 grid, all flags overridden ---------------------
# Three prefix-share buckets, two burstiness values. The dry run must print
# exactly six invocations, one per cell.
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

# --- Defaults contract: a zero-override dry run emits the documented defaults --
out="$("${wrapper}" --dry-run)"
assert_count "default grid is 3 shares x 2 burst" "vllm bench serve" 6
assert "default base url" "--base-url http://localhost:8000"
assert "default model" "--model Qwen/Qwen2.5-0.5B-Instruct"
assert "default goodput SLO" "--goodput ttft:1000 tpot:50"
assert "default share 10 -> prefix 100" "--prefix-repetition-prefix-len 100"
assert "default share 50 -> prefix 500" "--prefix-repetition-prefix-len 500"
assert "default share 90 -> prefix 900" "--prefix-repetition-prefix-len 900"
assert "default num prompts" "--num-prompts 100"
assert "default num prefixes" "--prefix-repetition-num-prefixes 5"
assert "default output len" "--prefix-repetition-output-len 128"
assert "default request rate" "--request-rate 8"
assert_count "default burst 0.2 on 3 cells" "--burstiness 0.2" 3
assert_count "default burst 1.0 on 3 cells" "--burstiness 1.0" 3

# --- Goodput override: the space-split re-expands as two tokens, not one -------
out="$("${wrapper}" --dry-run --goodput "ttft:500 tpot:25")"
assert_count "overridden goodput on every cell" "--goodput ttft:500 tpot:25" 6

# --- Split-math boundaries: shares 0 and 100, and integer truncation ----------
out="$("${wrapper}" --dry-run --total-len 1000 --prefix-shares "0 100" --burstiness-values 1.0)"
assert "share 0 -> prefix 0" "--prefix-repetition-prefix-len 0"
assert "share 0 -> suffix 1000" "--prefix-repetition-suffix-len 1000"
assert "share 100 -> prefix 1000" "--prefix-repetition-prefix-len 1000"
assert "share 100 -> suffix 0" "--prefix-repetition-suffix-len 0"

out="$("${wrapper}" --dry-run --total-len 100 --prefix-shares 33 --burstiness-values 1.0)"
assert "share 33 of 100 truncates to prefix 33" "--prefix-repetition-prefix-len 33"
assert "share 33 of 100 truncates to suffix 67" "--prefix-repetition-suffix-len 67"

# --- Control-flow branches ---------------------------------------------------
assert_exit "help exits 0" 0 --help
assert_exit "unknown option exits 2" 2 --nope
assert_stderr "unknown option names the flag" "unknown option: --nope" --nope

# --- Input validation: a bad arg is rejected before vLLM is built -------------
assert_exit "non-numeric total-len rejected" 2 --dry-run --total-len foo
assert_stderr "non-numeric total-len diagnosed" "invalid --total-len" --dry-run --total-len foo
assert_exit "zero total-len rejected" 2 --dry-run --total-len 0
assert_exit "non-numeric num-prompts rejected" 2 --dry-run --num-prompts abc
assert_exit "prefix-share above 100 rejected" 2 --dry-run --prefix-shares 150
assert_stderr "out-of-range share diagnosed" "invalid prefix-share" --dry-run --prefix-shares 150
assert_exit "negative-looking share rejected" 2 --dry-run --prefix-shares -5
assert_exit "empty goodput rejected" 2 --dry-run --goodput ""
assert_stderr "empty goodput diagnosed" "invalid goodput" --dry-run --goodput ""
assert_exit "non-numeric burstiness rejected" 2 --dry-run --burstiness-values bursty
assert_exit "non-numeric request-rate rejected" 2 --dry-run --request-rate quick
assert_exit "request-rate inf accepted" 0 --dry-run --request-rate inf

if [[ "${fail}" -ne 0 ]]; then
  echo "---- last dry-run output ----" >&2
  echo "${out}" >&2
  exit 1
fi

echo "PASS: sweep emits correct commands, honours defaults, and rejects bad input"
