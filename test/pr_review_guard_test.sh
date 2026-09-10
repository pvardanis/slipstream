#!/usr/bin/env bash
# Test for the PreToolUse guard that stops `gh pr create` until the
# pr-review-toolkit review has run in the session. Drives the hook the way Claude
# Code does — a JSON payload on stdin — and asserts the exit code and stderr a
# person reads back. The blocking case is proven by the "no review" test, which
# fails the day the `exit 2` is removed.
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
hook="${repo_root}/.claude/hooks/guard-review-before-a-pull-request.sh"

work_dir="$(mktemp -d)"
cleanup() { rm -rf "${work_dir}"; }
trap cleanup EXIT

# A transcript that launched the review toolkit, and one that never did.
reviewed="${work_dir}/reviewed.jsonl"
printf '%s\n' '{"type":"assistant","message":{"content":[{"type":"tool_use","name":"Task","input":{"subagent_type":"pr-review-toolkit:code-reviewer"}}]}}' >"${reviewed}"
unreviewed="${work_dir}/unreviewed.jsonl"
printf '%s\n' '{"type":"assistant","message":{"content":[{"type":"text","text":"opening the pull request now"}]}}' >"${unreviewed}"

# The stdin payload the harness hands a PreToolUse hook.
payload() {
  local command="$1" transcript="${2:-}"
  jq -cn --arg cmd "${command}" --arg tp "${transcript}" \
    '{tool_input: {command: $cmd}, transcript_path: $tp}'
}

# Pipe a payload into the hook, capturing exit code and stderr. A third argument,
# if given, is a NAME=VALUE set in the hook's environment (the escape hatch).
run_hook() {
  local payload="$1" env_pair="${2:-}" status
  hook_err=""
  set +e
  if [[ -n "${env_pair}" ]]; then
    hook_err="$(printf '%s' "${payload}" | env "${env_pair}" "${hook}" 2>&1 >/dev/null)"
  else
    hook_err="$(printf '%s' "${payload}" | "${hook}" 2>&1 >/dev/null)"
  fi
  status=$?
  set -e
  return "${status}"
}

fail=0
expect_pass() {
  local desc="$1" payload="$2" env_pair="${3:-}"
  if run_hook "${payload}" "${env_pair}"; then
    return
  fi
  echo "FAIL: ${desc} — expected exit 0 (allowed), got block. stderr: ${hook_err}" >&2
  fail=1
}
expect_block() {
  local desc="$1" payload="$2"
  if run_hook "${payload}"; then
    echo "FAIL: ${desc} — expected exit 2 (blocked), got pass" >&2
    fail=1
    return
  fi
  if [[ "${hook_err}" != *"pr-review"* ]]; then
    echo "FAIL: ${desc} — blocked but message did not name the review. stderr: ${hook_err}" >&2
    fail=1
  fi
}

# A command that is not opening a pull request passes untouched.
expect_pass "non-PR command" "$(payload 'git status')"

# gh pr create with a review in the transcript opens freely.
expect_pass "reviewed PR" "$(payload 'gh pr create --base main' "${reviewed}")"

# gh pr create with no review in the transcript is blocked. This is the test the
# guard exists to pass; deleting the exit 2 fails it.
expect_block "unreviewed PR blocked" "$(payload 'gh pr create --base main' "${unreviewed}")"

# The escape hatch lets a deliberately-unreviewed PR through.
expect_pass "escape hatch" "$(payload 'gh pr create' "${unreviewed}")" "SLIPSTREAM_SKIP_PR_REVIEW=1"

# What the guard cannot read, it allows: an unreadable transcript never blocks.
expect_pass "unreadable transcript" "$(payload 'gh pr create' "${work_dir}/missing.jsonl")"

if [[ "${fail}" -ne 0 ]]; then
  exit 1
fi

echo "PASS: guard blocks an unreviewed PR, allows reviewed / skipped / unseeable ones"
