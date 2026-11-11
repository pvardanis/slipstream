#!/usr/bin/env bash
# Static check of the child Applications the app-of-apps chart (k8s/argocd/apps) renders
# for the root Application: the chart is rendered with the cluster values the eks stack
# passes (ci/test-values.yaml), and the Prefect worker's Application must deploy the
# pinned upstream chart into the prefect namespace, server-side, with the worker's
# values — every cluster-specific one taken from the root's values, the image tag
# included. The upstream chart is then rendered with those values, so a key the chart
# does not read fails here rather than leaving the worker on a chart default. A render
# with any cluster value missing must fail. Needs helm, yq and the prefect-helm chart
# repository; no cluster.
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
apps_chart="${repo_root}/k8s/argocd/apps"
apps_values="${apps_chart}/ci/test-values.yaml"

fail=0

rendered="$(helm template argocd-apps-test "${apps_chart}" --values "${apps_values}")"
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
expect "${worker_values_path}.worker.image.repository" "$(root_value '.orchestrationImageRepository')" "the image repository from the root's values"
expect "${worker_values_path}.worker.image.prefectTag" "$(root_value '.orchestrationImageTag')" "the image tag from the root's values"
expect "${worker_values_path}.worker.image.pullPolicy" 'IfNotPresent' 'the tag is an immutable content-sha, so a cached image is always the right one'
expect "${worker_values_path}.worker.config.type" 'process' 'a process worker runs the serial single-GPU sweep as a subprocess'
expect "${worker_values_path}.worker.config.workPool" "${work_pool}" 'the work pool just prefect-up creates (justfile prefect_work_pool)'
expect "${worker_values_path}.worker.apiConfig" 'selfHostedServer' 'the in-cluster self-hosted server'
expect "${worker_values_path}.worker.selfHostedServerApiConfig.apiUrl" "$(root_value '.prefectApiUrl')" "the server's in-cluster API URL from the root's values"
expect "${worker_values_path}.worker.replicaCount" '1' 'one worker drains the concurrency-1 pool'
expect "${worker_values_path}.worker.extraEnvVarsCMs | length" '1' 'the worker loads one ConfigMap into its environment'
expect "${worker_values_path}.worker.extraEnvVarsCMs[0]" "$(root_value '.configMaps.benchEndpointTopology')" "the bench-endpoint topology ConfigMap from the root's values"
expect "${worker_values_path}.serviceAccount.create" 'true' 'the chart creates the ServiceAccount Pod Identity binds'
expect "${worker_values_path}.serviceAccount.name" "$(root_value '.serviceAccounts.prefectWorker')" "the ServiceAccount name from the root's values"
expect "${worker_values_path}.role.create" 'false' 'the eks stack owns the worker RBAC'
expect "${worker_values_path}.rolebinding.create" 'false' 'the eks stack owns the worker RBAC'

# --- The upstream chart, rendered with the worker's values ---
# What Argo CD syncs: the pinned prefect-worker chart rendered with the Application's
# values into its destination namespace, under its name as the release name.
version="$(yq '.spec.source.targetRevision' <<<"${worker_app}")"
if [[ ! "${version}" =~ ^[0-9][0-9.]*$ ]]; then
  echo "FAIL: prefect-worker Application targetRevision '${version}' is not a pinned chart version" >&2
  exit 1
fi
worker_values="$(mktemp)"
trap 'rm -f "${worker_values}"' EXIT
yq "${worker_values_path}" <<<"${worker_app}" >"${worker_values}"
worker_rendered="$(helm template prefect-worker "$(yq '.spec.source.chart' <<<"${worker_app}")" \
  --repo "$(yq '.spec.source.repoURL' <<<"${worker_app}")" --version "${version}" \
  --namespace "$(root_value '.namespaces.prefect')" --values "${worker_values}")"
deployment="$(yq 'select(.kind == "Deployment")' <<<"${worker_rendered}")"
container='.spec.template.spec.containers[0]'
expect_in "${deployment}" '.metadata.name' 'prefect-worker' 'the Deployment the dispatch guard reads the worker image from'
expect_in "${deployment}" '.spec.replicas' '1' 'one worker'
expect_in "${deployment}" "${container}.image" "$(root_value '.orchestrationImageRepository'):$(root_value '.orchestrationImageTag')" "the image from the root's values"
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
for key in orchestrationImageRepository orchestrationImageTag prefectApiUrl \
  namespaces.prefect serviceAccounts.prefectWorker configMaps.benchEndpointTopology; do
  if missing_render="$(helm template argocd-apps-test "${apps_chart}" --values "${apps_values}" --set "${key}=" 2>&1)"; then
    echo "FAIL: ${apps_chart} renders with ${key} empty" >&2
    fail=1
  elif [[ "${missing_render}" != *"${key} is required"* ]]; then
    echo "FAIL: ${apps_chart} fails with ${key} empty, but not on it: ${missing_render}" >&2
    fail=1
  fi
done

exit "${fail}"
