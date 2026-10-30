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

DASHBOARD="${dashboard}" yq "${resolve_queries}" "${cases}" >"${tmp}/tests.yaml"
if ! output="$(promtool test rules "${tmp}/tests.yaml" 2>&1)"; then
  echo "FAIL: promtool test rules failed on ${cases#"${repo_root}/"}:" >&2
  echo "${output}" >&2
  exit 1
fi

echo "PASS: the Fleet health dashboard's queries compute every expected value in ${cases#"${repo_root}/"}"
