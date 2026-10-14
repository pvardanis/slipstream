#!/usr/bin/env bash
# Check the orchestration image's floating :main tag is published to the bootstrap ECR
# repo, which the eks stack resolves to deploy the Prefect worker. Run before an eks plan
# or apply so a missing image fails with the publish step to take, not a bare ECR error.
# Usage: orchestration-image-check.sh <bootstrap-dir>
set -euo pipefail

bootstrap_dir="$1"

# Bare assignments so a failed `terraform output` aborts; the non-empty checks catch
# the exit-0-empty-stdout case `-raw` gives for an output that resolves to nothing.
region="$(terraform -chdir="${bootstrap_dir}" output -raw region)"
[[ -n "${region}" ]] || {
  echo "bootstrap output region is empty" >&2
  exit 1
}
repo_url="$(terraform -chdir="${bootstrap_dir}" output -raw orchestration_image_repo_url)"
[[ -n "${repo_url}" ]] || {
  echo "bootstrap output orchestration_image_repo_url is empty" >&2
  exit 1
}
# ECR looks the repository up by its name, the URL's path after the registry host.
repo="${repo_url#*/}"

# The aws error stays on stderr, so a credentials or access failure reads as itself.
if ! aws ecr describe-images --region "${region}" --repository-name "${repo}" --image-ids imageTag=main >/dev/null; then
  echo "${repo}:main is not published (or cannot be read); publish it by running the orchestration-image workflow (push to main or \`gh workflow run orchestration-image.yml\`, after \`just gh-vars\`), then rerun" >&2
  exit 1
fi
