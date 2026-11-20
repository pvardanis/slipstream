#!/usr/bin/env bash
# Steps shared by the promtool case tests, test/dashboard-promql-test.sh and
# test/serving-triggers-promql-test.sh, sourced by both: the tool check, a scratch directory
# removed on exit, running the cases, and judging a mutated query against them. The
# sourcing test sets repo_root and cases (the cases file) and defines write_tests, which
# writes the cases, with the queries from the file it is given, as promtool tests to
# ${tmp}/tests.yaml.
# shellcheck disable=SC2034,SC2154 # fail and mutations are read, and cases and repo_root
# set, by the sourcing test.

tmp="$(mktemp -d)"
trap 'rm -r "${tmp}"' EXIT

# Set by check_mutant_fails and the sourcing test's mutation checks: fail is 1 once a
# mutation check fails, and mutations counts the checks run.
fail=0
mutations=0

# Fails naming the first of the tools given that is not on PATH.
require_tools() {
  local tool
  for tool in "$@"; do
    if ! command -v "${tool}" >/dev/null; then
      echo "FAIL: ${tool} is not on PATH; this test needs $*, and 'mise install'" \
        "installs the ones mise.toml pins" >&2
      exit 1
    fi
  done
}

# Runs promtool over the tests write_tests wrote, printing its output.
run_tests() {
  promtool test rules "${tmp}/tests.yaml" 2>&1
}

# Runs the cases against the queries in file $1, and fails printing promtool's output
# when any case fails.
check_cases_pass() {
  write_tests "$1"
  local output
  if ! output="$(run_tests)"; then
    echo "FAIL: promtool test rules failed on ${cases#"${repo_root}/"}:" >&2
    echo "${output}" >&2
    exit 1
  fi
}

# Records a failed mutation check for the defect $3 whose pattern $2 does not match
# the query $1 names.
report_unmatched_mutation() {
  local query_name="$1" pattern="$2" defect="$3"
  echo "FAIL: ${query_name} no longer matches /${pattern}/, so the mutation for" \
    "${defect} cannot be applied; update it to the query's current text" >&2
  fail=1
}

# Checks the cases fail against the mutated queries in file $1, so a case catches the
# defect $2 names in the query $3 names: a case fails on the mutated query, by a value
# other than the one expected or by an error evaluating it. Records a failed check when
# the cases pass, when the mutated query does not parse, or when promtool fails on no
# case, as the cases then fail without catching anything.
check_mutant_fails() {
  local mutant="$1" defect="$2" query_name="$3"
  write_tests "${mutant}"
  local mutant_output
  if mutant_output="$(run_tests)"; then
    echo "FAIL: the cases still pass with ${defect} in ${query_name}; add a case to" \
      "${cases#"${repo_root}/"} that catches it. promtool's output:" >&2
    echo "${mutant_output}" >&2
    fail=1
  elif grep -q 'parse error:' <<<"${mutant_output}"; then
    echo "FAIL: the mutation for ${defect} leaves ${query_name} unparseable, so the" \
      "cases fail without catching it; fix its replacement. promtool's output:" >&2
    echo "${mutant_output}" >&2
    fail=1
  elif ! grep -q '^ *expr: .*, time: ' <<<"${mutant_output}"; then
    echo "FAIL: promtool fails with ${defect} in ${query_name}, but on no case, so the" \
      "cases catch nothing; promtool's output:" >&2
    echo "${mutant_output}" >&2
    fail=1
  fi
}
