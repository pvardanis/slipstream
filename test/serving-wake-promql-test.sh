#!/usr/bin/env bash
# Serving fleet wake trigger check: runs the promtool unit tests in
# test/serving-wake-promql-cases.yaml against the Prometheus trigger queries in
# k8s/serving/scaled-object.yaml (ADR-0031). The cases name each query by its trigger's
# name, so the queries are read from the ScaledObject, never copied. The wake trigger
# must activate above 0, the value the cases are worked against, and mutated copies of
# its query must fail the cases. Needs yq and promtool; needs no cluster.
# shellcheck disable=SC2016 # $ in the yq programs is theirs, not the shell's.
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
scaled_object="${repo_root}/k8s/serving/scaled-object.yaml"
cases="${repo_root}/test/serving-wake-promql-cases.yaml"

# shellcheck source=test/lib/promql-cases.sh
source "${repo_root}/test/lib/promql-cases.sh"
require_tools yq promtool

# KEDA activates a trigger when its value is above activationThreshold; the cases show
# the query above 0 under load and at 0 or empty when idle, so the threshold must be 0.
activation="$(yq '.spec.triggers[] | select(.name == "wake") | .metadata.activationThreshold' "${scaled_object}")"
if [[ "${activation}" != "0" ]]; then
  echo "FAIL: the wake trigger's activationThreshold is '${activation}', not \"0\": the" \
    "cases show the query above 0 exactly when requests arrive" >&2
  exit 1
fi

# Fills in each case's expr from the ScaledObject at $SCALED_OBJECT by its trigger name,
# and drops the name, which promtool does not accept.
resolve_queries='load(strenv(SCALED_OBJECT)) as $s
  | (.tests[].promql_expr_test[]) |= (
      . as $t
      | .expr = ([$s.spec.triggers[] | select(.type == "prometheus" and .name == $t.trigger)
          | .metadata.query] | .[0])
      | del(.trigger))'

# Writes the cases, with the queries from the ScaledObject file $1, as promtool tests,
# and fails counting the cases whose trigger has no query.
write_tests() {
  SCALED_OBJECT="$1" yq "${resolve_queries}" "${cases}" >"${tmp}/tests.yaml"
  local missing
  missing="$(yq '[.tests[].promql_expr_test[] | select((.expr // "") == "")] | length' "${tmp}/tests.yaml")"
  if [[ "${missing}" != "0" ]]; then
    echo "FAIL: ${missing} case(s) in ${cases#"${repo_root}/"} name a trigger with no" \
      "Prometheus query in ${scaled_object#"${repo_root}/"}" >&2
    return 1
  fi
}

check_cases_pass "${scaled_object}"

# Replaces the regex $1 with $2 in the wake trigger's query, in a copy of the
# ScaledObject, and checks the cases fail against it, so a case catches the defect $3
# names.
check_mutation_fails() {
  local pattern="$1" replacement="$2" defect="$3"
  local query='.spec.triggers[] | select(.name == "wake") | .metadata.query'
  mutations=$((mutations + 1))
  if [[ "$(RE="${pattern}" yq "${query} | test(strenv(RE))" "${scaled_object}")" != "true" ]]; then
    report_unmatched_mutation "the wake query" "${pattern}" "${defect}"
    return
  fi
  RE="${pattern}" TO="${replacement}" yq "(${query}) |= sub(strenv(RE); strenv(TO))" \
    "${scaled_object}" >"${tmp}/mutant.yaml"
  check_mutant_fails "${tmp}/mutant.yaml" "${defect}" "the wake query"
}

check_mutation_fails ',envoy_http_conn_manager_prefix="ingress"' '' \
  'the metrics listener counted, so Prometheus scrapes keep the fleet awake'
check_mutation_fails 'namespace="serving",' '' 'requests to Envoys outside serving counted'
check_mutation_fails 'sum\(rate\((.*)\[1m\]\)\)' 'sum(${1})' \
  'the request count, not its rate, which stays above 0 once any request arrived'

if [[ "${fail}" -ne 0 ]]; then
  exit 1
fi

echo "PASS: the serving fleet's wake trigger computes every expected value in" \
  "${cases#"${repo_root}/"}, and all ${mutations} mutations of it fail it"
