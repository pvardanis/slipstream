#!/usr/bin/env bash
# Static goodput parity check: the Collector's slo_met gates
# (k8s/otel-collector-config.yaml, in seconds) must equal the goodput gates every
# bench config passes to `vllm bench serve` (["ttft:1000", "tpot:50"], in ms), so
# live and offline goodput cannot drift apart (ADR-0026). Dashboard queries may
# compare ttft_ms only as span.ttft_ms > <ms>, at the same TTFT gate, in a query of
# vLLM's llm_request spans piped at most to select(), and a Tempo table panel must
# hold one. <ms> must be a decimal literal: the Collector writes ttft_ms as a
# double, and Tempo matches no double against an integer literal. Reads the
# configs with yq and the dashboards with jq; needs no running Collector.
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
collector_config="${OTEL_COLLECTOR_CONFIG:-${repo_root}/k8s/otel-collector-config.yaml}"
bench_configs_dir="${BENCH_CONFIGS_DIR:-${repo_root}/bench/configs}"
dashboards_dir="${DASHBOARDS_DIR:-${repo_root}/terraform/grafana/dashboards}"

fail=0

# The transform processor's OTTL statements, one per line.
statements="$(yq '.processors.transform.trace_statements[].statements[]' "${collector_config}")"
# Every statement that sets slo_met other than its false default. There must be
# exactly one, the gated true; any other would override the gates at runtime.
slo_met_statement="$(grep -F 'set(span.attributes["slo_met"]' <<<"${statements}" |
  grep -vxF 'set(span.attributes["slo_met"], false)' || true)"

# Every statement that sets gate $1's (ttft or tpot) cache value. There must be
# exactly one, unconditional; a second would override the value the check reads.
collector_gate_statements() {
  grep -F "set(span.cache[\"$1_gate_s\"]" <<<"${statements}" || true
}

# The Collector's gate $1 in seconds, from its one unconditional
# set(span.cache["<gate>_gate_s"], <value>) statement. Empty when absent.
collector_gate_s() {
  sed -n "s/^set(span.cache\[\"$1_gate_s\"\], \([0-9.]*\))\$/\1/p" <<<"${statements}"
}

# Every bench config file, relative to the repo root.
bench_configs() {
  find "${bench_configs_dir}" \( -name '*.yaml' -o -name '*.yml' \) | sed "s|^${repo_root}/||" | sort
}

# Every "<config>: <gate>:<ms>" goodput entry across the bench configs, wherever
# the goodput list sits in the config (top level or under load). find exits
# non-zero when yq fails on any config.
bench_gates() {
  find "${bench_configs_dir}" \( -name '*.yaml' -o -name '*.yml' \) -exec yq --no-doc \
    '.. | select(tag == "!!map") | select(has("goodput")) | .goodput[] | filename + ": " + .' {} + |
    sed "s|^${repo_root}/||" | sort
}

if [[ "$(grep -c . <<<"${slo_met_statement}")" -ne 1 ]] ||
  [[ "${slo_met_statement}" != 'set(span.attributes["slo_met"], true) where '* ]]; then
  echo "FAIL: ${collector_config} must set slo_met true in exactly one statement," \
    "gated by a where clause; found:" >&2
  echo "${slo_met_statement:-  (none)}" >&2
  fail=1
fi

ttft_s=""
tpot_s=""
for gate in ttft tpot; do
  gate_statements="$(collector_gate_statements "${gate}")"
  if [[ "$(grep -c . <<<"${gate_statements}")" -ne 1 ]]; then
    echo "FAIL: ${collector_config} sets span.cache[\"${gate}_gate_s\"] in" \
      "$(grep -c . <<<"${gate_statements}") statements; expected exactly one" >&2
    fail=1
    continue
  fi
  value="$(collector_gate_s "${gate}")"
  if [[ -z "${value}" ]]; then
    echo "FAIL: no ${gate} gate in ${collector_config}; expected a statement" \
      "set(span.cache[\"${gate}_gate_s\"], <seconds>) in the transform processor" >&2
    fail=1
  fi
  printf -v "${gate}_s" '%s' "${value}"
  if ! grep -qF "<= span.cache[\"${gate}_gate_s\"]" <<<"${slo_met_statement}"; then
    echo "FAIL: the slo_met statement in ${collector_config} does not compare against" \
      "span.cache[\"${gate}_gate_s\"]; the gate the check reads is not the one applied" >&2
    fail=1
  fi
done

entries="$(bench_gates)"
if [[ -z "${entries}" ]]; then
  echo "FAIL: no goodput gates found under ${bench_configs_dir}; the check would pass vacuously" >&2
  fail=1
fi

while IFS= read -r entry; do
  [[ -n "${entry}" ]] || continue
  config="${entry%%: *}"
  gate="${entry#*: }"
  name="${gate%%:*}"
  ms="${gate#*:}"
  if ! [[ "${ms}" =~ ^[0-9]+(\.[0-9]+)?$ ]]; then
    echo "FAIL: ${config} gates ${name} at '${ms}', not a number of milliseconds" >&2
    fail=1
    continue
  fi
  case "${name}" in
  ttft) collector_s="${ttft_s}" ;;
  tpot) collector_s="${tpot_s}" ;;
  *)
    echo "FAIL: ${config} gates goodput on '${name}', which the Collector's slo_met" \
      "does not; add it to the transform or drop it from the config" >&2
    fail=1
    continue
    ;;
  esac
  # vllm bench serve converts the ms gate to seconds by dividing by 1000; compare
  # the same way so the two read one threshold.
  if [[ -z "${collector_s}" ]] || ! awk -v ms="${ms}" -v s="${collector_s}" 'BEGIN { exit !(ms / 1000 == s) }'; then
    echo "FAIL: ${config} gates ${name} at ${ms} ms but the Collector's slo_met uses" \
      "${collector_s:-nothing} s (${collector_config}); make them equal" >&2
    fail=1
  fi
done <<<"${entries}"

# slo_met applies both gates jointly, so every bench config must gate on both;
# a config gating on fewer, or on none, would count goodput the Collector does not.
while IFS= read -r config; do
  [[ -n "${config}" ]] || continue
  for gate in ttft tpot; do
    if ! grep -q "^${config}: ${gate}:" <<<"${entries}"; then
      echo "FAIL: ${config} has no ${gate} goodput gate, but the Collector's slo_met" \
        "applies one; add it to the config's goodput list" >&2
      fail=1
    fi
  done
done <<<"$(bench_configs)"

# One "<dashboard><TAB><kind><TAB><ms>" line per span.ttft_ms > <ms> comparison in
# a dashboard query, <kind> being table-cut in a Tempo table panel and cut anywhere
# else. A table panel and its target count as Tempo only when both name a tempo
# datasource. The table's cut must be visible and run as TraceQL, or it gives one
# "<dashboard><TAB>inactive<TAB><refId>" line, and every other visible target on
# that table gives one "<dashboard><TAB>extra<TAB><refId>" line, since its rows
# would mix into the table's. A query that cuts ttft_ms must be exactly
#   { resource.service.name = "vllm" && name = "llm_request" && span.ttft_ms > <ms> }
# with at most a select() after it, so a negated cut, an || beside it, another
# service or a pipeline stage that drops rows cannot empty the table unchecked;
# such a query gives one "<dashboard><TAB>unshaped<TAB><query>" line. A query that
# mentions ttft_ms anywhere other than that cut or a select() list gives one
# "<dashboard><TAB>unrecognised<TAB><query>" line, so a reversed, unscoped or
# differently-directed comparison cannot go unchecked either.
dashboard_ttft_cuts() {
  find "${dashboards_dir}" -name '*.json' -exec jq -r \
    'def ms: "[0-9]+(?:\\.[0-9]+)?";
    . as $root | paths(objects and has("query")) as $path | getpath($path) as $target
      | $target.query | strings | select(test("ttft_ms")) as $q
      | (if $path[-2] == "targets" then $root | getpath($path[:-2]) else null end) as $panel
      | ($panel.type == "table" and (($panel.datasource | objects | .type) // null) == "tempo"
        and (($target.datasource | objects | .type) // "tempo") == "tempo") as $table
      | ([$q | scan("ttft_ms\\b")] | length) as $mentions
      | [$q | scan("(?<![\\w.])span\\.ttft_ms\\s*>\\s*(" + ms + ")(?![\\w.])")[0]] as $cuts
      | ([$q | scan("select\\([^)]*(?<![\\w.])span\\.ttft_ms\\s*[,)]")] | length) as $selected
      | ($q | test("^\\s*\\{\\s*resource\\.service\\.name\\s*=\\s*\"vllm\"\\s*&&\\s*name\\s*=\\s*\"llm_request\"\\s*&&\\s*span\\.ttft_ms\\s*>\\s*" + ms + "\\s*\\}(?:\\s*\\|\\s*select\\([^()|]*\\))?\\s*$")) as $shaped
      | if $mentions != ($cuts | length) + $selected
        then input_filename + "\tunrecognised\t" + ($q | gsub("\\s+"; " "))
        elif ($cuts | length) > 0 and ($shaped | not)
        then input_filename + "\tunshaped\t" + ($q | gsub("\\s+"; " "))
        elif ($cuts | length) > 0 and $table and ($target.hide == true or $target.queryType != "traceql")
        then input_filename + "\tinactive\t" + ($target.refId // "?")
        else $cuts[] | input_filename + "\t" + (if $table then "table-cut" else "cut" end) + "\t" + . end,
    ($root | .. | objects | select(.type == "table" and (.targets | type) == "array") | .targets
      | select(any(.[]; .query | strings | test("ttft_ms")))
      | .[] | select(.hide != true and (.query | strings | test("ttft_ms") | not))
      | input_filename + "\textra\t" + (.refId // "?"))' {} + |
    sed "s|^${repo_root}/||" | sort
}

cuts="$(dashboard_ttft_cuts)" || {
  echo "FAIL: cannot read the dashboards under ${dashboards_dir}; every one must be valid JSON" >&2
  exit 1
}
if ! grep -q $'\ttable-cut\t' <<<"${cuts}"; then
  echo "FAIL: no Tempo table panel under ${dashboards_dir} filters span.ttft_ms > <ms>;" \
    "the requests-over-the-TTFT-gate table must cut at the TTFT gate" >&2
  fail=1
fi
while IFS=$'\t' read -r dashboard kind value; do
  [[ -n "${dashboard}" ]] || continue
  if [[ "${kind}" == unrecognised ]]; then
    echo "FAIL: ${dashboard} compares ttft_ms other than as span.ttft_ms > <ms>, the" \
      "requests over the Collector's TTFT gate: ${value}" >&2
    fail=1
  elif [[ "${kind}" == inactive ]]; then
    echo "FAIL: ${dashboard} has the TTFT table's query ${value} hidden or not run as" \
      "TraceQL; it must be visible with queryType traceql to fill the table" >&2
    fail=1
  elif [[ "${kind}" == extra ]]; then
    echo "FAIL: ${dashboard} runs query ${value} beside the TTFT table's cut; its rows" \
      "would mix into the requests over the TTFT gate, so remove or hide it" >&2
    fail=1
  elif [[ "${kind}" == unshaped ]]; then
    echo "FAIL: ${dashboard} queries span.ttft_ms other than as { resource.service.name =" \
      "\"vllm\" && name = \"llm_request\" && span.ttft_ms > <ms> }, optionally piped to" \
      "select(), so its rows may not be vLLM's requests over the TTFT gate: ${value}" >&2
    fail=1
  elif [[ "${value}" != *.* ]]; then
    echo "FAIL: ${dashboard} cuts span.ttft_ms at the integer ${value}; ttft_ms is a" \
      "double and Tempo matches no double against an integer literal, so write ${value}.0" >&2
    fail=1
  elif [[ -z "${ttft_s}" ]] || ! awk -v ms="${value}" -v s="${ttft_s}" 'BEGIN { exit !(ms / 1000 == s) }'; then
    echo "FAIL: ${dashboard} cuts span.ttft_ms at ${value} ms but the Collector's slo_met" \
      "TTFT gate is ${ttft_s:-nothing} s (${collector_config}); make them equal" >&2
    fail=1
  fi
done <<<"${cuts}"

if [[ "${fail}" -ne 0 ]]; then
  exit 1
fi

echo "PASS: Collector slo_met gates (ttft ${ttft_s} s, tpot ${tpot_s} s) equal every bench config's goodput gates and every dashboard's ttft_ms threshold"
