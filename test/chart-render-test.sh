#!/usr/bin/env bash
# Static render check for the local Helm charts under terraform/*/charts/: each chart
# is linted and rendered with its ci/test-values.yaml, and the output is validated
# against the Kubernetes and CRD schemas, so a wrong apiVersion, a misspelt field or
# a template that fails to render fails CI rather than a paid cluster-up (ADR-0002's
# no-cloud tier). The root Application's app-of-apps chart (k8s/argocd/apps) gets the
# same lint, render and schema check, except that an empty render passes: a chart with
# no child Applications is a valid root. Each of its templates must still render. The
# OTel Collector's chart (k8s/otel-collector), which Argo CD deploys, is linted, rendered
# and validated with model.yaml as its values file, and must stamp model.yaml's model and
# roll its pod on a change to its config or that model.
# Needs helm, kubeconform and yq on PATH; no cluster.
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# Schemas for kinds outside core Kubernetes (the External Secrets Operator's
# SecretStore and ExternalSecret, Argo CD's Application), from the datreeio CRDs
# catalog pinned to one commit so a catalog change cannot move the check. kubeconform
# fails a kind it has no schema for, so a chart adding a CRD kind the catalog lacks
# fails here.
crd_catalog="https://raw.githubusercontent.com/datreeio/CRDs-catalog/3eb90d47aba8d61919d4419dd38301536e32a94b"
crd_schema_location="${crd_catalog}/{{.Group}}/{{.ResourceKind}}_{{.ResourceAPIVersion}}.json"

# Validates the rendered manifests on stdin against the Kubernetes and CRD schemas.
validate_schemas() {
  kubeconform -strict -summary \
    -schema-location default \
    -schema-location "${crd_schema_location}"
}

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
  validate_schemas <<<"${rendered}"
done

# The app-of-apps chart Argo CD syncs for the root Application, linted and rendered with
# the cluster values the eks stack passes it (ci/test-values.yaml), so a broken Chart.yaml
# or a template that fails on those values fails CI rather than the root Application's
# sync. Its child Applications validate against the Argo CD CRD schema in the catalog.
apps_chart="${repo_root}/k8s/argocd/apps"
apps_values="${apps_chart}/ci/test-values.yaml"
echo "--- ${apps_chart#"${repo_root}/"}"
helm lint --strict --values "${apps_values}" "${apps_chart}"
rendered="$(helm template chart-render-test "${apps_chart}" --values "${apps_values}")"
# Helm prints a `# Source:` header only for a template that renders something, so a
# child Application gated on a misspelt or missing value shows up as a template with
# no header. NOTES.txt and _-prefixed helpers never render a manifest.
templates="$(cd "${apps_chart}" && find templates -type f ! -name NOTES.txt ! -name '_*' | sort)"
sources="$(sed -n 's|^# Source: [^/]*/||p' <<<"${rendered}" | sort -u)"
if [[ "${templates}" != "${sources}" ]]; then
  echo "${apps_chart} has templates that render nothing with ci/test-values.yaml:" >&2
  comm -23 <(printf '%s\n' "${templates}") <(printf '%s\n' "${sources}") >&2
  exit 1
fi
validate_schemas <<<"${rendered}"

# The OTel Collector's chart (k8s/otel-collector), an Argo CD child app, rendered as its
# Application renders it: with model.yaml as its values file. The served model reaches
# the Collector as the ConfigMap's model-id, and the pod template carries a checksum of
# that ConfigMap, so a merged change to the config or the model rolls the pod: the
# Collector reads both only at start.
collector_chart="${repo_root}/k8s/otel-collector"
model_values="${repo_root}/model.yaml"
echo "--- ${collector_chart#"${repo_root}/"}"
helm lint --strict --values "${model_values}" "${collector_chart}"
rendered="$(helm template chart-render-test "${collector_chart}" --values "${model_values}")"
validate_schemas <<<"${rendered}"
model_id="$(yq '.model.hfId' "${model_values}")"
rendered_model_id="$(yq 'select(.kind == "ConfigMap" and .metadata.name == "otel-collector-config") | .data["model-id"]' <<<"${rendered}")"
if [[ "${rendered_model_id}" != "${model_id}" ]]; then
  echo "${collector_chart} gives the Collector model-id '${rendered_model_id}', not model.yaml's hfId '${model_id}'" >&2
  exit 1
fi

# Prints the Collector pod template's config checksum from the chart rendered with the
# values files given.
get_config_checksum() {
  local chart="$1"
  shift
  helm template chart-render-test "${chart}" "${@/#/--values=}" |
    yq 'select(.kind == "Deployment") | .spec.template.metadata.annotations["checksum/config"]'
}
checksum="$(get_config_checksum "${collector_chart}" "${model_values}")"
if [[ ! "${checksum}" =~ ^[0-9a-f]{64}$ ]]; then
  echo "${collector_chart}'s pod template has no checksum/config of its ConfigMap, so a config change would not roll the Collector: '${checksum}'" >&2
  exit 1
fi
tmp="$(mktemp -d)"
trap 'rm -rf "${tmp}"' EXIT
printf 'model:\n  hfId: other-org/other-model\n' >"${tmp}/other-model.yaml"
if [[ "$(get_config_checksum "${collector_chart}" "${tmp}/other-model.yaml")" == "${checksum}" ]]; then
  echo "${collector_chart}'s checksum/config does not change with the model, so a model change would not roll the Collector" >&2
  exit 1
fi
cp -R "${collector_chart}" "${tmp}/chart"
echo '# a merged config change' >>"${tmp}/chart/config.yaml"
if [[ "$(get_config_checksum "${tmp}/chart" "${model_values}")" == "${checksum}" ]]; then
  echo "${collector_chart}'s checksum/config does not change with config.yaml, so a config change would not roll the Collector" >&2
  exit 1
fi
