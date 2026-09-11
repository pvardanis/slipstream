#!/usr/bin/env bash
# L0 prefix-cache-hit scraper + join: turns the server-side prefix-cache counters
# vLLM exposes on its Prometheus /metrics endpoint into a per-run hit rate joined to
# the client result JSON. vLLM's vllm:prefix_cache_queries / vllm:prefix_cache_hits
# are counters cumulative across the whole server life, so their bare ratio is
# polluted by every prior request; the run's true rate is the delta over the run
# window. The caller curls /metrics before and after the run into two snapshot files
# and hands them here with the run's client JSON; this computes
# (hits_after - hits_before) / (queries_after - queries_before) and joins it onto the
# client record, carrying a cold/warm label so the two cache regimes are told apart.
# The run's SLO numbers (TTFT/TPOT p95/p99, throughput, goodput) ride on the record
# too, so cold and warm are compared against the SLO without reopening the client JSON.
#
# Cold vs warm is the caller's doing: reset the prefix cache (POST /reset_prefix_cache)
# before the cold run, reuse the warmed cache for the warm run, and pass the matching
# --cache-state. The output is a pure function of its inputs — a re-run reproduces the
# record — and one invocation joins exactly one run.
set -euo pipefail

queries_metric="vllm:prefix_cache_queries"
hits_metric="vllm:prefix_cache_hits"

cache_state=""
metrics_before=""
metrics_after=""
result=""
model=""

usage() {
  cat <<'USAGE'
Usage: prefix_cache_scrape.sh --cache-state cold|warm \
         --metrics-before SNAP --metrics-after SNAP --result CLIENT_JSON [--model NAME]
  --cache-state cold|warm   which cache regime this run measured
  --metrics-before SNAP     /metrics text captured just before the run
  --metrics-after SNAP      /metrics text captured just after the run
  --result CLIENT_JSON      the run's `vllm bench serve --save-result` JSON
  --model NAME              model_name label to select (default: the result's model_id)
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
  --cache-state)
    cache_state="$2"
    shift 2
    ;;
  --metrics-before)
    metrics_before="$2"
    shift 2
    ;;
  --metrics-after)
    metrics_after="$2"
    shift 2
    ;;
  --result)
    result="$2"
    shift 2
    ;;
  --model)
    model="$2"
    shift 2
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

require_flag() {
  local name="$1" value="$2"
  [[ -n "${value}" ]] || {
    echo "missing required ${name}" >&2
    exit 2
  }
}
require_flag --cache-state "${cache_state}"
require_flag --metrics-before "${metrics_before}"
require_flag --metrics-after "${metrics_after}"
require_flag --result "${result}"

# The label is the whole basis of the cold-vs-warm distinction; a free-text value
# would let a typo mislabel a regime, so accept only the two the split defines.
[[ "${cache_state}" == "cold" || "${cache_state}" == "warm" ]] || {
  echo "invalid --cache-state: '${cache_state}' (want cold or warm)" >&2
  exit 2
}

for file in "${metrics_before}" "${metrics_after}" "${result}"; do
  [[ -f "${file}" ]] || {
    echo "file not found: ${file}" >&2
    exit 2
  }
done

# A per-cell failure can leave a syntactically-valid but empty/stub result JSON; the
# join would then emit null model_id/completed behind a real-looking hit rate. The
# record is only meaningful keyed to a real run, so require both fields up front.
[[ "$(jq -r '(.model_id != null) and (.completed != null)' "${result}")" == "true" ]] || {
  echo "result ${result} missing model_id/completed (truncated or empty run?)" >&2
  exit 2
}

# Default the metric-series selector to the model the client ran against, so a
# multi-model server's other series never fold into this run's counters. An empty
# selector would sum *every* model's series — a silent cross-model rate — so a result
# without model_id and no --model is an error, not an all-series sum.
[[ -n "${model}" ]] || model="$(jq -r '.model_id // empty' "${result}")"
[[ -n "${model}" ]] || {
  echo "could not determine model from ${result}: no model_id and no --model given" >&2
  exit 2
}

# Sum a counter's value across the series matching the model label, ignoring the
# HELP/TYPE comment lines that carry the metric name too. A prometheus_client counter
# renders in the exposition with a _total suffix (vllm:prefix_cache_queries_total),
# while vLLM's metric reference and PromQL name it without one, so accept either.
# Fails when the metric is absent entirely — prefix caching disabled or the series
# never emitted — which is a nothing-to-join error (read_counter turns it into a
# diagnosed exit 2), not a silent zero.
extract_counter() {
  local file="$1" name="$2" want_model="$3"
  awk -v name="${name}" -v want_model="${want_model}" '
    /^[[:space:]]*#/ { next }
    {
      key = $1
      sub(/\{.*/, "", key)
      if (key != name && key != name "_total") next
      if (want_model != "" && index($0, "model_name=\"" want_model "\"") == 0) next
      sum += $2
      found = 1
    }
    END {
      if (!found) exit 3
      # Integer counters: %g caps at 6 significant digits and %.10g at 10, both of
      # which round the large cumulative values a long-lived server reaches into
      # scientific notation and corrupt the delta. %.0f keeps the full integer.
      printf "%.0f", sum
    }
  ' "${file}"
}

read_counter() {
  local file="$1" name="$2" value
  if ! value="$(extract_counter "${file}" "${name}" "${model}")"; then
    echo "metric ${name} not found in ${file} (is prefix caching enabled on the server?)" >&2
    exit 2
  fi
  printf '%s' "${value}"
}

queries_before="$(read_counter "${metrics_before}" "${queries_metric}")"
queries_after="$(read_counter "${metrics_after}" "${queries_metric}")"
hits_before="$(read_counter "${metrics_before}" "${hits_metric}")"
hits_after="$(read_counter "${metrics_after}" "${hits_metric}")"

queries="$(jq -n --argjson a "${queries_after}" --argjson b "${queries_before}" '$a - $b')"
hits="$(jq -n --argjson a "${hits_after}" --argjson b "${hits_before}" '$a - $b')"

# A counter that shrank over the window means the server restarted (or the cache was
# reset) mid-run; the delta is meaningless and the rate would be a lie.
[[ "$(jq -n --argjson q "${queries}" --argjson h "${hits}" '$q >= 0 and $h >= 0')" == "true" ]] || {
  echo "prefix cache counters went backwards between snapshots (server restart mid-run?)" >&2
  exit 2
}

# No queries in the window is a 0/0 rate: there is no run to measure, not a 0% run.
[[ "$(jq -n --argjson q "${queries}" '$q > 0')" == "true" ]] || {
  echo "no prefix cache queries between the snapshots (nothing to measure)" >&2
  exit 2
}

jq -n \
  --arg source "${result}" \
  --arg state "${cache_state}" \
  --argjson queries "${queries}" \
  --argjson hits "${hits}" \
  --slurpfile client "${result}" \
  '{
    source: $source,
    model_id: ($client[0].model_id),
    cache_state: $state,
    completed: ($client[0].completed),
    prefix_cache_queries: $queries,
    prefix_cache_hits: $hits,
    prefix_cache_hit_rate: ($hits / $queries),
    client_metrics: {
      request_throughput: ($client[0].request_throughput),
      request_goodput: ($client[0].request_goodput),
      p95_ttft_ms: ($client[0].p95_ttft_ms),
      p99_ttft_ms: ($client[0].p99_ttft_ms),
      p95_tpot_ms: ($client[0].p95_tpot_ms),
      p99_tpot_ms: ($client[0].p99_tpot_ms)
    }
  }'
