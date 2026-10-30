#!/usr/bin/env bash
# Fleet health dashboard PromQL check: runs the promtool unit tests in
# test/dashboard-promql-cases.yaml against the queries in
# terraform/grafana/dashboards/fleet-health.json. The cases name each query by panel id
# and refId, or by annotation name, so the queries are read from the dashboard, never
# copied. Needs yq and promtool; needs no cluster or Grafana.
# shellcheck disable=SC2016 # $ in the yq programs is yq's, not the shell's.
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
dashboard="${repo_root}/terraform/grafana/dashboards/fleet-health.json"
cases="${repo_root}/test/dashboard-promql-cases.yaml"

tmp="$(mktemp -d)"
trap 'rm -r "${tmp}"' EXIT

# Fills in each case's expr from the dashboard at $DASHBOARD, with $__range set to 60m.
resolve_queries='load(strenv(DASHBOARD)) as $d
  | (.tests[].promql_expr_test[] | select(has("panel"))) |= (
      . as $t
      | .expr = ([$d.panels[] | select(.id == $t.panel)
          | .targets[] | select(.refId == $t.ref) | .expr] | .[0])
      | del(.panel, .ref))
  | (.tests[].promql_expr_test[] | select(has("annotation"))) |= (
      . as $t
      | .expr = ([$d.annotations.list[] | select(.name == $t.annotation) | .expr] | .[0])
      | del(.annotation))
  | (.tests[].promql_expr_test[].expr) |= sub("\$__range"; "60m")'

# Runs the cases against the queries in the dashboard file $1, printing promtool's output.
run_cases() {
  DASHBOARD="$1" yq "${resolve_queries}" "${cases}" >"${tmp}/tests.yaml" &&
    promtool test rules "${tmp}/tests.yaml" 2>&1
}

if ! output="$(run_cases "${dashboard}")"; then
  echo "FAIL: promtool test rules failed on ${cases#"${repo_root}/"}:" >&2
  echo "${output}" >&2
  exit 1
fi

fail=0
mutations=0

# Replaces the regex $3 with $4 in panel $1's refId $2 query, in a copy of the dashboard,
# and checks the cases fail against it, so a case catches the defect $5 names.
check_mutation_fails() {
  local panel="$1" ref="$2" pattern="$3" replacement="$4" defect="$5"
  local query='.panels[] | select(.id == $id) | .targets[] | select(.refId == $ref) | .expr'
  local args=(--argjson id "${panel}" --arg ref "${ref}" --arg re "${pattern}")
  mutations=$((mutations + 1))
  if ! jq -e "${args[@]}" "[${query} | test(\$re)] == [true]" "${dashboard}" >/dev/null; then
    echo "FAIL: panel ${panel} ${ref}'s query no longer matches /${pattern}/, so the" \
      "mutation for ${defect} cannot be applied; update it to the query's current text" >&2
    fail=1
    return
  fi
  jq "${args[@]}" --arg to "${replacement}" "(${query}) |= sub(\$re; \$to)" \
    "${dashboard}" >"${tmp}/mutant.json"
  if run_cases "${tmp}/mutant.json" >/dev/null; then
    echo "FAIL: the cases still pass with ${defect} in panel ${panel} ${ref}; add a case" \
      "to ${cases#"${repo_root}/"} that catches it" >&2
    fail=1
  fi
}

# The scoreboard panels: $ per 1M tokens and Tokens/s per $/h.
price_join='on \(instance_type, capacity_type, zone\)'
for panel in 17 18; do
  check_mutation_fails "${panel}" A '/ 60\)' '/ 30)' 'the spend subquery divided by 30, not 60'
  check_mutation_fails "${panel}" A "${price_join}" 'on (capacity_type, zone)' \
    'the price join ignoring instance_type'
  check_mutation_fails "${panel}" A "${price_join}" 'on (instance_type, zone)' \
    'the price join ignoring capacity_type'
  check_mutation_fails "${panel}" A "${price_join}" 'on (instance_type, capacity_type)' \
    'the price join ignoring zone'
done

if [[ "${fail}" -ne 0 ]]; then
  exit 1
fi

echo "PASS: the Fleet health dashboard's queries compute every expected value in" \
  "${cases#"${repo_root}/"}, and all ${mutations} mutations of them fail it"
