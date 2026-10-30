#!/usr/bin/env bash
# Fleet health dashboard PromQL check: runs the promtool unit tests in
# test/dashboard-promql-cases.yaml against the queries in
# terraform/grafana/dashboards/fleet-health.json. The cases name each query by panel id
# and refId, or by annotation name, so the queries are read from the dashboard, never
# copied. PROMQL_CASES points it at another cases file. Needs yq, jq and promtool; needs
# no cluster or Grafana.
# shellcheck disable=SC2016 # $ in the yq and jq programs is theirs, not the shell's.
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
dashboard="${repo_root}/terraform/grafana/dashboards/fleet-health.json"
cases="${PROMQL_CASES:-${repo_root}/test/dashboard-promql-cases.yaml}"

for tool in yq jq promtool; do
  if ! command -v "${tool}" >/dev/null; then
    echo "FAIL: ${tool} is not on PATH; this test needs yq, jq and promtool, and" \
      "'mise install' installs the ones mise.toml pins" >&2
    exit 1
  fi
done

tmp="$(mktemp -d)"
trap 'rm -r "${tmp}"' EXIT

# Fills in each case's expr from the dashboard at $DASHBOARD, with $__range set to 60m,
# keeping the panel, ref or annotation it names.
resolve_queries='load(strenv(DASHBOARD)) as $d
  | (.tests[].promql_expr_test[] | select(has("panel"))) |= (
      . as $t
      | .expr = ([$d.panels[] | select(.id == $t.panel)
          | .targets[] | select(.refId == $t.ref) | .expr] | .[0]))
  | (.tests[].promql_expr_test[] | select(has("annotation"))) |= (
      . as $t
      | .expr = ([$d.annotations.list[] | select(.name == $t.annotation) | .expr] | .[0]))
  | (.tests[].promql_expr_test[].expr | select(tag == "!!str")) |= sub("\$__range"; "60m")'

# Drops the names resolve_queries keeps, which promtool does not accept.
drop_query_names='del(.tests[].promql_expr_test[] | (.panel, .ref, .annotation))'

# Each resolved case that names no panel or annotation, or whose query is missing, empty
# or still holds a $ variable, one per line (a jq program, as yq has no if-then-else).
unresolved_queries='.tests[].promql_expr_test[]
  | (if has("panel") then "panel \(.panel) refId \(.ref)"
     elif has("annotation") then "annotation \(.annotation)"
     else null end) as $name
  | if $name == null then
      "a case at eval_time \(.eval_time) names no panel or annotation; name the dashboard"
      + " query it tests, so its expr is read from the dashboard, not copied"
    elif (.expr // "") == "" then
      "\($name) names no query in the dashboard, or an empty one; name a panel id and"
      + " refId, or an annotation, that the dashboard has"
    elif (.expr | test("\\$")) then
      "\($name) still holds a $ variable after $__range is set: \(.expr); set that"
      + " variable in this script, or test a query without it"
    else empty end'

DASHBOARD="${dashboard}" yq -o json "${resolve_queries}" "${cases}" >"${tmp}/resolved.json"
unresolved="$(jq -r "${unresolved_queries}" "${tmp}/resolved.json")"
if [[ -n "${unresolved}" ]]; then
  while IFS= read -r reason; do
    echo "FAIL: in ${cases#"${repo_root}/"}, ${reason}" >&2
  done <<<"${unresolved}"
  exit 1
fi

# Writes the cases, with the queries from the dashboard file $1, as promtool tests.
write_tests() {
  DASHBOARD="$1" yq "${resolve_queries} | ${drop_query_names}" "${cases}" >"${tmp}/tests.yaml"
}

# Runs promtool over the tests write_tests wrote, printing its output.
run_tests() {
  promtool test rules "${tmp}/tests.yaml" 2>&1
}

write_tests "${dashboard}"
if ! output="$(run_tests)"; then
  echo "FAIL: promtool test rules failed on ${cases#"${repo_root}/"}:" >&2
  echo "${output}" >&2
  exit 1
fi

fail=0
mutations=0

# Replaces the regex $3 with $4 in panel $1's refId $2 query, in a copy of the dashboard,
# and checks the cases fail against it, so a case catches the defect $5 names. Fails when
# $3 no longer matches the query, or when the mutated query does not parse, as the cases
# then fail without catching anything.
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
  write_tests "${tmp}/mutant.json"
  local mutant_output
  if mutant_output="$(run_tests)"; then
    echo "FAIL: the cases still pass with ${defect} in panel ${panel} ${ref}; add a case" \
      "to ${cases#"${repo_root}/"} that catches it. promtool's output:" >&2
    echo "${mutant_output}" >&2
    fail=1
  elif grep -q 'parse error:' <<<"${mutant_output}"; then
    echo "FAIL: the mutation for ${defect} leaves panel ${panel} ${ref}'s query unparseable," \
      "so the cases fail without catching it; fix its replacement. promtool's output:" >&2
    echo "${mutant_output}" >&2
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
check_mutation_fails 17 A ' > 0\) \* 1e6$' ') * 1e6' 'no > 0 filter on the token count'

# The Karpenter panels' counters, which Karpenter creates at their first increment: the
# branch that counts a series born inside the range.
born_in_range_branch=' or \(last_over_time\([^)]*\) unless [^)]*\)'
check_mutation_fails 14 B "${born_in_range_branch}" '' 'no branch for buckets born in range'
check_mutation_fails 15 A "${born_in_range_branch}" '' 'no branch for disruption reasons born in range'
for ref in A B; do
  check_mutation_fails 14 "${ref}" '\) >= 0$' ')' 'no >= 0 filter on the quantile'
done

if [[ "${fail}" -ne 0 ]]; then
  exit 1
fi

echo "PASS: the Fleet health dashboard's queries compute every expected value in" \
  "${cases#"${repo_root}/"}, and all ${mutations} mutations of them fail it"
