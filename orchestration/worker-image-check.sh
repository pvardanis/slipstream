#!/usr/bin/env bash
# Check the Prefect worker runs main's latest orchestration image before
# flow-dispatch.yml creates a run, and show the flow's config in the job summary. The
# image's tag is the content tag: 12 hex characters of the last commit that touched the
# image's inputs, flow code and baked configs among them (content-tag.sh). Main's latest
# image is the one content-tag.sh derives from this checkout, so a worker tag that
# differs from it means the worker runs code or config main no longer holds: Image
# Updater has not yet polled the new image, a rollout freeze is holding the rollout back,
# or the image is still building. The worker reads the config baked into its image, not
# this checkout, so the summary shows the worker commit's copy; a flow that reads no baked
# config passes an empty config path, and the summary names the run and the worker image
# alone. Run from a checkout of main with full history.
# Usage: worker-image-check.sh <prefect-namespace> <config-repo-path|""> <summary-heading> <flow-run-id>
set -euo pipefail

prefect_namespace="$1"
config_repo_path="$2"
config_heading="$3"
flow_run_id="$4"
: "${GITHUB_STEP_SUMMARY:?GITHUB_STEP_SUMMARY is unset — run inside a GitHub Actions step}"

worker_images="$(kubectl -n "${prefect_namespace}" get pods \
  -l app.kubernetes.io/name=prefect-worker --field-selector=status.phase=Running \
  -o jsonpath='{range .items[*]}{.spec.containers[0].image}{"\n"}{end}' | sort -u)" || {
  echo "::error::cannot list running prefect-worker pods in namespace ${prefect_namespace}; check the dispatch role's cluster access"
  exit 1
}
if [[ -z "${worker_images}" || "${worker_images}" == *$'\n'* ]]; then
  echo "::error::expected one running prefect-worker image, got: ${worker_images:-none}"
  exit 1
fi
worker_tag="${worker_images##*:}"
if [[ -z "${config_repo_path}" ]]; then
  {
    echo "## ${config_heading}"
    echo
    echo "Run \`${flow_run_id}\` reads no config; it runs worker image \`${worker_images}\`."
  } >>"${GITHUB_STEP_SUMMARY}"
else
  config_yaml="$(git show "${worker_tag}:${config_repo_path}")" || {
    echo "::error::cannot read ${config_repo_path} at worker image commit ${worker_tag}"
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
fi
# Refuse to create the run unless the worker runs main's latest image: the run would
# execute flow code or config main no longer holds. Checked after the summary, so the
# job page still shows the config the worker would have run.
main_tag="$("$(dirname "${BASH_SOURCE[0]}")/content-tag.sh")"
if [[ "${worker_tag}" != "${main_tag}" ]]; then
  echo "::error::rollout pending — the worker runs image ${worker_tag}, main's latest is ${main_tag}; retry once it lands, or fix main's orchestration-image run if it failed"
  exit 1
fi
