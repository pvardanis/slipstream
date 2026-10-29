#!/usr/bin/env bash
# Static dashboard check: every dashboard file in terraform/grafana/dashboards must parse
# as JSON, and every metric its queries read must be in the remote-write allowlist
# (terraform/eks/remote-write-allowlist.yaml), since a series Prometheus does not send
# to Grafana Cloud leaves its panel showing "no data" (ADR-0026). Reads both with jq
# and yq; needs no cluster or Grafana.
# shellcheck disable=SC2016 # $ in the jq programs is jq's, not the shell's.
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
dashboards_dir="${DASHBOARDS_DIR:-${repo_root}/terraform/grafana/dashboards}"
allowlist="${REMOTE_WRITE_ALLOWLIST:-${repo_root}/terraform/eks/remote-write-allowlist.yaml}"

# Every "expr" anywhere in a dashboard, and those that are strings: the PromQL queries
# its panels run.
exprs='.. | objects | select(has("expr")) | .expr'
queries="${exprs} | strings"

# Removes PromQL's double-, single- and back-quoted strings from a query.
strip_strings='def strip_strings:
  gsub("\"([^\"\\\\]|\\\\.)*\"|'"'"'([^'"'"'\\\\]|\\\\.)*'"'"'|`[^`]*`"; "");'

# The metric names a dashboard's queries read, one per line. Strings, label matchers,
# range and subquery brackets and grouping label lists are dropped, then function and
# aggregation calls; the identifiers left, other than PromQL's keywords, are metric
# names. An identifier preceded by a digit, dot or $ is a duration unit, exponent or
# Grafana variable, not a metric.
metric_extraction="${strip_strings}"'
  ['"${queries}"'
    | strip_strings
    | gsub("\\{[^}]*\\}"; "")
    | gsub("\\[[^\\]]*\\]"; "")
    | gsub("\\b(by|without|on|ignoring|group_left|group_right)\\s*\\([^)]*\\)"; "")
    | gsub("[a-zA-Z_][a-zA-Z0-9_]*\\s*\\("; "(")
    | scan("(?<![0-9a-zA-Z_:.$])[a-zA-Z_:][a-zA-Z0-9_:]*")]
  | map(select(ascii_downcase
      | IN("and", "or", "unless", "bool", "offset", "on", "ignoring", "by", "without",
          "group_left", "group_right", "inf", "nan") | not))
  | unique[]'

# What in a dashboard names metrics the extraction above cannot read, one per line: an
# expr that is not a string, a query that mentions __name__, a Grafana variable followed
# by a label matcher or range (standing for the metric name), and a query variable.
unreadable_queries="${strip_strings}"'
  ('"${exprs}"' | select(type != "string")
    | "an expr that is not a string: \(tojson)"),
  ('"${queries}"' | select(test("__name__"))
    | "a query that mentions __name__: \(.)"),
  ('"${queries}"'
    | select(strip_strings | test("\\$(\\{[^}]*\\}|\\w+)\\s*[{\\[]|\\[\\["))
    | "a query whose metric name is a Grafana variable: \(.)"),
  (.templating.list[]? | select(.type == "query")
    | "a query variable: \(.name)")'

fail=0

allowlist_is_name_list='(.metrics | tag == "!!seq") and (.metrics | length > 0)
  and ([.metrics[] | select(tag != "!!str")] | length == 0)'
if ! yq -e "${allowlist_is_name_list}" "${allowlist}" >/dev/null ||
  ! allowed="$(yq '.metrics[]' "${allowlist}")"; then
  echo "FAIL: no metrics read from ${allowlist}; expected metrics to be a non-empty list of names" >&2
  exit 1
fi

dashboards="$(find "${dashboards_dir}" -name '*.json' | sort)"
if [[ -z "${dashboards}" ]]; then
  echo "FAIL: no dashboard files under ${dashboards_dir}; the check would pass vacuously" >&2
  exit 1
fi

checked=0
while IFS= read -r dashboard; do
  name="${dashboard#"${repo_root}/"}"
  if ! parse_error="$(jq empty "${dashboard}" 2>&1)"; then
    echo "FAIL: ${name} is not valid JSON: ${parse_error}" >&2
    fail=1
    continue
  fi
  if ! unreadable="$(jq -r "${unreadable_queries}" "${dashboard}" 2>&1)"; then
    echo "FAIL: ${name}: reading its queries failed: ${unreadable}" >&2
    fail=1
    continue
  fi
  if ! metrics="$(jq -r "${metric_extraction}" "${dashboard}" 2>&1)"; then
    echo "FAIL: ${name}: reading its metrics failed: ${metrics}" >&2
    fail=1
    continue
  fi
  while IFS= read -r reason; do
    [[ -n "${reason}" ]] || continue
    echo "FAIL: ${name} has ${reason}; this check cannot read which metrics it needs," \
      "so name each metric literally in a panel's expr or extend this check" >&2
    fail=1
  done <<<"${unreadable}"
  while IFS= read -r metric; do
    [[ -n "${metric}" ]] || continue
    checked=$((checked + 1))
    if ! grep -qxF "${metric}" <<<"${allowed}"; then
      echo "FAIL: ${name} queries ${metric}, which is not in ${allowlist#"${repo_root}/"};" \
        "add it to the allowlist's metrics or the panel shows no data" >&2
      fail=1
    fi
  done <<<"${metrics}"
done <<<"${dashboards}"

if [[ "${fail}" -ne 0 ]]; then
  exit 1
fi

if [[ "${checked}" -eq 0 ]]; then
  echo "FAIL: no metric queries found under ${dashboards_dir}; the check would pass vacuously" >&2
  exit 1
fi

echo "PASS: every dashboard parses, and all ${checked} metrics their queries read are in the remote-write allowlist"
