#!/usr/bin/env bash
# Set the GitHub repository variables the image and sweep-dispatch workflows read,
# from the bootstrap and eks stacks' outputs. Every output is read before any
# variable is set, so a missing or empty one aborts without a half-set list.
# Usage: gh-vars.sh <bootstrap-dir> <eks-dir>
set -euo pipefail

bootstrap_dir="$1"
eks_dir="$2"

# Bare `terraform output` aborts on a failed read; the non-empty check catches the
# exit-0-empty-stdout case `-raw` gives for an output that resolves to nothing.
read_output() {
  local dir="$1" name="$2" value
  value="$(terraform -chdir="${dir}" output -raw "${name}")"
  if [[ -z "${value}" ]]; then
    echo "${dir} output ${name} is empty" >&2
    return 1
  fi
  printf '%s' "${value}"
}

# The eks outputs exist only while a cluster is up, so a failed read names the fix.
read_eks_output() {
  if ! read_output "${eks_dir}" "$1"; then
    echo "cannot read eks outputs: run \`just cluster-up\` first" >&2
    return 1
  fi
}

region="$(read_output "${bootstrap_dir}" region)"
bench_push_role="$(read_output "${bootstrap_dir}" bench_image_push_role_arn)"
bench_repo_url="$(read_output "${bootstrap_dir}" bench_image_repo_url)"
orch_push_role="$(read_output "${bootstrap_dir}" orchestration_image_push_role_arn)"
orch_repo_url="$(read_output "${bootstrap_dir}" orchestration_image_repo_url)"
dispatch_role="$(read_output "${bootstrap_dir}" sweep_dispatch_role_arn)"
cluster_name="$(read_eks_output cluster_name)"
prefect_namespace="$(read_eks_output prefect_namespace)"

# The workflows take the ECR repository name, not its <registry>/<name> URL.
gh variable set AWS_REGION --body "${region}"
gh variable set AWS_BENCH_IMAGE_PUSH_ROLE_ARN --body "${bench_push_role}"
gh variable set ECR_REPOSITORY --body "${bench_repo_url#*/}"
gh variable set AWS_ORCHESTRATION_IMAGE_PUSH_ROLE_ARN --body "${orch_push_role}"
gh variable set ORCHESTRATION_ECR_REPOSITORY --body "${orch_repo_url#*/}"
gh variable set AWS_SWEEP_DISPATCH_ROLE_ARN --body "${dispatch_role}"
gh variable set EKS_CLUSTER_NAME --body "${cluster_name}"
gh variable set PREFECT_NAMESPACE --body "${prefect_namespace}"
