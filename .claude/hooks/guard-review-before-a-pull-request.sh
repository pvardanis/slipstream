#!/usr/bin/env bash
# Stops a pull request opening before the pr-review-toolkit review has run on the
# work. The habit is easy to skip: the skill that implements a change ends at its
# own review, and nothing checks for the deeper PR review at the moment the work
# is offered for merge. This is that check.
#
# A pull request, not every push: a push is how work in progress is kept, and
# guarding each one would ask for a review of something nobody is proposing yet.
# Opening a pull request is the moment the work is put up to be merged, which is
# the moment the rule is about.
#
# What it looks for is an agent or skill from the toolkit actually being
# launched, not the toolkit being mentioned: a conversation about reviewing would
# otherwise satisfy the guard it is discussing.
#
# It allows what it cannot check. An unreadable transcript means the harness
# changed shape, and a guard that cannot see should not stop every pull request
# on the machine — the rule it enforces is a habit, not a permission.
set -euo pipefail

payload=$(cat)
command=$(printf '%s' "$payload" | jq -r '.tool_input.command // empty')

case $command in
*"gh pr create"*) ;;
*) exit 0 ;;
esac

[[ ${SLIPSTREAM_SKIP_PR_REVIEW:-} == 1 ]] && exit 0

transcript=$(printf '%s' "$payload" | jq -r '.transcript_path // empty')
[[ -n $transcript && -r $transcript ]] || exit 0

# The shapes a launched agent and an invoked skill take in the transcript. Both
# carry the toolkit's name inside a JSON field, which prose about a review cannot
# reproduce.
#
# Assembled here rather than written out, because the transcript records this
# file being read and written: spelled in full, the guard would be satisfied by
# anyone who had opened it.
toolkit='pr-review-toolkit:'
launched='"subagent_type":"'$toolkit
invoked='"skill":"'$toolkit

grep -qF -e "$launched" -e "$invoked" "$transcript" && exit 0

cat >&2 <<'MESSAGE'
This session has not run the pr-review-toolkit review, which is the review this
project asks for before work goes up for merge.

Run /pr-review-toolkit:review-pr against the branch diff — always code-reviewer
and silent-failure-hunter; add pr-test-analyzer when tests changed,
type-design-analyzer when a type was added, comment-analyzer when a comment
claims something about a runtime or hardware. Act on what it finds, then open
the pull request.

If this one does not want a review — a docs branch, work already reviewed — say
so out loud and run the command again with SLIPSTREAM_SKIP_PR_REVIEW=1.
MESSAGE
exit 2
