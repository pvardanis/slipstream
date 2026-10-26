#!/usr/bin/env bash
# Static goodput parity check: the Collector's slo_met gates
# (k8s/otel-collector-config.yaml, in seconds) must equal the goodput gates every
# bench config passes to `vllm bench serve` (["ttft:1000", "tpot:50"], in ms), so
# live and offline goodput cannot drift apart (ADR-0026). Reads both configs
# with yq; needs no running Collector.
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
collector_config="${OTEL_COLLECTOR_CONFIG:-${repo_root}/k8s/otel-collector-config.yaml}"
bench_configs_dir="${BENCH_CONFIGS_DIR:-${repo_root}/bench/configs}"

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

if [[ "${fail}" -ne 0 ]]; then
  exit 1
fi

echo "PASS: Collector slo_met gates (ttft ${ttft_s} s, tpot ${tpot_s} s) equal every bench config's goodput gates"
