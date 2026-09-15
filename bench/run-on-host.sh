# Shared SSM runner for the bench recipes (`just bench`, `just prefix-cache`). Sourced
# into a recipe that has already set `region` and `instance` (the bench host's id); it
# defines run_on_host, which sends one shell command to the host over SSM Run Command,
# polls until it finishes, then surfaces its stderr. stdout is not retrieved — the host
# scripts log progress to stderr and write real results to S3. Returns 0 if the host
# command succeeded, non-zero otherwise (send/poll failure, a terminal non-Success
# status, or the wait ceiling).
#
# shellcheck shell=bash
# shellcheck disable=SC2154  # region and instance are set by the sourcing recipe
run_on_host() {
  local label=$1 command=$2 timeout=$3 cid status
  local params
  # The command string is JSON-escaped by python so it cannot break the parameters
  # document (a raw quote in the command would otherwise produce invalid JSON).
  params="$(python3 -c 'import json,sys; print(json.dumps({"commands":[sys.argv[1]]}))' "${command}")"
  # Capture send-command explicitly. Some callers invoke this as `... || rc=$?` (to
  # salvage a partial run), which suspends set -e for the whole body, so an unchecked
  # send failure would leave cid empty and spin the poll loop to the deadline instead
  # of failing fast. The explicit check fails fast for those callers and is harmless
  # for the ones that invoke it bare.
  if ! cid="$(aws ssm send-command --region "${region}" \
    --instance-ids "${instance}" \
    --document-name AWS-RunShellScript \
    --comment "${label}" \
    --parameters "${params}" \
    --query Command.CommandId --output text)"; then
    echo "${label}: ssm send-command failed" >&2
    return 1
  fi
  local deadline=$((SECONDS + timeout))
  while ((SECONDS < deadline)); do
    # get-command-invocation 404s for a moment right after send; tolerate only that
    # InvocationDoesNotExist race as Pending. Any other error (credentials expiring
    # mid-sweep, throttling, wrong region) stops the wait with its message rather
    # than being swallowed as Pending and spinning to the deadline.
    if ! status="$(aws ssm get-command-invocation --region "${region}" \
      --command-id "${cid}" --instance-id "${instance}" \
      --query Status --output text 2>&1)"; then
      if [[ "${status}" == *InvocationDoesNotExist* ]]; then
        status="Pending"
      else
        echo "${label}: polling failed: ${status}" >&2
        return 1
      fi
    fi
    case "${status}" in
    Success)
      aws ssm get-command-invocation --region "${region}" --command-id "${cid}" \
        --instance-id "${instance}" --query StandardErrorContent --output text >&2 || true
      return 0
      ;;
    Failed | Cancelled | TimedOut | Undeliverable | Terminated)
      echo "${label}: ${status}" >&2
      aws ssm get-command-invocation --region "${region}" --command-id "${cid}" \
        --instance-id "${instance}" --query StandardErrorContent --output text >&2 || true
      return 1
      ;;
    esac
    sleep 5
  done
  echo "${label}: did not finish within ${timeout}s (last status ${status})" >&2
  return 1
}
