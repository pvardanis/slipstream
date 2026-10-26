#!/usr/bin/env bash
# Static render check for the local Helm charts under terraform/*/charts/: each chart
# is linted and rendered with its ci/test-values.yaml, and the output is validated
# against the Kubernetes and CRD schemas, so a wrong apiVersion, a misspelt field or
# a template that fails to render fails CI rather than a paid cluster-up (ADR-0002's
# no-cloud tier). Needs helm and kubeconform on PATH; no cluster.
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# Schemas for kinds outside core Kubernetes (the External Secrets Operator's
# SecretStore and ExternalSecret), from the datreeio CRDs catalog pinned to one
# commit so a catalog change cannot move the check. kubeconform fails a kind it has
# no schema for, so a chart adding a CRD kind the catalog lacks fails here.
crd_catalog="https://raw.githubusercontent.com/datreeio/CRDs-catalog/3eb90d47aba8d61919d4419dd38301536e32a94b"
crd_schema_location="${crd_catalog}/{{.Group}}/{{.ResourceKind}}_{{.ResourceAPIVersion}}.json"

shopt -s nullglob
charts=("${repo_root}"/terraform/*/charts/*/)
if [[ ${#charts[@]} -eq 0 ]]; then
  echo "no charts under terraform/*/charts/; the check would pass vacuously" >&2
  exit 1
fi

for chart in "${charts[@]}"; do
  values="${chart}ci/test-values.yaml"
  if [[ ! -f "${values}" ]]; then
    echo "${chart} has no ci/test-values.yaml; every chart must be rendered here" >&2
    exit 1
  fi
  echo "--- ${chart#"${repo_root}/"}"
  helm lint --strict --values "${values}" "${chart}"
  rendered="$(helm template chart-render-test "${chart}" --values "${values}")"
  # kubeconform passes an empty stream, so a chart whose templates all render away
  # under its test values would otherwise pass with nothing checked.
  if ! grep -q '^kind:' <<<"${rendered}"; then
    echo "${chart} renders no resources with ci/test-values.yaml" >&2
    exit 1
  fi
  kubeconform -strict -summary \
    -schema-location default \
    -schema-location "${crd_schema_location}" <<<"${rendered}"
done
