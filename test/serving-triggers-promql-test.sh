#!/usr/bin/env bash
# Serving fleet trigger check: runs the promtool unit tests in
# test/serving-triggers-promql-cases.yaml against the Prometheus trigger queries in
# k8s/serving/scaled-object.yaml (ADR-0031). The cases name each query by its trigger's
# name, so the queries are read from the ScaledObject, never copied. Each trigger's
# metric type, threshold and activationThreshold must be the ones the cases are worked
# against, and mutated copies of each query must fail the cases. Needs yq and promtool;
# needs no cluster.
# shellcheck disable=SC2016 # $ in the yq programs is theirs, not the shell's.
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
scaled_object="${repo_root}/k8s/serving/scaled-object.yaml"
cases="${repo_root}/test/serving-triggers-promql-cases.yaml"

# shellcheck source=test/lib/promql-cases.sh
source "${repo_root}/test/lib/promql-cases.sh"
require_tools yq promtool

# Fails unless the trigger named $1 sets its field $2 to $3, the value the cases'
# replica counts are worked against, as $4 says.
check_setting() {
  local trigger="$1" field="$2" expected="$3" why="$4"
  local actual
  actual="$(TRIGGER="${trigger}" yq ".spec.triggers[] | select(.name == strenv(TRIGGER)) | .${field}" \
    "${scaled_object}")"
  if [[ "${actual}" != "${expected}" ]]; then
    echo "FAIL: the ${trigger} trigger's ${field} is '${actual}', not '${expected}': ${why}" >&2
    exit 1
  fi
}

check_setting wake metadata.activationThreshold 0 'it reads above 0 exactly when requests arrive'
check_setting wake metricType AverageValue 'reading 1, it asks for one replica'
check_setting wake metadata.threshold 1 'reading 1, it asks for one replica'
check_setting load metadata.activationThreshold 0 'it reads above 0 while any request is running or waiting'
check_setting load metricType AverageValue 'the HPA averages the requests over the replicas'
check_setting load metadata.threshold 6 'the target is 6 requests per replica'
check_setting queue metadata.activationThreshold 0 'it reads 1 when it fires and 0 otherwise'
check_setting queue metricType Value 'firing, it asks for twice the current replicas'
check_setting queue metadata.threshold 0.5 'firing, it asks for twice the current replicas'

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

# Replaces the regex $2 with $3 in the trigger $1's query, in a copy of the
# ScaledObject, and checks the cases fail against it, so a case catches the defect $4
# names.
check_mutation_fails() {
  local trigger="$1" pattern="$2" replacement="$3" defect="$4"
  local query='.spec.triggers[] | select(.name == strenv(TRIGGER)) | .metadata.query'
  mutations=$((mutations + 1))
  if [[ "$(TRIGGER="${trigger}" RE="${pattern}" yq "${query} | test(strenv(RE))" "${scaled_object}")" != "true" ]]; then
    report_unmatched_mutation "the ${trigger} query" "${pattern}" "${defect}"
    return
  fi
  TRIGGER="${trigger}" RE="${pattern}" TO="${replacement}" \
    yq "(${query}) |= sub(strenv(RE); strenv(TO))" "${scaled_object}" >"${tmp}/mutant.yaml"
  check_mutant_fails "${tmp}/mutant.yaml" "${defect}" "the ${trigger} query"
}

check_mutation_fails wake ',envoy_http_conn_manager_prefix="ingress"' '' \
  'the metrics listener counted, so Prometheus scrapes keep the fleet awake'
check_mutation_fails wake 'namespace="serving",' '' 'requests to Envoys outside serving counted'
check_mutation_fails wake 'sum\(rate\((.*)\[1m\]\)\)' 'sum(${1})' \
  'the request count, not its rate, which stays above 0 once any request arrived'
check_mutation_fails wake ' > bool 0$' '' \
  'the request rate itself, which asks for a replica per request per second'
check_mutation_fails load ' \+ vllm:num_requests_waiting\{namespace="serving"\}' '' \
  'waiting requests left out, so a replica with only a queue reads idle'
check_mutation_fails load '\{namespace="serving"\}' '' 'the bench Engine counted'
check_mutation_fails load '^sum\(' 'avg(' \
  'requests averaged over Engines, which the HPA then divides by the replicas again'
check_mutation_fails queue ' or vector\(0\)' '' \
  'a step with no Engine series skipped, so an Engine up under a minute fires it'
check_mutation_fails queue ' > bool 0\)' ')' \
  'the waiting count, not whether any request waits, which asks for more than twice'
check_mutation_fails queue '\[1m:15s\]' '[30s:15s]' \
  'half a minute of waiting firing it'

if [[ "${fail}" -ne 0 ]]; then
  exit 1
fi

echo "PASS: the serving fleet's triggers compute every expected value in" \
  "${cases#"${repo_root}/"}, and all ${mutations} mutations of them fail it"
