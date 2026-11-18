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

for tool in yq promtool; do
  if ! command -v "${tool}" >/dev/null; then
    echo "FAIL: ${tool} is not on PATH; this test needs yq and promtool, and" \
      "'mise install' installs the ones mise.toml pins" >&2
    exit 1
  fi
done

tmp="$(mktemp -d)"
trap 'rm -r "${tmp}"' EXIT

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
# and fails naming a case whose trigger has no query.
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

# Runs promtool over the tests write_tests wrote, printing its output.
run_tests() {
  promtool test rules "${tmp}/tests.yaml" 2>&1
}

write_tests "${scaled_object}"
if ! output="$(run_tests)"; then
  echo "FAIL: promtool test rules failed on ${cases#"${repo_root}/"}:" >&2
  echo "${output}" >&2
  exit 1
fi

fail=0
mutations=0

# Replaces the regex $1 with $2 in the wake trigger's query, in a copy of the
# ScaledObject, and checks the cases fail against it, so a case catches the defect $3
# names. Fails when $1 no longer matches the query, or when the mutated query does not
# parse, as the cases then fail without catching anything.
check_mutation_fails() {
  local pattern="$1" replacement="$2" defect="$3"
  local query='.spec.triggers[] | select(.name == "wake") | .metadata.query'
  mutations=$((mutations + 1))
  if [[ "$(RE="${pattern}" yq "${query} | test(strenv(RE))" "${scaled_object}")" != "true" ]]; then
    echo "FAIL: the wake query no longer matches /${pattern}/, so the mutation for" \
      "${defect} cannot be applied; update it to the query's current text" >&2
    fail=1
    return
  fi
  RE="${pattern}" TO="${replacement}" yq "(${query}) |= sub(strenv(RE); strenv(TO))" \
    "${scaled_object}" >"${tmp}/mutant.yaml"
  write_tests "${tmp}/mutant.yaml"
  local mutant_output
  if mutant_output="$(run_tests)"; then
    echo "FAIL: the cases still pass with ${defect}; add a case to" \
      "${cases#"${repo_root}/"} that catches it. promtool's output:" >&2
    echo "${mutant_output}" >&2
    fail=1
  elif grep -q 'parse error:' <<<"${mutant_output}"; then
    echo "FAIL: the mutation for ${defect} leaves the wake query unparseable, so the" \
      "cases fail without catching it; fix its replacement. promtool's output:" >&2
    echo "${mutant_output}" >&2
    fail=1
  fi
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
