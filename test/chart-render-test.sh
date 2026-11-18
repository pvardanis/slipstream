#!/usr/bin/env bash
# Static render check for the local Helm charts under terraform/*/charts/: each chart
# is linted and rendered with its ci/test-values.yaml, and the output is validated
# against the Kubernetes and CRD schemas, so a wrong apiVersion, a misspelt field or
# a template that fails to render fails CI rather than a paid cluster-up (ADR-0002's
# no-cloud tier). The root Application's app-of-apps chart (k8s/argocd/apps) gets the
# same lint, render and schema check; each of its templates must render, and each
# Application it renders must carry the shared sync policy. The OTel Collector's chart (k8s/otel-collector), which Argo CD deploys, is linted, rendered
# and validated with model.yaml as its values file, and must stamp model.yaml's model and
# roll its pod on a change to its config or that model. The serving fleet's kustomization
# (k8s/serving), which Argo CD deploys, is rendered and validated, must land in its
# Application's namespace, must leave the replicas of the Deployment its ScaledObject
# scales unset, and must wire its proxy's Envoy config to the proxy's ports, the fleet's
# Service and the wake query.
# Needs helm, kubeconform, kubectl (for kustomize) and yq on PATH; no cluster.
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# Schemas for kinds outside core Kubernetes (the External Secrets Operator's
# SecretStore and ExternalSecret, Argo CD's Application, Image Updater's
# ImageUpdater), from the datreeio CRDs catalog pinned to one commit so a catalog
# change cannot move the check. kubeconform fails a kind it has no schema for, so a
# chart adding a CRD kind the catalog lacks fails here.
crd_catalog="https://raw.githubusercontent.com/datreeio/CRDs-catalog/510ab7f7305427d21ff2bbceecd1ba16eb186c16"
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
apps_rendered="$(helm template chart-render-test "${apps_chart}" --values "${apps_values}")"
# Helm prints a `# Source:` header only for a template that renders something, so a
# child Application gated on a misspelt or missing value shows up as a template with
# no header. NOTES.txt and _-prefixed helpers never render a manifest.
templates="$(cd "${apps_chart}" && find templates -type f ! -name NOTES.txt ! -name '_*' | sort)"
sources="$(sed -n 's|^# Source: [^/]*/||p' <<<"${apps_rendered}" | sort -u)"
if [[ "${templates}" != "${sources}" ]]; then
  echo "${apps_chart} has templates that render nothing with ci/test-values.yaml:" >&2
  comm -23 <(printf '%s\n' "${templates}") <(printf '%s\n' "${sources}") >&2
  exit 1
fi
validate_schemas <<<"${apps_rendered}"
# Argo CD's sync policy is per Application, so each child must carry the shared one
# itself: automated prune and selfHeal, so a merged change syncs without an operator, and
# ServerSideApply, without which a sync that drops a field such as replicas resets it.
app_names="$(yq -N 'select(.kind == "Application") | .metadata.name' <<<"${apps_rendered}")"
if [[ -z "${app_names}" ]]; then
  echo "${apps_chart} renders no Applications with ci/test-values.yaml" >&2
  exit 1
fi
without_sync_policy="$(yq -N 'select(.kind == "Application" and ((.spec.syncPolicy.automated.prune == true and .spec.syncPolicy.automated.selfHeal == true and ((.spec.syncPolicy.syncOptions // []) | any_c(. == "ServerSideApply=true"))) | not)) | .metadata.name' <<<"${apps_rendered}")"
if [[ -n "${without_sync_policy}" ]]; then
  echo "${apps_chart} renders Applications without automated prune and selfHeal and ServerSideApply=true in syncPolicy:" >&2
  echo "${without_sync_policy}" >&2
  exit 1
fi

# The OTel Collector's chart (k8s/otel-collector), an Argo CD child app, rendered as its
# Application renders it: the chart at the Application's path, with its values file,
# which Argo CD resolves from that path and which must be model.yaml. The served model
# reaches the Collector as the ConfigMap's model-id, and the pod template carries a
# checksum of that ConfigMap, so a merged change to the config or the model rolls the
# pod: the Collector reads both only at start.
collector_app="$(yq 'select(.kind == "Application" and .metadata.name == "otel-collector")' <<<"${apps_rendered}")"
if [[ -z "${collector_app}" ]]; then
  echo "${apps_chart} renders no otel-collector Application" >&2
  exit 1
fi
# Prints a field of the otel-collector Application, and fails naming the field when the
# Application has none.
get_collector_app_field() {
  yq -e "$1" <<<"${collector_app}" || {
    echo "the otel-collector Application has no $1" >&2
    return 1
  }
}
collector_chart="${repo_root}/$(get_collector_app_field '.spec.source.path')"
value_file="$(get_collector_app_field '.spec.source.helm.valueFiles[0]')"
model_values="$(cd "${collector_chart}/$(dirname "${value_file}")" && pwd)/$(basename "${value_file}")"
if [[ "${model_values}" != "${repo_root}/model.yaml" ]]; then
  echo "the otel-collector Application's values file resolves to ${model_values}, not model.yaml" >&2
  exit 1
fi
echo "--- ${collector_chart#"${repo_root}/"}"
helm lint --strict --values "${model_values}" "${collector_chart}"
rendered="$(helm template chart-render-test "${collector_chart}" --values "${model_values}")"
validate_schemas <<<"${rendered}"
# Argo CD applies the chart into the Application's destination namespace; a manifest
# naming another namespace would land outside it. Every manifest in the chart names its
# namespace, so one without a namespace fails here too.
destination="$(get_collector_app_field '.spec.destination.namespace')"
elsewhere="$(yq -N "select(.metadata.namespace != \"${destination}\") | .kind + \"/\" + .metadata.name" <<<"${rendered}")"
if [[ -n "${elsewhere}" ]]; then
  echo "${collector_chart} renders manifests outside its Application's destination namespace ${destination}:" >&2
  echo "${elsewhere}" >&2
  exit 1
fi
model_id="$(yq '.model.hfId' "${model_values}")"
rendered_model_id="$(yq 'select(.kind == "ConfigMap" and .metadata.name == "otel-collector-config") | .data["model-id"]' <<<"${rendered}")"
if [[ "${rendered_model_id}" != "${model_id}" ]]; then
  echo "${collector_chart} gives the Collector model-id '${rendered_model_id}', not model.yaml's hfId '${model_id}'" >&2
  exit 1
fi

# Prints the Collector pod template's config checksum from the chart rendered with the
# values file given, and fails when the chart does not render or carries none.
get_config_checksum() {
  local chart="$1" values="$2" checksum
  checksum="$(helm template chart-render-test "${chart}" --values "${values}" |
    yq 'select(.kind == "Deployment") | .spec.template.metadata.annotations["checksum/config"]')"
  if [[ ! "${checksum}" =~ ^[0-9a-f]{64}$ ]]; then
    echo "${chart}'s pod template has no checksum/config of its ConfigMap, so a config change would not roll the Collector: '${checksum}'" >&2
    return 1
  fi
  echo "${checksum}"
}
checksum="$(get_config_checksum "${collector_chart}" "${model_values}")"
tmp="$(mktemp -d)"
trap 'rm -rf "${tmp}"' EXIT
printf 'model:\n  hfId: other-org/other-model\n' >"${tmp}/other-model.yaml"
other_model_checksum="$(get_config_checksum "${collector_chart}" "${tmp}/other-model.yaml")"
if [[ "${other_model_checksum}" == "${checksum}" ]]; then
  echo "${collector_chart}'s checksum/config does not change with the model, so a model change would not roll the Collector" >&2
  exit 1
fi
cp -R "${collector_chart}" "${tmp}/chart"
echo '# a merged config change' >>"${tmp}/chart/config.yaml"
other_config_checksum="$(get_config_checksum "${tmp}/chart" "${model_values}")"
if [[ "${other_config_checksum}" == "${checksum}" ]]; then
  echo "${collector_chart}'s checksum/config does not change with config.yaml, so a config change would not roll the Collector" >&2
  exit 1
fi

# The serving fleet's child Application (ADR-0031), rendered as Argo CD renders it: the
# kustomization at the Application's path. The fleet, its proxy and its ScaledObject
# must validate, land in the Application's destination namespace, and leave the
# replicas of the Deployment the ScaledObject scales unset, so a sync never resets the
# count KEDA owns. Server-side apply, without which a sync that drops a field resets it,
# is checked with the shared sync policy above.
serving_app="$(yq 'select(.kind == "Application" and .metadata.name == "serving")' <<<"${apps_rendered}")"
if [[ -z "${serving_app}" ]]; then
  echo "${apps_chart} renders no serving Application" >&2
  exit 1
fi
serving_path="$(yq -e '.spec.source.path' <<<"${serving_app}")"
serving_destination="$(yq -e '.spec.destination.namespace' <<<"${serving_app}")"
if [[ "${serving_path}" != "k8s/serving" || "${serving_destination}" != "serving" ]]; then
  echo "the serving Application must sync k8s/serving into the serving namespace, not ${serving_path} into ${serving_destination}" >&2
  exit 1
fi
echo "--- ${serving_path}"
serving_rendered="$(kubectl kustomize "${repo_root}/${serving_path}")"
validate_schemas <<<"${serving_rendered}"
elsewhere="$(yq -N "select(.metadata.namespace != \"${serving_destination}\") | .kind + \"/\" + .metadata.name" <<<"${serving_rendered}")"
if [[ -n "${elsewhere}" ]]; then
  echo "${serving_path} renders manifests outside its Application's destination namespace ${serving_destination}:" >&2
  echo "${elsewhere}" >&2
  exit 1
fi
scaled_target="$(yq -N 'select(.kind == "ScaledObject") | .spec.scaleTargetRef.name' <<<"${serving_rendered}")"
scaled_deployment="$(yq -N "select(.kind == \"Deployment\" and .metadata.name == \"${scaled_target}\")" <<<"${serving_rendered}")"
if [[ -z "${scaled_target}" || -z "${scaled_deployment}" ]]; then
  echo "${serving_path} renders no ScaledObject whose scaleTargetRef names a Deployment it renders: '${scaled_target}'" >&2
  exit 1
fi
if [[ "$(yq '.spec | has("replicas")' <<<"${scaled_deployment}")" != "false" ]]; then
  echo "${serving_path}'s Deployment ${scaled_target} sets replicas, which a sync would reset under KEDA" >&2
  exit 1
fi

# The proxy's wiring, each value held in two files and read here from the render: its
# config volume names the ConfigMap kustomize renders with Envoy's config, its Envoy
# listeners are its container ports, its readiness probe is a path the listener on its
# metrics port serves, Envoy's upstream is the fleet's Service, and the listener on its
# http port counts requests under the conn manager prefix the wake query reads.
proxy="$(yq -N 'select(.kind == "Deployment" and .metadata.name == "serving-proxy")' <<<"${serving_rendered}")"
if [[ -z "${proxy}" ]]; then
  echo "${serving_path} renders no serving-proxy Deployment" >&2
  exit 1
fi
proxy_config_map="$(yq '.spec.template.spec.volumes[] | select(.name == "config") | .configMap.name' <<<"${proxy}")"
envoy_config="$(yq -N "select(.kind == \"ConfigMap\" and .metadata.name == \"${proxy_config_map}\") | .data[\"envoy.yaml\"]" <<<"${serving_rendered}")"
if [[ -z "${envoy_config}" ]]; then
  echo "the serving-proxy's config volume names ConfigMap '${proxy_config_map}', which ${serving_path} does not render with an envoy.yaml" >&2
  exit 1
fi
# Prints the port of the proxy's container port named $1.
get_proxy_port() {
  yq ".spec.template.spec.containers[0].ports[] | select(.name == \"$1\") | .containerPort" <<<"${proxy}"
}
# Prints the Envoy listener on port $1.
get_envoy_listener() {
  yq ".static_resources.listeners[] | select(.address.socket_address.port_value == $1)" <<<"${envoy_config}"
}
listener_ports="$(yq '[.static_resources.listeners[].address.socket_address.port_value] | sort | .[]' <<<"${envoy_config}")"
container_ports="$(yq '[.spec.template.spec.containers[0].ports[].containerPort] | sort | .[]' <<<"${proxy}")"
if [[ "${listener_ports}" != "${container_ports}" ]]; then
  echo "the serving-proxy's Envoy listens on ports $(echo "${listener_ports}" | xargs), not its container ports $(echo "${container_ports}" | xargs)" >&2
  exit 1
fi
probe="$(yq '.spec.template.spec.containers[0].readinessProbe.httpGet | .port + " " + .path' <<<"${proxy}")"
metrics_paths="$(get_envoy_listener "$(get_proxy_port metrics)" | yq '.. | select(has("match")) | .match.path')"
if [[ "${probe%% *}" != "metrics" ]] || ! grep -qxF "${probe#* }" <<<"${metrics_paths}"; then
  echo "the serving-proxy's readiness probe '${probe}' is not a path its Envoy serves on the metrics port: $(echo "${metrics_paths}" | xargs)" >&2
  exit 1
fi
fleet_service="$(yq -N "select(.kind == \"Service\" and .metadata.name == \"${scaled_target}\") | .metadata.name + \".\" + .metadata.namespace + \".svc.cluster.local:\" + (.spec.ports[0].port | tostring)" <<<"${serving_rendered}")"
upstream="$(yq '.static_resources.clusters[] | select(.type == "STRICT_DNS") | .load_assignment.endpoints[].lb_endpoints[].endpoint.address.socket_address | .address + ":" + (.port_value | tostring)' <<<"${envoy_config}")"
if [[ -z "${fleet_service}" || "${upstream}" != "${fleet_service}" ]]; then
  echo "the serving-proxy's Envoy forwards to '${upstream}', not the fleet's Service '${fleet_service}'" >&2
  exit 1
fi
ingress_prefix="$(get_envoy_listener "$(get_proxy_port http)" | yq '.. | select(has("stat_prefix")) | .stat_prefix')"
wake_query="$(yq -N 'select(.kind == "ScaledObject") | .spec.triggers[] | select(.name == "wake") | .metadata.query' <<<"${serving_rendered}")"
if [[ -z "${ingress_prefix}" || "${wake_query}" != *"envoy_http_conn_manager_prefix=\"${ingress_prefix}\""* ]]; then
  echo "the wake query does not count the serving-proxy's http listener, stat_prefix '${ingress_prefix}': ${wake_query}" >&2
  exit 1
fi
