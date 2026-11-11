#!/usr/bin/env bash
# Static check of the child Applications the app-of-apps chart (k8s/argocd/apps) renders
# for the root Application: the chart is rendered with the cluster values the eks stack
# passes (ci/test-values.yaml), and the Prefect worker's Application must deploy the
# pinned upstream chart into the prefect namespace, server-side, with the worker's values —
# every cluster-specific one taken from the root's values, the image tag included. Needs helm and yq; no cluster.
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

# Fails when the worker Application's value at yq path $1 is not $2, naming what $3 says
# it must be.
expect() {
  local actual
  actual="$(yq "$1" <<<"${worker_app}")"
  if [[ "${actual}" != "$2" ]]; then
    echo "FAIL: prefect-worker Application $1 is '${actual}', want '$2' ($3)" >&2
    fail=1
  fi
}

worker_values_path='.spec.source.helm.valuesObject'

# --- Placement: project, source chart, destination, sync ---
expect '.metadata.namespace' 'argocd' "child Applications live in Argo CD's namespace"
expect '.spec.project' 'workloads' 'the workloads project scopes where the worker may deploy'
expect '.spec.source.repoURL' 'https://prefecthq.github.io/prefect-helm' 'the upstream prefect-helm chart repository'
expect '.spec.source.chart' 'prefect-worker' 'the upstream prefect-worker chart'
expect '.spec.source.targetRevision' '2026.9.26234203' 'the pinned chart version, matching the prefect-server chart'
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
expect "${worker_values_path}.worker.config.workPool" "$(sed -n 's/^prefect_work_pool := "\(.*\)"$/\1/p' "${repo_root}/justfile")" 'the work pool just prefect-up creates (justfile prefect_work_pool)'
expect "${worker_values_path}.worker.apiConfig" 'selfHostedServer' 'the in-cluster self-hosted server'
expect "${worker_values_path}.worker.selfHostedServerApiConfig.apiUrl" "$(root_value '.prefectApiUrl')" "the server's in-cluster API URL from the root's values"
expect "${worker_values_path}.worker.replicaCount" '1' 'one worker drains the concurrency-1 pool'
expect "${worker_values_path}.worker.extraEnvVarsCMs | length" '1' 'the worker loads one ConfigMap into its environment'
expect "${worker_values_path}.worker.extraEnvVarsCMs[0]" "$(root_value '.configMaps.benchEndpointTopology')" "the bench-endpoint topology ConfigMap from the root's values"
expect "${worker_values_path}.serviceAccount.create" 'true' 'the chart creates the ServiceAccount Pod Identity binds'
expect "${worker_values_path}.serviceAccount.name" "$(root_value '.serviceAccounts.prefectWorker')" "the ServiceAccount name from the root's values"
expect "${worker_values_path}.role.create" 'false' 'the eks stack owns the worker RBAC'
expect "${worker_values_path}.rolebinding.create" 'false' 'the eks stack owns the worker RBAC'

exit "${fail}"
