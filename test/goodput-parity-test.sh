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

# The Collector's gate $1 (ttft or tpot) in seconds, from its
# set(span.cache["<gate>_gate_s"], <value>) statement. Empty when absent.
collector_gate_s() {
  yq '.processors.transform.trace_statements[].statements[]' "${collector_config}" |
    sed -n "s/^set(span.cache\[\"$1_gate_s\"\], \([0-9.]*\))\$/\1/p"
}

# Every "<config>: <gate>:<ms>" goodput entry across the bench configs, wherever
# the goodput list sits in the config (top level or under load). find exits
# non-zero when yq fails on any config.
bench_gates() {
  find "${bench_configs_dir}" -name '*.yaml' -exec yq --no-doc \
    '.. | select(tag == "!!map") | select(has("goodput")) | .goodput[] | filename + ": " + .' {} + |
    sed "s|^${repo_root}/||" | sort
}

ttft_s="$(collector_gate_s ttft)"
tpot_s="$(collector_gate_s tpot)"
for gate in ttft tpot; do
  value_var="${gate}_s"
  if [[ -z "${!value_var}" ]]; then
    echo "FAIL: no ${gate} gate in ${collector_config}; expected a statement" \
      "set(span.cache[\"${gate}_gate_s\"], <seconds>) in the transform processor" >&2
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

if [[ "${fail}" -ne 0 ]]; then
  exit 1
fi

echo "PASS: Collector slo_met gates (ttft ${ttft_s} s, tpot ${tpot_s} s) equal every bench config's goodput gates"
