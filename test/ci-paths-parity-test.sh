#!/usr/bin/env bash
# Static check of ci.yml's path gating (ADR-0027). The image input filters in the
# `changes` job must name the same paths as the git pathspecs that derive each image's
# content tag: the bench_image filter against bench/image-tag.sh (image_sha), the
# orchestration_image filter against orchestration-image.yml's tag derivation. The
# docs-only `code` filter must match everything outside its excludes, and its excludes
# must equal the push paths-ignore of both publish workflows. scan_config must name
# .trivyignore and the scan workflow ci.yml calls, every filter output the job reads
# must exist and fail closed, and `ci-ok` must need every other job and run always(), so
# no gated job can drop out of the required check. Then runs the checks against mutated
# copies of the files that must fail. Reads the workflows with yq and bench/image-tag.sh
# with awk; runs no git, build or network call.
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ci_workflow="${CI_WORKFLOW:-${repo_root}/.github/workflows/ci.yml}"
orchestration_workflow="${ORCHESTRATION_WORKFLOW:-${repo_root}/.github/workflows/orchestration-image.yml}"
bench_image_tag="${BENCH_IMAGE_TAG:-${repo_root}/bench/image-tag.sh}"
bench_workflow="${BENCH_WORKFLOW:-${repo_root}/.github/workflows/bench-image.yml}"

fail=0

# The changes job's paths-filter filters, as a YAML document.
filters() {
  yq '.jobs.changes.steps[] | select(.id == "filter") | .with.filters' "${ci_workflow}"
}

# The patterns of filter $1, one per line, sorted.
ci_filter() {
  filters | yq ".$1[]" | sort
}

# The docs denylist in the changes job's code filter: its !-negated patterns, unnegated.
ci_docs_denylist() {
  filters | yq '.code[] | select(test("^!")) | sub("^!", "")' | sort
}

# The push trigger's paths-ignore in workflow $1, one pattern per line.
push_paths_ignore() {
  yq '.on.push.paths-ignore[]' "$1" | sort
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

# A docs location the gate skips must be one the publish workflows skip, and no other.
check_same "ci.yml docs denylist" "$(ci_docs_denylist)" \
  "bench-image.yml push paths-ignore" "$(push_paths_ignore "${bench_workflow}")"

check_same "ci.yml docs denylist" "$(ci_docs_denylist)" \
  "orchestration-image.yml push paths-ignore" "$(push_paths_ignore "${orchestration_workflow}")"

# The orchestration scan runs on a change to its waivers, to the workflow ci.yml
# actually calls, or to a repo script that workflow runs against the image, or a renamed
# workflow would stop triggering its own scan and an edited check would go unrun.
scan_workflow="$(yq '.jobs.orchestration-image-scan.uses | sub("^\\./", "")' "${ci_workflow}")"
check_same "ci.yml scan_config filter" "$(ci_filter scan_config)" \
  ".trivyignore, the scan workflow ci.yml calls and the scripts it runs" \
  "$({
    echo .trivyignore
    echo "${scan_workflow}"
    yq '.jobs[].steps[].run // ""' "${repo_root}/${scan_workflow}" |
      { grep -o 'orchestration/[A-Za-z0-9_.-]*\.sh' || true; }
  } | sort -u)"

# Every output must fail closed, so a missing filter result runs the tests: docs_only
# true only on the filter's explicit 'false', the image outputs false only on it.
changes_outputs="$(yq '.jobs.changes.outputs | to_entries | .[] | .key + "=" + .value' "${ci_workflow}")"
fail_open_outputs="$(awk -F= '
  /\047true\047/ { print $1; next }
  $1 == "docs_only" && !/== \047false\047/ { print $1; next }
  $1 != "docs_only" && !/!= \047false\047/ { print $1 }
' <<<"${changes_outputs}")"
if [[ -z "${changes_outputs}" ]] || [[ -n "${fail_open_outputs}" ]]; then
  echo "FAIL: changes job outputs must read the filter's explicit 'false'; failing open:" \
    "${fail_open_outputs:-(no outputs read)}" >&2
  fail=1
else
  echo "ok   changes job outputs fail closed"
fi

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

# Run the checks above with the file in env var $2 replaced by a copy with sed
# expression $3 applied, and fail unless the check labelled $4 fails: each case is a
# defect named $1 that check exists to catch. A sed expression that no longer changes the
# file fails too, as does a run that fails some other check, so no case passes vacuously.
check_mutation_fails() {
  local defect="$1" env_var="$2" expression="$3" label="$4" original mutated output
  case "${env_var}" in
  CI_WORKFLOW) original="${ci_workflow}" ;;
  BENCH_IMAGE_TAG) original="${bench_image_tag}" ;;
  ORCHESTRATION_WORKFLOW) original="${orchestration_workflow}" ;;
  BENCH_WORKFLOW) original="${bench_workflow}" ;;
  *)
    echo "FAIL: no file is read from ${env_var}" >&2
    fail=1
    return
    ;;
  esac
  mutated="$(mktemp)"
  sed "${expression}" "${original}" >"${mutated}"
  if cmp -s "${original}" "${mutated}"; then
    echo "FAIL: the mutation for ${defect} does not change ${original}; update it to the file's text" >&2
    fail=1
  elif output="$(env CI_PATHS_PARITY_MUTATION=1 "${env_var}=${mutated}" bash "${BASH_SOURCE[0]}" 2>&1)"; then
    echo "FAIL: the checks pass with ${defect}" >&2
    fail=1
  elif ! grep -qF "FAIL: ${label}" <<<"${output}"; then
    echo "FAIL: with ${defect}, the '${label}' check passes; the run failed on:" >&2
    grep -F 'FAIL:' <<<"${output}" >&2 || echo "${output}" >&2
    fail=1
  else
    echo "ok   the checks catch ${defect}"
  fi
  rm -f "${mutated}"
}

if [[ -z "${CI_PATHS_PARITY_MUTATION:-}" ]]; then
  check_mutation_fails "model.yaml dropped from bench_image" CI_WORKFLOW \
    "/bench_image:/,/orchestration_image:/{/'model.yaml'/d;}" "ci.yml bench_image filter"
  check_mutation_fails "the bench/configs exclude dropped" CI_WORKFLOW \
    "/'!bench\/configs\/\*\*'/d" "ci.yml bench_image filter"
  check_mutation_fails "packages/** narrowed to packages" CI_WORKFLOW \
    "s|'packages/\*\*'|'packages'|" "ci.yml bench_image filter"
  check_mutation_fails "model.yaml dropped from image_sha" BENCH_IMAGE_TAG \
    "s/ pyproject.toml model.yaml / pyproject.toml /" "ci.yml bench_image filter"
  check_mutation_fails "a broken continuation in image_sha" BENCH_IMAGE_TAG \
    's/ .dockerignore \\$/ .dockerignore/' "ci.yml bench_image filter"
  check_mutation_fails "uv.lock dropped from the orchestration pathspec" ORCHESTRATION_WORKFLOW \
    "s/ uv.lock / /" "ci.yml orchestration_image filter"
  check_mutation_fails "the match-all dropped from code" CI_WORKFLOW \
    "/^              - '\*\*'$/d" "ci.yml code filter includes"
  check_mutation_fails "docs_only failing open" CI_WORKFLOW \
    "s/outputs\.code == 'false'/outputs.code != 'true'/" "changes job outputs"
  check_mutation_fails "bench_image failing open" CI_WORKFLOW \
    "s/outputs\.bench_image != 'false'/outputs.bench_image == 'true'/" "changes job outputs"
  check_mutation_fails "shell-tests dropped from ci-ok needs" CI_WORKFLOW \
    "/^      - shell-tests$/d" "ci-ok needs"
  check_mutation_fails ".trivyignore dropped from scan_config" CI_WORKFLOW \
    "/^              - '\.trivyignore'$/d" "ci.yml scan_config filter"
  check_mutation_fails "the terraform check dropped from scan_config" CI_WORKFLOW \
    "/^              - 'orchestration\/orchestration-image-terraform-offline-check\.sh'$/d" "ci.yml scan_config filter"
  check_mutation_fails "a misspelt filter output" CI_WORKFLOW \
    "s/steps\.filter\.outputs\.code/steps.filter.outputs.codes/" "filter outputs the changes job reads"
  check_mutation_fails "reports/** dropped from the docs denylist" CI_WORKFLOW \
    "/^              - '!reports\/\*\*'$/d" "ci.yml docs denylist"
  check_mutation_fails "reports/** dropped from bench-image.yml paths-ignore" BENCH_WORKFLOW \
    "/^      - 'reports\/\*\*'$/d" "ci.yml docs denylist"
fi

exit "${fail}"
