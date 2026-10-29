#!/usr/bin/env bash
# Static dashboard check: every dashboard file in terraform/grafana/dashboards must parse
# as JSON, and every metric its queries read must be in the remote-write allowlist
# (terraform/eks/remote-write-allowlist.yaml), since a series Prometheus does not send
# to Grafana Cloud leaves its panel showing "no data" (ADR-0026). Reads both with jq
# and yq; needs no cluster or Grafana.
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
dashboards_dir="${DASHBOARDS_DIR:-${repo_root}/terraform/grafana/dashboards}"
allowlist="${REMOTE_WRITE_ALLOWLIST:-${repo_root}/terraform/eks/remote-write-allowlist.yaml}"

# Every "expr" anywhere in a dashboard: the PromQL queries its panels run.
queries='.. | objects | select(has("expr")) | .expr | strings'

# The metric names a dashboard's queries read, one per line. Strings, label matchers,
# range and subquery brackets and grouping label lists are dropped, then function and
# aggregation calls; the identifiers left, other than PromQL's keywords, are metric names. An identifier preceded by a digit,
# dot or $ is a duration unit, exponent or Grafana variable, not a metric.
# shellcheck disable=SC2016 # $ in the jq program is jq's, not the shell's.
metric_extraction='
  ['"${queries}"'
    | gsub("\"([^\"\\\\]|\\\\.)*\"|'"'"'([^'"'"'\\\\]|\\\\.)*'"'"'|`[^`]*`"; "")
    | gsub("\\{[^}]*\\}"; "")
    | gsub("\\[[^\\]]*\\]"; "")
    | gsub("\\b(by|without|on|ignoring|group_left|group_right)\\s*\\([^)]*\\)"; "")
    | gsub("[a-zA-Z_][a-zA-Z0-9_]*\\s*\\("; "(")
    | scan("(?<![0-9a-zA-Z_:.$])[a-zA-Z_:][a-zA-Z0-9_:]*")]
  | map(select(ascii_downcase
      | IN("and", "or", "unless", "bool", "offset", "on", "ignoring", "by", "without",
          "group_left", "group_right", "inf", "nan") | not))
  | unique[]'

# Every query that selects its metric by a __name__ matcher, which the extraction above
# cannot read.
name_matcher_queries="${queries}"' | select(test("__name__"))'

fail=0

if ! allowed="$(yq '.metrics[]' "${allowlist}")" || [[ -z "${allowed}" ]]; then
  echo "FAIL: no metrics read from ${allowlist}; expected a non-empty metrics list" >&2
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
  while IFS= read -r query; do
    [[ -n "${query}" ]] || continue
    echo "FAIL: ${name} selects a metric by __name__, which this check cannot read;" \
      "name the metric before its braces: ${query}" >&2
    fail=1
  done <<<"$(jq -r "${name_matcher_queries}" "${dashboard}")"
  while IFS= read -r metric; do
    [[ -n "${metric}" ]] || continue
    checked=$((checked + 1))
    if ! grep -qxF "${metric}" <<<"${allowed}"; then
      echo "FAIL: ${name} queries ${metric}, which is not in ${allowlist#"${repo_root}/"};" \
        "add it to the allowlist's metrics or the panel shows no data" >&2
      fail=1
    fi
  done <<<"$(jq -r "${metric_extraction}" "${dashboard}")"
done <<<"${dashboards}"

if [[ "${fail}" -ne 0 ]]; then
  exit 1
fi

if [[ "${checked}" -eq 0 ]]; then
  echo "FAIL: no metric queries found under ${dashboards_dir}; the check would pass vacuously" >&2
  exit 1
fi

echo "PASS: every dashboard parses, and all ${checked} metrics their queries read are in the remote-write allowlist"
