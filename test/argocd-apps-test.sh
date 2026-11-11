#!/usr/bin/env bash
# Static check of the child Applications the app-of-apps chart (k8s/argocd/apps) renders
# for the root Application: the chart is rendered with the cluster values the eks stack
# passes (ci/test-values.yaml), and the Prefect worker's Application must deploy the
# pinned upstream chart into the prefect namespace, server-side, with the worker values
# Terraform used to release it with — every cluster-specific one taken from the root's
# values, the image tag included. Needs helm and yq; no cluster.
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

# The value at yq path $1 in a root value file ($2), as yq prints it.
value_of() {
  yq "$1" "$2"
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

values='.spec.source.helm.valuesObject'

# --- Placement: project, source chart, destination, sync ---
expect '.metadata.namespace' 'argocd' "child Applications live in Argo CD's namespace"
expect '.spec.project' 'workloads' 'the workloads project scopes where the worker may deploy'
expect '.spec.source.repoURL' 'https://prefecthq.github.io/prefect-helm' 'the upstream prefect-helm chart repository'
expect '.spec.source.chart' 'prefect-worker' 'the upstream prefect-worker chart'
expect '.spec.source.targetRevision' '2026.9.26234203' 'the pinned chart version, matching the prefect-server chart'
expect '.spec.destination.server' 'https://kubernetes.default.svc' 'the cluster Argo CD runs in'
expect '.spec.destination.namespace' "$(value_of '.namespaces.prefect' "${apps_values}")" "the prefect namespace from the root's values, where Pod Identity binds the worker"
expect '.spec.syncPolicy.automated' "$(printf 'prune: true\nselfHeal: true')" 'automated sync that prunes and reverts drift'
expect '.spec.syncPolicy.syncOptions | length' '1' 'server-side apply is the one sync option'
expect '.spec.syncPolicy.syncOptions[0]' 'ServerSideApply=true' 'server-side apply'

# --- Worker values ---
expect "${values}.worker.image.repository" "$(value_of '.orchestrationImageRepository' "${apps_values}")" "the image repository from the root's values"
expect "${values}.worker.image.prefectTag" "$(value_of '.orchestrationImageTag' "${apps_values}")" "the image tag from the root's values"
expect "${values}.worker.image.pullPolicy" 'IfNotPresent' 'the tag is an immutable content-sha, so a cached image is always the right one'
expect "${values}.worker.config.type" 'process' 'a process worker runs the serial single-GPU sweep as a subprocess'
expect "${values}.worker.config.workPool" "$(sed -n 's/^prefect_work_pool := "\(.*\)"$/\1/p' "${repo_root}/justfile")" 'the work pool just prefect-up creates (justfile prefect_work_pool)'
expect "${values}.worker.apiConfig" 'selfHostedServer' 'the in-cluster self-hosted server'
expect "${values}.worker.selfHostedServerApiConfig.apiUrl" "$(value_of '.prefectApiUrl' "${apps_values}")" "the server's in-cluster API URL from the root's values"
expect "${values}.worker.replicaCount" '1' 'one worker drains the concurrency-1 pool'
expect "${values}.worker.extraEnvVarsCMs | length" '1' 'the worker loads one ConfigMap into its environment'
expect "${values}.worker.extraEnvVarsCMs[0]" "$(value_of '.configMaps.benchEndpointTopology' "${apps_values}")" "the bench-endpoint topology ConfigMap from the root's values"
expect "${values}.serviceAccount.create" 'true' 'the chart creates the ServiceAccount Pod Identity binds'
expect "${values}.serviceAccount.name" "$(value_of '.serviceAccounts.prefectWorker' "${apps_values}")" "the ServiceAccount name from the root's values"
expect "${values}.role.create" 'false' 'the eks stack owns the worker RBAC'
expect "${values}.rolebinding.create" 'false' 'the eks stack owns the worker RBAC'

exit "${fail}"
