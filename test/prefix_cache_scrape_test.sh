#!/usr/bin/env bash
# Unit test for the L0 prefix-cache-hit scraper (bench/prefix_cache_scrape.sh).
# Feeds it two synthetic vLLM Prometheus /metrics snapshots bracketing a run plus
# a client result JSON, then pins the arithmetic: the per-run hit rate is the delta
# of the cumulative counters over the window, (hits_after - hits_before) /
# (queries_after - queries_before), never the polluted lifetime ratio. It pins the
# join onto the client JSON, the cold-vs-warm label the record carries, and the
# fail-fast guards on missing snapshots, a missing/disabled metric, a counter that
# went backwards (server restart), and an empty query window. Reproducibility is
# pinned by running twice and diffing. No cluster: the live curl of /metrics is the
# caller's job; this pins the parsing, delta, and join that turn that text into a
# joined record.
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
scraper="${repo_root}/bench/prefix_cache_scrape.sh"

work="$(mktemp -d)"
trap 'rm -rf "${work}"' EXIT

model="Qwen/Qwen2.5-0.5B-Instruct"

# A client result file the join keys on. Only the fields the join echoes need be
# present; the token/latency arrays vllm bench serve saves are irrelevant here.
result="${work}/pshare90_burst1.0.json"
cat >"${result}" <<JSON
{
  "model_id": "${model}",
  "duration": 12.5,
  "completed": 100,
  "request_throughput": 8.0,
  "request_goodput": 7.5,
  "p95_ttft_ms": 850.0,
  "p99_ttft_ms": 990.0,
  "p95_tpot_ms": 42.0,
  "p99_tpot_ms": 48.0
}
JSON

# Prometheus exposition snapshots. The counters are cumulative across the server's
# whole life, so both snapshots start well above zero; only the delta over the
# window is this run's traffic. The HELP/TYPE comment lines carry the metric name
# too and must not be counted as data.
cold_before="${work}/cold_before.prom"
cat >"${cold_before}" <<PROM
# HELP vllm:prefix_cache_queries Prefix cache queries.
# TYPE vllm:prefix_cache_queries counter
vllm:prefix_cache_queries{model_name="${model}"} 1000.0
# HELP vllm:prefix_cache_hits Prefix cache hits.
# TYPE vllm:prefix_cache_hits counter
vllm:prefix_cache_hits{model_name="${model}"} 200.0
PROM

# Cold run: cache was reset, so of 100 queries only 10 hit — a low rate a first
# request stream sees. queries 1000->1100 (delta 100), hits 200->210 (delta 10).
cold_after="${work}/cold_after.prom"
cat >"${cold_after}" <<PROM
# TYPE vllm:prefix_cache_queries counter
vllm:prefix_cache_queries{model_name="${model}"} 1100.0
vllm:prefix_cache_hits{model_name="${model}"} 210.0
PROM

# Warm run: the cache is already populated, so of 100 queries 90 hit — the steady
# state. before is the cold run's after (1100/210); after is 1200/300.
warm_after="${work}/warm_after.prom"
cat >"${warm_after}" <<PROM
# TYPE vllm:prefix_cache_queries counter
vllm:prefix_cache_queries{model_name="${model}"} 1200.0
vllm:prefix_cache_hits{model_name="${model}"} 300.0
PROM

fail=0
out=""
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
  "${scraper}" "$@" >/dev/null 2>&1 || got=$?
  if [[ "${got}" -ne "${want}" ]]; then
    echo "FAIL: ${desc} — expected exit ${want}, got ${got}" >&2
    fail=1
  fi
}
assert_stderr() {
  local desc="$1" needle="$2"
  shift 2
  local err
  err="$("${scraper}" "$@" 2>&1 >/dev/null || true)"
  if ! grep -qF -- "${needle}" <<<"${err}"; then
    echo "FAIL: ${desc} — expected stderr to mention '${needle}'" >&2
    fail=1
  fi
}

# --- Cold run: delta hit rate joined to the client JSON, labelled cold ----------
out="$(
  "${scraper}" --cache-state cold \
    --metrics-before "${cold_before}" --metrics-after "${cold_after}" \
    --result "${result}"
)"

# The delta over the window, not the 210/1100 lifetime ratio.
assert_field "cold queries are the window delta" '.prefix_cache_queries' 100
assert_field "cold hits are the window delta" '.prefix_cache_hits' 10
assert_field "cold hit rate is hits/queries over the window" '.prefix_cache_hit_rate' 0.1
# Cold vs warm distinguishable: the record carries the label (acceptance criterion 3).
assert_field "cold label carried" '.cache_state' cold
# Joined to the client JSON per run (acceptance criterion 2). The SLO numbers ride on
# the record so cold and warm are compared against the SLO without reopening the
# client JSON (spec.md: report cold and steady-state SLO side by side).
assert_field "client model joined" '.model_id' "${model}"
assert_field "client completed joined" '.completed' 100
assert_field "source result recorded" '.source' "${result}"
# The client's numbers pass through verbatim — the scraper joins them, it does not
# reformat them, so the literal forms from the result JSON survive unchanged.
assert_field "ttft p95 joined" '.client_metrics.p95_ttft_ms' 850.0
assert_field "ttft p99 joined" '.client_metrics.p99_ttft_ms' 990.0
assert_field "tpot p95 joined" '.client_metrics.p95_tpot_ms' 42.0
assert_field "tpot p99 joined" '.client_metrics.p99_tpot_ms' 48.0
assert_field "request throughput joined" '.client_metrics.request_throughput' 8.0
assert_field "request goodput joined" '.client_metrics.request_goodput' 7.5

# A result missing a field (a run fired without --goodput has no request_goodput)
# joins as null, never a fabricated number.
nogood="${work}/nogoodput.json"
echo "{\"model_id\":\"${model}\",\"completed\":5,\"p95_ttft_ms\":100.0,\"p99_ttft_ms\":200.0,\"p95_tpot_ms\":10.0,\"p99_tpot_ms\":20.0,\"request_throughput\":3.0}" >"${nogood}"
nogood_out="$(
  "${scraper}" --cache-state cold \
    --metrics-before "${cold_before}" --metrics-after "${cold_after}" \
    --result "${nogood}"
)"
if [[ "$(jq -r '.client_metrics.request_goodput' <<<"${nogood_out}")" != "null" ]]; then
  echo "FAIL: a missing client metric should join as null" >&2
  fail=1
fi

# --- Warm run: same window size, higher hit rate, labelled warm -----------------
cold_out="${out}"
warm_out="$(
  "${scraper}" --cache-state warm \
    --metrics-before "${cold_after}" --metrics-after "${warm_after}" \
    --result "${result}"
)"
out="${warm_out}"
assert_field "warm queries are the window delta" '.prefix_cache_queries' 100
assert_field "warm hits are the window delta" '.prefix_cache_hits' 90
assert_field "warm hit rate is hits/queries over the window" '.prefix_cache_hit_rate' 0.9
assert_field "warm label carried" '.cache_state' warm

# The whole point of the cold/warm split: the warm rate exceeds the cold rate, and
# the labels let a reader tell which is which.
cold_rate="$(jq -r '.prefix_cache_hit_rate' <<<"${cold_out}")"
warm_rate="$(jq -r '.prefix_cache_hit_rate' <<<"${warm_out}")"
if [[ "$(jq -n --argjson c "${cold_rate}" --argjson w "${warm_rate}" '$w > $c')" != "true" ]]; then
  echo "FAIL: warm hit rate (${warm_rate}) should exceed cold (${cold_rate})" >&2
  fail=1
fi

# --- Metric series selected by model when several are exposed -------------------
# A second model's counters share the file; the run's model must not fold them in.
multi_before="${work}/multi_before.prom"
cat >"${multi_before}" <<PROM
# TYPE vllm:prefix_cache_queries counter
vllm:prefix_cache_queries{model_name="${model}"} 1000.0
vllm:prefix_cache_queries{model_name="other/model"} 5000.0
vllm:prefix_cache_hits{model_name="${model}"} 200.0
vllm:prefix_cache_hits{model_name="other/model"} 4000.0
PROM
multi_after="${work}/multi_after.prom"
cat >"${multi_after}" <<PROM
# TYPE vllm:prefix_cache_queries counter
vllm:prefix_cache_queries{model_name="${model}"} 1100.0
vllm:prefix_cache_queries{model_name="other/model"} 9999.0
vllm:prefix_cache_hits{model_name="${model}"} 210.0
vllm:prefix_cache_hits{model_name="other/model"} 8888.0
PROM
out="$(
  "${scraper}" --cache-state cold \
    --metrics-before "${multi_before}" --metrics-after "${multi_after}" \
    --result "${result}"
)"
assert_field "other model's counters not folded in" '.prefix_cache_queries' 100
assert_field "other model's hits not folded in" '.prefix_cache_hits' 10

# --- Exposition _total suffix: prometheus_client renders counters with it -------
# The metric vLLM constructs as vllm:prefix_cache_queries surfaces on /metrics as
# vllm:prefix_cache_queries_total; the scraper must read that rendering too.
total_before="${work}/total_before.prom"
cat >"${total_before}" <<PROM
# TYPE vllm:prefix_cache_queries counter
vllm:prefix_cache_queries_total{model_name="${model}"} 1000.0
vllm:prefix_cache_hits_total{model_name="${model}"} 200.0
PROM
total_after="${work}/total_after.prom"
cat >"${total_after}" <<PROM
# TYPE vllm:prefix_cache_queries counter
vllm:prefix_cache_queries_total{model_name="${model}"} 1100.0
vllm:prefix_cache_hits_total{model_name="${model}"} 290.0
PROM
out="$(
  "${scraper}" --cache-state warm \
    --metrics-before "${total_before}" --metrics-after "${total_after}" \
    --result "${result}"
)"
assert_field "_total-suffixed queries read" '.prefix_cache_queries' 100
assert_field "_total-suffixed hits read" '.prefix_cache_hits' 90
assert_field "_total-suffixed hit rate computed" '.prefix_cache_hit_rate' 0.9

# --- Reproducibility: same inputs, byte-identical output ------------------------
run_a="$("${scraper}" --cache-state cold --metrics-before "${cold_before}" --metrics-after "${cold_after}" --result "${result}")"
run_b="$("${scraper}" --cache-state cold --metrics-before "${cold_before}" --metrics-after "${cold_after}" --result "${result}")"
if [[ "${run_a}" != "${run_b}" ]]; then
  echo "FAIL: re-run does not reproduce the joined record" >&2
  fail=1
fi

# --- Fail-fast guards -----------------------------------------------------------
assert_exit "help exits 0" 0 --help
assert_exit "unknown option exits 2" 2 --nope
assert_exit "missing cache-state rejected" 2 --metrics-before "${cold_before}" --metrics-after "${cold_after}" --result "${result}"
assert_stderr "missing cache-state diagnosed" "cache-state" --metrics-before "${cold_before}" --metrics-after "${cold_after}" --result "${result}"
assert_exit "bad cache-state rejected" 2 --cache-state lukewarm --metrics-before "${cold_before}" --metrics-after "${cold_after}" --result "${result}"
assert_stderr "bad cache-state diagnosed" "cold" --cache-state lukewarm --metrics-before "${cold_before}" --metrics-after "${cold_after}" --result "${result}"
assert_exit "missing before snapshot rejected" 2 --cache-state cold --metrics-after "${cold_after}" --result "${result}"
assert_exit "missing after snapshot rejected" 2 --cache-state cold --metrics-before "${cold_before}" --result "${result}"
assert_exit "missing result rejected" 2 --cache-state cold --metrics-before "${cold_before}" --metrics-after "${cold_after}"
assert_exit "nonexistent snapshot file rejected" 2 --cache-state cold --metrics-before "${work}/nope.prom" --metrics-after "${cold_after}" --result "${result}"
assert_stderr "nonexistent snapshot diagnosed" "not found" --cache-state cold --metrics-before "${work}/nope.prom" --metrics-after "${cold_after}" --result "${result}"

# A snapshot with prefix caching disabled (metric absent) must not price as a 0/0
# rate — it means the scrape has nothing to join, so fail loudly.
nocache="${work}/nocache.prom"
cat >"${nocache}" <<PROM
# TYPE vllm:num_requests_running gauge
vllm:num_requests_running{model_name="${model}"} 0.0
PROM
assert_exit "absent metric rejected" 2 --cache-state cold --metrics-before "${nocache}" --metrics-after "${cold_after}" --result "${result}"
assert_stderr "absent metric named" "vllm:prefix_cache_queries" --cache-state cold --metrics-before "${nocache}" --metrics-after "${cold_after}" --result "${result}"

# A counter that went backwards means the server restarted mid-window; the delta
# would be negative and the rate a lie. Reject it.
restarted="${work}/restarted.prom"
cat >"${restarted}" <<PROM
# TYPE vllm:prefix_cache_queries counter
vllm:prefix_cache_queries{model_name="${model}"} 5.0
vllm:prefix_cache_hits{model_name="${model}"} 1.0
PROM
assert_exit "counter reset rejected" 2 --cache-state cold --metrics-before "${cold_before}" --metrics-after "${restarted}" --result "${result}"
assert_stderr "counter reset diagnosed" "backwards" --cache-state cold --metrics-before "${cold_before}" --metrics-after "${restarted}" --result "${result}"

# An empty query window (no traffic between the two snapshots) is a 0/0 rate — no
# run to measure. Reject rather than emit null and pretend.
assert_exit "empty query window rejected" 2 --cache-state cold --metrics-before "${cold_before}" --metrics-after "${cold_before}" --result "${result}"
assert_stderr "empty window diagnosed" "no prefix cache queries" --cache-state cold --metrics-before "${cold_before}" --metrics-after "${cold_before}" --result "${result}"

if [[ "${fail}" -ne 0 ]]; then
  echo "---- last output ----" >&2
  echo "${out}" >&2
  exit 1
fi

echo "PASS: scraper computes per-run delta hit rate, joins to client JSON, labels cold vs warm, and rejects bad input"
