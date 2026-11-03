#!/usr/bin/env bash
# Static CI path-list parity check (ADR-0027): the image input filters in ci.yml's
# `changes` job must name the same paths as the git pathspecs that derive each image's
# content tag, so a PR build gate cannot skip a change that rolls the tag, nor build on
# one that does not. Compares the bench_image filter with bench/image-tag.sh (image_sha)
# and the orchestration_image filter with orchestration-image.yml's tag derivation.
# Reads the workflows with yq; runs no git, build or network call.
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ci_workflow="${CI_WORKFLOW:-${repo_root}/.github/workflows/ci.yml}"
orchestration_workflow="${ORCHESTRATION_WORKFLOW:-${repo_root}/.github/workflows/orchestration-image.yml}"
bench_image_tag="${BENCH_IMAGE_TAG:-${repo_root}/bench/image-tag.sh}"

fail=0

# The patterns of filter $1 in the changes job's paths-filter step, one per line, as
# git pathspecs: a trailing /** dropped, and a !-negation as :(exclude).
ci_filter() {
  yq '.jobs.changes.steps[] | select(.id == "filter") | .with.filters' "${ci_workflow}" |
    yq ".$1[]" |
    sed -e 's|/\*\*$||' -e 's|^!\(.*\)$|:(exclude)\1|' |
    sort
}

# The pathspec of the first `git ... log -1 ... -- <paths>)"` command in text on stdin,
# one path per line: every word after the `--` up to the closing `)"`.
pathspec_of() {
  awk '
    /git .*log -1/ { grab = 1 }
    grab {
      line = $0
      if (!seen && sub(/.* -- /, "", line)) { seen = 1 } else if (!seen) { next }
      done = sub(/\)".*/, "", line)
      gsub(/\\/, "", line)
      n = split(line, words, " ")
      for (i = 1; i <= n; i++) { w = words[i]; gsub(/\047/, "", w); print w }
      if (done) { exit }
    }
  ' | sort
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
  "bench/image-tag.sh pathspec" "$(pathspec_of <"${bench_image_tag}")"

check_same "ci.yml orchestration_image filter" "$(ci_filter orchestration_image)" \
  "orchestration-image.yml pathspec" \
  "$(yq '.jobs.publish.steps[] | select(.id == "tags") | .run' "${orchestration_workflow}" | pathspec_of)"

exit "${fail}"
