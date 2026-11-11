#!/usr/bin/env bash
# Check the Prefect worker runs main's copy of a flow's config before flow-dispatch.yml
# creates a run, and show that config in the job summary. The worker reads the config
# baked into its image, not this checkout, and the worker image's tag is the last commit
# that touched the image's inputs, the baked configs among them (orchestration-image.yml),
# so that commit's copy of the file is the config the worker runs. A config that differs
# from main's means the worker has not been re-pinned since the config changed. Run from
# a checkout of main with full history.
# Usage: worker-config-check.sh <prefect-namespace> <config-repo-path> <summary-heading> <flow-run-id>
set -euo pipefail

prefect_namespace="$1"
config_repo_path="$2"
config_heading="$3"
flow_run_id="$4"
: "${GITHUB_STEP_SUMMARY:?GITHUB_STEP_SUMMARY is unset — run inside a GitHub Actions step}"

worker_images="$(kubectl -n "${prefect_namespace}" get pods \
  -l app.kubernetes.io/name=prefect-worker --field-selector=status.phase=Running \
  -o jsonpath='{range .items[*]}{.spec.containers[0].image}{"\n"}{end}' | sort -u)"
if [[ -z "${worker_images}" || "${worker_images}" == *$'\n'* ]]; then
  echo "::error::expected one running prefect-worker image, got: ${worker_images:-none}"
  exit 1
fi
worker_commit="${worker_images##*:}"
config_yaml="$(git show "${worker_commit}:${config_repo_path}")" || {
  echo "::error::cannot read ${config_repo_path} at worker image commit ${worker_commit}"
  exit 1
}
{
  echo "## ${config_heading}"
  echo
  echo "Run \`${flow_run_id}\` reads \`${config_repo_path}\` as built into worker image \`${worker_images}\`."
  echo
  echo '```yaml'
  echo "${config_yaml}"
  echo '```'
} >>"${GITHUB_STEP_SUMMARY}"
# Refuse to create the run when the worker's config is not main's: the run would
# execute a config main no longer holds. Checked after the summary, so the job page
# still shows the config the worker would have run.
if ! git diff --quiet "${worker_commit}" HEAD -- "${config_repo_path}"; then
  echo "::error::the worker runs ${config_repo_path} from ${worker_commit}, which differs from main's; re-pin the worker (just cluster-up) and dispatch again"
  exit 1
fi
