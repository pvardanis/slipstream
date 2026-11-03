#!/usr/bin/env bash
# Static check of ci.yml's path gating (ADR-0027). The image input filters in the
# `changes` job must name the same paths as the git pathspecs that derive each image's
# content tag: the bench_image filter against bench/image-tag.sh (image_sha), the
# orchestration_image filter against orchestration-image.yml's tag derivation. The
# docs-only `code` filter must match everything outside its excludes, every filter
# output the job reads must exist, and `ci-ok` must need every other job and run
# always(), so no gated job can drop out of the required check. Then runs the checks
# against mutated copies of ci.yml that must fail. Reads the workflows with yq and
# bench/image-tag.sh with awk; runs no git, build or network call.
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ci_workflow="${CI_WORKFLOW:-${repo_root}/.github/workflows/ci.yml}"
orchestration_workflow="${ORCHESTRATION_WORKFLOW:-${repo_root}/.github/workflows/orchestration-image.yml}"
bench_image_tag="${BENCH_IMAGE_TAG:-${repo_root}/bench/image-tag.sh}"

fail=0

# The changes job's paths-filter filters, as a YAML document.
filters() {
  yq '.jobs.changes.steps[] | select(.id == "filter") | .with.filters' "${ci_workflow}"
}

# The patterns of filter $1, one per line, sorted.
ci_filter() {
  filters | yq ".$1[]" | sort
}

# The pathspec of the `sha="$(git ... -- <paths>)"` (or sha_tag=) assignment in text on
# stdin, one path per line: every word after the `--` up to the closing `)"`, across
# backslash-continued lines. Prints nothing when the command ends without `)"`.
pathspec_of() {
  awk '
    /^[[:space:]]*(sha|sha_tag)="\$\(git / { grab = 1 }
    grab { cmd = cmd " " $0; if ($0 !~ /\\$/) { exit } }
    END {
      if (!sub(/.* -- /, "", cmd) || !sub(/\)".*/, "", cmd)) { exit }
      gsub(/\\/, "", cmd)
      n = split(cmd, words, " ")
      for (i = 1; i <= n; i++) { w = words[i]; gsub(/\047/, "", w); print w }
    }
  ' | sort
}

# Each git pathspec on stdin as the paths-filter pattern that matches the same files: a
# directory gains /**, :(exclude) becomes a !-negation, :(exclude,glob) keeps its glob.
as_filter_patterns() {
  local p q
  while read -r p; do
    case "${p}" in
    ':(exclude,glob)'*) echo "!${p#*)}" ;;
    ':(exclude)'*)
      q="${p#*)}"
      if [[ -d "${repo_root}/${q}" ]]; then echo "!${q}/**"; else echo "!${q}"; fi
      ;;
    *) if [[ -d "${repo_root}/${p}" ]]; then echo "${p}/**"; else echo "${p}"; fi ;;
    esac
  done | sort
}

# Fail when list $2 (named $1) is empty or differs from list $4 (named $3).
check_same() {
  if [[ -z "$2" ]] || [[ -z "$4" ]]; then
    echo "FAIL: $1 or $3 is empty — the list could not be read" >&2
    fail=1
  elif [[ "$2" != "$4" ]]; then
    echo "FAIL: $1 differs from $3:" >&2
    diff <(echo "$2") <(echo "$4") >&2 || true
    fail=1
  else
    echo "ok   $1 matches $3"
  fi
}

check_same "ci.yml bench_image filter" "$(ci_filter bench_image)" \
  "bench/image-tag.sh pathspec" "$(pathspec_of <"${bench_image_tag}" | as_filter_patterns)"

check_same "ci.yml orchestration_image filter" "$(ci_filter orchestration_image)" \
  "orchestration-image.yml pathspec" \
  "$(yq '.jobs.publish.steps[] | select(.id == "tags") | .run' "${orchestration_workflow}" |
    pathspec_of | as_filter_patterns)"

# Every pattern in the code filter other than its excludes: a narrower include would
# read a code change as docs-only and skip the tests.
check_same "ci.yml code filter includes" "$(filters | yq '.code[] | select(test("^!") | not)')" \
  "the match-all pattern" "**"

# The excludes in every filter depend on this quantifier (see ci.yml).
check_same "ci.yml paths-filter quantifier" \
  "$(yq '.jobs.changes.steps[] | select(.id == "filter") | .with["predicate-quantifier"]' "${ci_workflow}")" \
  "some-with-excludes" "some-with-excludes"

# A filter output the job reads but the filter does not define is empty on every run.
check_same "filter outputs the changes job reads" \
  "$(yq '.jobs.changes.outputs[]' "${ci_workflow}" | grep -o 'steps\.filter\.outputs\.[a-z_]*' |
    sed 's/^steps\.filter\.outputs\.//' | sort -u)" \
  "the filters defined" "$(filters | yq 'keys | .[]' | sort)"

# A job missing from ci-ok's needs can fail without blocking a merge; without always(),
# ci-ok skips when a needed job fails or skips, and a skipped required check passes.
check_same "ci-ok needs" "$(yq '.jobs["ci-ok"].needs[]' "${ci_workflow}" | sort)" \
  "every other ci.yml job" "$(yq '.jobs | keys | .[] | select(. != "ci-ok")' "${ci_workflow}" | sort)"
check_same "ci-ok if" "$(yq '.jobs["ci-ok"].if' "${ci_workflow}")" "always()" "always()"

# Run the checks above against a copy of ci.yml with sed expression $2 applied, and fail
# unless they fail: each case is a defect named $1 the checks exist to catch. A sed
# expression that no longer changes ci.yml fails too, so the case cannot pass vacuously.
check_mutation_fails() {
  local mutated
  mutated="$(mktemp)"
  sed "$2" "${ci_workflow}" >"${mutated}"
  if cmp -s "${ci_workflow}" "${mutated}"; then
    echo "FAIL: the mutation for $1 does not change ci.yml; update it to ci.yml's text" >&2
    fail=1
  elif CI_PATHS_PARITY_MUTATION=1 CI_WORKFLOW="${mutated}" bash "${BASH_SOURCE[0]}" >/dev/null 2>&1; then
    echo "FAIL: the checks pass on ci.yml with $1" >&2
    fail=1
  else
    echo "ok   the checks catch $1"
  fi
  rm -f "${mutated}"
}

if [[ -z "${CI_PATHS_PARITY_MUTATION:-}" ]]; then
  check_mutation_fails "model.yaml dropped from bench_image" "/bench_image:/,/orchestration_image:/{/'model.yaml'/d;}"
  check_mutation_fails "the bench/configs exclude dropped" "/'!bench\/configs\/\*\*'/d"
  check_mutation_fails "packages/** narrowed to packages" "s|'packages/\*\*'|'packages'|"
  check_mutation_fails "the match-all dropped from code" "/^              - '\*\*'$/d"
  check_mutation_fails "shell-tests dropped from ci-ok needs" "/^      - shell-tests$/d"
  check_mutation_fails "a misspelt filter output" "s/steps\.filter\.outputs\.code/steps.filter.outputs.codes/"
fi

exit "${fail}"
