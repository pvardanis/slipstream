#!/usr/bin/env bash
# Static check of the child Applications the app-of-apps chart (k8s/argocd/apps) renders
# for the root Application: the chart is rendered with the cluster values the eks stack
# passes (ci/test-values.yaml), and the Prefect worker's Application must deploy the
# pinned upstream chart into the prefect namespace, server-side, with the worker's
# values — every cluster-specific one taken from the root's values — and opt in to Argo
# CD Image Updater, the single writer of its image tag. The upstream chart is then
# rendered with those values and the tag Image Updater writes, so a key the chart does
# not read fails here when it feeds an asserted Deployment field, rather than leaving the
# worker on a chart default. A render with any cluster value missing must fail. Needs
# helm, yq and the prefect-helm chart repository; no cluster.
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
apps_chart="${repo_root}/k8s/argocd/apps"
apps_values="${apps_chart}/ci/test-values.yaml"

fail=0

# The app-of-apps chart rendered with the root's CI values, plus any extra helm flags.
render_apps() {
  helm template argocd-apps-test "${apps_chart}" --values "${apps_values}" "$@"
}

rendered="$(render_apps)"
worker_app="$(yq 'select(.kind == "Application" and .metadata.name == "prefect-worker")' <<<"${rendered}")"
if [[ -z "${worker_app}" ]]; then
  echo "FAIL: ${apps_chart} renders no prefect-worker Application" >&2
  exit 1
fi

# The root's value at yq path $1, as yq prints it.
root_value() {
  yq "$1" "${apps_values}"
}

# Fails when the value at yq path $2 in document $1 is not $3, naming what $4 says it
# must be.
expect_in() {
  local actual
  actual="$(yq "$2" <<<"$1")"
  if [[ "${actual}" != "$3" ]]; then
    echo "FAIL: $2 is '${actual}', want '$3' ($4)" >&2
    fail=1
  fi
}

# expect_in on the worker Application.
expect() {
  expect_in "${worker_app}" "$@"
}

# expect on the worker Application's Helm values, at yq path $1 under them.
expect_value() {
  expect "${worker_values_path}$1" "${@:2}"
}

worker_values_path='.spec.source.helm.valuesObject'

work_pool="$(sed -n 's/^prefect_work_pool := "\(.*\)"$/\1/p' "${repo_root}/justfile")"
if [[ -z "${work_pool}" ]]; then
  echo "FAIL: no prefect_work_pool := \"...\" line in ${repo_root}/justfile" >&2
  exit 1
fi

# --- Placement: project, source chart, destination, sync ---
expect '.metadata.namespace' 'argocd' "child Applications live in Argo CD's namespace"
expect '.spec.project' 'workloads' 'the workloads project scopes where the worker may deploy'
expect '.spec.source.repoURL' 'https://prefecthq.github.io/prefect-helm' 'the upstream prefect-helm chart repository'
expect '.spec.source.chart' 'prefect-worker' 'the upstream prefect-worker chart'
expect '.spec.destination.server' 'https://kubernetes.default.svc' 'the cluster Argo CD runs in'
expect '.spec.destination.namespace' "$(root_value '.namespaces.prefect')" "the prefect namespace from the root's values, where Pod Identity binds the worker"
expect '.spec.syncPolicy.automated' "$(printf 'prune: true\nselfHeal: true')" 'automated sync that prunes and reverts drift'
expect '.spec.syncPolicy.syncOptions | length' '1' 'server-side apply is the one sync option'
expect '.spec.syncPolicy.syncOptions[0]' 'ServerSideApply=true' 'server-side apply'

# --- Worker values ---
expect_value '.worker.image.repository' "$(root_value '.orchestrationImageRepository')" "the image repository from the root's values"
expect_value '.worker.image.prefectTag' 'null' 'Image Updater writes the tag, so git sets none'
expect_value '.worker.image.pullPolicy' 'IfNotPresent' 'the tag is an immutable content-sha, so a cached image is always the right one'
expect_value '.worker.config.type' 'process' 'a process worker runs the serial single-GPU sweep as a subprocess'
expect_value '.worker.config.workPool' "${work_pool}" 'the work pool just prefect-up creates (justfile prefect_work_pool)'
expect_value '.worker.apiConfig' 'selfHostedServer' 'the in-cluster self-hosted server'
expect_value '.worker.selfHostedServerApiConfig.apiUrl' "$(root_value '.prefectApiUrl')" "the server's in-cluster API URL from the root's values"
expect_value '.worker.replicaCount' '1' 'one worker drains the concurrency-1 pool'
expect_value '.worker.extraEnvVarsCMs | length' '1' 'the worker loads one ConfigMap into its environment'
expect_value '.worker.extraEnvVarsCMs[0]' "$(root_value '.configMaps.benchEndpointTopology')" "the bench-endpoint topology ConfigMap from the root's values"
expect_value '.serviceAccount.create' 'true' 'the chart creates the ServiceAccount Pod Identity binds'
expect_value '.serviceAccount.name' "$(root_value '.serviceAccounts.prefectWorker')" "the ServiceAccount name from the root's values"
expect_value '.role.create' 'false' 'the eks stack owns the worker RBAC'
expect_value '.rolebinding.create' 'false' 'the eks stack owns the worker RBAC'

# --- Image Updater: the single writer of the worker's image tag ---
# The ImageUpdater resource (terraform/eks/charts/image-updater-apps) reads the settings
# of the Applications carrying its selector label from their annotations.
updater_annotation='.metadata.annotations["argocd-image-updater.argoproj.io/'
selector="$(helm template image-updater-apps "${repo_root}/terraform/eks/charts/image-updater-apps" |
  yq '.spec.applicationRefs[0].labelSelectors.matchLabels | to_entries | .[0] | .key + "=" + .value')"
expect '.metadata.labels | to_entries | map(.key + "=" + .value) | join(",")' "${selector}" "the label the ImageUpdater resource selects Applications by"
expect "${updater_annotation}image-list\"]" "worker=$(root_value '.orchestrationImageRepository')" "the orchestration image from the root's values, under the alias worker"
expect "${updater_annotation}worker.update-strategy\"]" 'newest-build' 'the most recently built image wins'
expect "${updater_annotation}worker.allow-tags\"]" 'regexp:^[0-9a-f]{12}$' 'only the 12-hex content-sha tags, never :main'
# The tag CI pushes must pass that filter, or newest-build keeps the worker on the newest
# older image with nothing failing. The pattern is read from the annotation, so a change
# to either side fails here.
content_tag="$("${repo_root}/orchestration/content-tag.sh")"
allow_tags="$(yq "${updater_annotation}worker.allow-tags\"]" <<<"${worker_app}")"
if [[ ! "${content_tag}" =~ ${allow_tags#regexp:} ]]; then
  echo "FAIL: orchestration/content-tag.sh prints '${content_tag}', which the worker's allow-tags '${allow_tags}' rejects" >&2
  fail=1
fi
expect "${updater_annotation}write-back-method\"]" 'argocd' "the tag is written onto the Application, since main's required check blocks a bot's git push"
expect "${updater_annotation}worker.helm.image-name\"]" 'worker.image.repository' "the worker chart's image repository value"
expect "${updater_annotation}worker.helm.image-tag\"]" 'worker.image.prefectTag' "the worker chart's image tag value"

# --- The upstream chart, rendered with the worker's values ---
# What Argo CD syncs: the pinned prefect-worker chart rendered with the Application's
# values and the tag Image Updater writes as a Helm parameter, into its destination
# namespace, under its name as the release name.
version="$(yq '.spec.source.targetRevision' <<<"${worker_app}")"
if [[ ! "${version}" =~ ^[0-9][0-9.]*$ ]]; then
  echo "FAIL: prefect-worker Application targetRevision '${version}' is not a pinned chart version" >&2
  exit 1
fi
worker_values="$(mktemp)"
trap 'rm -f "${worker_values}"' EXIT
yq "${worker_values_path}" <<<"${worker_app}" >"${worker_values}"
chart="$(yq '.spec.source.chart' <<<"${worker_app}")"
repo_url="$(yq '.spec.source.repoURL' <<<"${worker_app}")"
# The Helm parameter Image Updater writes the tag to, as the worker app names it.
tag_parameter="$(yq "${updater_annotation}worker.helm.image-tag\"]" <<<"${worker_app}")"
worker_rendered="$(helm template prefect-worker "${chart}" --repo "${repo_url}" --version "${version}" \
  --namespace "$(root_value '.namespaces.prefect')" --values "${worker_values}" \
  --set "${tag_parameter}=${content_tag}")"
deployment="$(yq 'select(.kind == "Deployment")' <<<"${worker_rendered}")"
container='.spec.template.spec.containers[0]'
expect_in "${deployment}" '.metadata.name' 'prefect-worker' 'the Deployment the dispatch guard reads the worker image from'
expect_in "${deployment}" '.spec.replicas' '1' 'one worker'
# cluster-up waits for the worker's image as the Application's only one.
expect_in "${deployment}" '.spec.template.spec.containers | length' '1' "the worker's container is the pod's only one"
expect_in "${deployment}" '.spec.template.spec.initContainers | length' '0' "no init container image beside the worker's"
expect_in "${deployment}" "${container}.image" "$(root_value '.orchestrationImageRepository'):${content_tag}" "the image from the root's values, at the tag Image Updater writes"
expect_in "${deployment}" "${container}.imagePullPolicy" 'IfNotPresent' 'the immutable content-sha tag'
expect_in "${deployment}" "${container}.args | join(\" \")" "prefect worker start --type process --pool ${work_pool} --install-policy prompt" 'a process worker on the work pool'
expect_in "${deployment}" "${container}.env[] | select(.name == \"PREFECT_API_URL\") | .value" "$(root_value '.prefectApiUrl')" "the server's API URL from the root's values"
expect_in "${deployment}" "[${container}.envFrom[].configMapRef.name] | join(\",\")" "$(root_value '.configMaps.benchEndpointTopology')" 'the bench-endpoint topology ConfigMap, and only it'
expect_in "${deployment}" '.spec.template.spec.serviceAccountName' "$(root_value '.serviceAccounts.prefectWorker')" 'the ServiceAccount Pod Identity binds'
rendered_kinds="$(yq ea '[select(.kind) | .kind + "/" + .metadata.name] | sort | join(",")' <<<"${worker_rendered}")"
if [[ "${rendered_kinds}" != "Deployment/prefect-worker,ServiceAccount/$(root_value '.serviceAccounts.prefectWorker')" ]]; then
  echo "FAIL: the worker chart renders ${rendered_kinds}, want only the worker Deployment and its ServiceAccount (no chart RBAC)" >&2
  fail=1
fi

# --- A missing cluster value fails the render ---
# Each cluster value the worker's Application reads, emptied in turn as a root that
# does not pass it would, must fail the render naming it.
for key in orchestrationImageRepository prefectApiUrl \
  namespaces.prefect serviceAccounts.prefectWorker configMaps.benchEndpointTopology; do
  if missing_render="$(render_apps --set "${key}=" 2>&1)"; then
    echo "FAIL: ${apps_chart} renders with ${key} empty" >&2
    fail=1
  elif [[ "${missing_render}" != *"${key} is required"* ]]; then
    echo "FAIL: ${apps_chart} fails with ${key} empty, but not on it: ${missing_render}" >&2
    fail=1
  fi
done

exit "${fail}"
