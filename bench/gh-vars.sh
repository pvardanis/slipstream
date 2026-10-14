#!/usr/bin/env bash
# Set the GitHub repository variables the image and sweep-dispatch workflows read,
# from the bootstrap stack's outputs, so they can be set before any cluster exists.
# Every output is read and checked before any variable is set, so a failed, empty or
# malformed one aborts with nothing set; a gh failure mid-update stops there and says
# to rerun, which is idempotent.
# Usage: gh-vars.sh <bootstrap-dir>
set -euo pipefail

bootstrap_dir="$1"

# errexit does not reach inside a command substitution, so a failed read is checked
# explicitly rather than letting whatever terraform printed through as the value. The
# non-empty check catches the exit-0-empty-stdout case `-raw` gives for an output that
# resolves to nothing.
read_output() {
  local dir="$1" name="$2" value
  if ! value="$(terraform -chdir="${dir}" output -raw "${name}")"; then
    echo "cannot read ${dir} output ${name}; if the bootstrap stack predates it, run \`just bootstrap\`" >&2
    return 1
  fi
  if [[ -z "${value}" ]]; then
    echo "${dir} output ${name} is empty" >&2
    return 1
  fi
  printf '%s' "${value}"
}

# The workflows take the ECR repository name, not its <registry>/<name> URL.
get_repo_name() {
  local url="$1" name="$2"
  if [[ "${url}" != */* ]]; then
    echo "${bootstrap_dir} output ${name} is not <registry>/<name>: ${url}" >&2
    return 1
  fi
  printf '%s' "${url#*/}"
}

set_variable() {
  if ! gh variable set "$1" --body "$2"; then
    echo "setting $1 failed; the variables before it are set, rerun \`just gh-vars\`" >&2
    return 1
  fi
}

if ! gh auth status >/dev/null 2>&1; then
  echo "gh is not authenticated: run \`gh auth login\`" >&2
  exit 1
fi

region="$(read_output "${bootstrap_dir}" region)"
bench_push_role="$(read_output "${bootstrap_dir}" bench_image_push_role_arn)"
bench_repo_url="$(read_output "${bootstrap_dir}" bench_image_repo_url)"
bench_repo="$(get_repo_name "${bench_repo_url}" bench_image_repo_url)"
orch_push_role="$(read_output "${bootstrap_dir}" orchestration_image_push_role_arn)"
orch_repo_url="$(read_output "${bootstrap_dir}" orchestration_image_repo_url)"
orch_repo="$(get_repo_name "${orch_repo_url}" orchestration_image_repo_url)"
dispatch_role="$(read_output "${bootstrap_dir}" sweep_dispatch_role_arn)"
cluster_name="$(read_output "${bootstrap_dir}" cluster_name)"
prefect_namespace="$(read_output "${bootstrap_dir}" prefect_namespace)"

set_variable AWS_REGION "${region}"
set_variable AWS_BENCH_IMAGE_PUSH_ROLE_ARN "${bench_push_role}"
set_variable ECR_REPOSITORY "${bench_repo}"
set_variable AWS_ORCHESTRATION_IMAGE_PUSH_ROLE_ARN "${orch_push_role}"
set_variable ORCHESTRATION_ECR_REPOSITORY "${orch_repo}"
set_variable AWS_SWEEP_DISPATCH_ROLE_ARN "${dispatch_role}"
set_variable EKS_CLUSTER_NAME "${cluster_name}"
set_variable PREFECT_NAMESPACE "${prefect_namespace}"
