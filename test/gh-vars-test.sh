#!/usr/bin/env bash
# Unit test for bench/gh-vars.sh: terraform and gh are stubbed on PATH so the
# test asserts the repo variables the script sets from the bootstrap outputs, never
# a real terraform read or GitHub write. It checks that all eight variables are
# set with their output values (the ECR repository names stripped of their
# registry host); that a failed, empty or malformed output, or an unauthenticated
# gh, aborts the script before any variable is set; and that a gh failure
# mid-update exits non-zero and says to rerun.
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
script="${repo_root}/bench/gh-vars.sh"
tmp="$(mktemp -d)"
trap 'rm -rf "${tmp}"' EXIT

# Stub terraform: answer `-chdir=<dir> output -raw <name>` from a fixed table,
# rejecting any other call shape. EMPTY_OUTPUT names one output that resolves to exit-0 empty stdout;
# FAIL_OUTPUT names one whose read fails after printing junk to stdout;
# BAD_REPO_URL makes the bench repository URL lose its registry host.
mkdir -p "${tmp}/bin"
cat >"${tmp}/bin/terraform" <<'EOF'
#!/usr/bin/env bash
[[ "$#" == 4 && "$2" == output && "$3" == -raw ]] || { echo "unexpected call: $*" >&2; exit 2; }
dir="${1#-chdir=}" name="$4"
[[ "${name}" == "${EMPTY_OUTPUT:-}" ]] && exit 0
if [[ "${name}" == "${FAIL_OUTPUT:-}" ]]; then
  echo junk
  echo "Output ${name} not found" >&2
  exit 1
fi
[[ "${name}" == bench_image_repo_url && "${BAD_REPO_URL:-0}" != 0 ]] && { echo slipstream-bench; exit 0; }
case "${dir}/${name}" in
  bootstrap/region) echo eu-west-1 ;;
  bootstrap/bench_image_push_role_arn) echo arn:bench-push ;;
  bootstrap/bench_image_repo_url) echo 123.dkr.ecr.eu-west-1.amazonaws.com/slipstream/bench ;;
  bootstrap/orchestration_image_push_role_arn) echo arn:orch-push ;;
  bootstrap/orchestration_image_repo_url) echo 123.dkr.ecr.eu-west-1.amazonaws.com/slipstream/orch ;;
  bootstrap/sweep_dispatch_role_arn) echo arn:dispatch ;;
  bootstrap/cluster_name) echo slipstream ;;
  bootstrap/prefect_namespace) echo prefect ;;
  *) echo "unexpected output ${dir}/${name}" >&2; exit 1 ;;
esac
EOF
# Stub gh: `auth status` exits ${GH_AUTH_EXIT:-0}; every variable set appends one
# line, except the one named by GH_FAIL_ON, which fails unrecorded.
cat >"${tmp}/bin/gh" <<EOF
#!/usr/bin/env bash
[[ "\$1" == auth ]] && exit "\${GH_AUTH_EXIT:-0}"
[[ "\$3" == "\${GH_FAIL_ON:-}" ]] && { echo "HTTP 502" >&2; exit 1; }
printf '%s\n' "\$*" >>"${tmp}/gh-calls"
EOF
chmod +x "${tmp}/bin/terraform" "${tmp}/bin/gh"

fail=0
run() {
  : >"${tmp}/gh-calls"
  PATH="${tmp}/bin:${PATH}" "${script}" bootstrap 2>"${tmp}/stderr"
}
check() {
  local label="$1" want="$2"
  if grep -qxF -- "${want}" "${tmp}/gh-calls"; then
    echo "ok   ${label}"
  else
    echo "FAIL ${label}: '${want}' not in gh calls" >&2
    fail=1
  fi
}
check_aborted() {
  local label="$1" want_err="$2"
  if run; then
    echo "FAIL ${label}: script exited 0" >&2
    fail=1
  elif [[ -s "${tmp}/gh-calls" ]]; then
    echo "FAIL ${label}: gh called before abort" >&2
    fail=1
  elif ! grep -qF -- "${want_err}" "${tmp}/stderr"; then
    echo "FAIL ${label}: stderr lacks '${want_err}'" >&2
    fail=1
  else
    echo "ok   ${label}"
  fi
}

# Happy path: every variable the workflows read, set from its output.
run || {
  cat "${tmp}/stderr" >&2
  exit 1
}
check "region" "variable set AWS_REGION --body eu-west-1"
check "bench push role" "variable set AWS_BENCH_IMAGE_PUSH_ROLE_ARN --body arn:bench-push"
check "bench repo name" "variable set ECR_REPOSITORY --body slipstream/bench"
check "orch push role" "variable set AWS_ORCHESTRATION_IMAGE_PUSH_ROLE_ARN --body arn:orch-push"
check "orch repo name" "variable set ORCHESTRATION_ECR_REPOSITORY --body slipstream/orch"
check "dispatch role" "variable set AWS_SWEEP_DISPATCH_ROLE_ARN --body arn:dispatch"
check "cluster name" "variable set EKS_CLUSTER_NAME --body slipstream"
check "prefect namespace" "variable set PREFECT_NAMESPACE --body prefect"
got="$(wc -l <"${tmp}/gh-calls" | tr -d ' ')"
if [[ "${got}" == 8 ]]; then
  echo "ok   exactly eight variables"
else
  echo "FAIL exactly eight variables: got ${got}" >&2
  fail=1
fi

# An output that resolves to nothing aborts rather than setting an empty variable.
EMPTY_OUTPUT=sweep_dispatch_role_arn check_aborted "empty bootstrap output" "sweep_dispatch_role_arn is empty"
EMPTY_OUTPUT=prefect_namespace check_aborted "empty namespace output" "prefect_namespace is empty"
# A failed read never passes its stdout through as a value, and says it failed.
FAIL_OUTPUT=region check_aborted "failed bootstrap read" "cannot read bootstrap output region"
FAIL_OUTPUT=cluster_name check_aborted "failed cluster name read" "cannot read bootstrap output cluster_name"
# A repository URL without a registry host would set the URL itself as the name.
BAD_REPO_URL=1 check_aborted "repo url without host" "bench_image_repo_url is not <registry>/<name>"
# An unauthenticated gh stops the script before the first variable.
GH_AUTH_EXIT=1 check_aborted "gh not authenticated" "gh auth login"

# A gh failure mid-update leaves earlier variables set; the script says to rerun.
if GH_FAIL_ON=ORCHESTRATION_ECR_REPOSITORY run; then
  echo "FAIL gh failure mid-update: script exited 0" >&2
  fail=1
elif ! grep -qF "rerun \`just gh-vars\`" "${tmp}/stderr"; then
  echo "FAIL gh failure mid-update: stderr lacks the rerun hint" >&2
  fail=1
else
  echo "ok   gh failure mid-update"
fi

exit "${fail}"
