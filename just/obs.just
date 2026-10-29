# Observability lifecycle: the persistent grafana Terraform stack, and the OTel Collector deploy, teardown, and trace pivot.

# Deploy the OTel Collector to the cluster. It exports to Grafana Cloud through the
# grafana-cloud-endpoints ConfigMap `just cluster-up` creates in slipstream and the
# grafana-cloud-write Secret ESO syncs there, and does not start without them, so
# both are checked for, the Secret waited for, before the deploy.
[group('obs')]
obs-up:
    #!/usr/bin/env bash
    set -euo pipefail
    {{ otel_check_tool }} preflight
    kubectl create namespace slipstream --dry-run=client -o yaml | kubectl apply -f -
    # The Collector stamps model_id on vLLM spans from model-id; model.yaml is its
    # source of truth, as for the serving manifest.
    model="$({{ image_tag_tool }} hf-id)"
    kubectl create configmap otel-collector-config -n slipstream \
      --from-file=config.yaml={{ otel_config }} \
      --from-literal=model-id="${model}" \
      --dry-run=client -o yaml | kubectl apply -f -
    kubectl apply -f {{ otel_manifests }}
    # The Collector reads config.yaml and its env only at start, and an unchanged
    # Deployment does not roll; restart so a changed config or model takes effect.
    kubectl -n slipstream rollout restart deploy/otel-collector
    {{ otel_check_tool }} rollout

# Remove the OTel Collector (leaves the cluster running).
[group('obs')]
obs-down:
    kubectl delete configmap otel-collector-config -n slipstream --ignore-not-found
    kubectl delete -f {{ otel_manifests }} --ignore-not-found

# Send one shape-only OTLP trace through the cluster collector, check Grafana Cloud's
# endpoints accept the Collector's spans and span metrics, and show the trace's request_id
# log line. Takes up to 95 s, for the span metrics' 60 s flush.
[group('obs')]
obs-pivot:
    #!/usr/bin/env bash
    set -euo pipefail
    {{ otel_check_tool }} rollout
    # The Collector's metrics port goes to local 18888, clear of a local Collector's 8888.
    # port-forward keeps running when only some ports bind, so the metrics port's line is
    # waited for beside the receiver answering.
    pf_log="$(mktemp)"
    kubectl -n slipstream port-forward svc/otel-collector 4318:4318 18888:8888 >"${pf_log}" 2>&1 &
    pf_pid=$!
    # Disowned, so killing it on exit prints no job notice.
    disown "${pf_pid}"
    trap 'kill "${pf_pid}" 2>/dev/null || true; rm -f "${pf_log}"' EXIT
    ready=""
    for _ in $(seq 30); do
      if ! kill -0 "${pf_pid}" 2>/dev/null || grep -q 'Unable to listen' "${pf_log}"; then
        echo "the port-forward to svc/otel-collector failed:" >&2
        cat "${pf_log}" >&2
        exit 1
      fi
      # A port that failed to bind is reported before the metrics port's line, so it is
      # read again once that line is in.
      if grep -q -- '-> 8888' "${pf_log}" && ! grep -q 'Unable to listen' "${pf_log}" &&
        curl -s --max-time 5 -o /dev/null -X POST http://localhost:4318/v1/traces \
        -H 'Content-Type: application/json' -d '{}'; then
        ready=1
        break
      fi
      sleep 1
    done
    if [[ -z "${ready}" ]]; then
      echo "port-forward to svc/otel-collector never came up:" >&2
      cat "${pf_log}" >&2
      exit 1
    fi
    # The exporters' counts before the demo trace, so only what is sent after it counts.
    if ! baseline="$(curl -sSf --max-time 5 http://localhost:18888/metrics)"; then
      echo "cannot read the Collector's metrics through the port-forward to svc/otel-collector" >&2
      exit 1
    fi
    # The demo trace's model_id names the served model; read it from model.yaml, the
    # source of truth the serving manifest is held to.
    model="$({{ image_tag_tool }} hf-id)"
    # Sent as service obs-pivot, so span metrics count it apart from vLLM's requests.
    payload="$({{ otel_check_tool }} demo-span "${model}")"
    if ! curl -sSf --max-time 5 -o /dev/null -X POST http://localhost:4318/v1/traces \
      -H 'Content-Type: application/json' -d "${payload}"; then
      echo "the Collector's OTLP receiver did not take the demo span" >&2
      exit 1
    fi
    {{ otel_check_tool }} delivery http://localhost:18888/metrics <<<"${baseline}"
    logs="$(kubectl -n slipstream logs deploy/otel-collector)"
    if ! grep -A12 'req-demo-001' <<<"${logs}"; then
      echo "the Collector's log lacks the demo span's request_id req-demo-001: check the debug exporter in {{ otel_config }}" >&2
      exit 1
    fi

# The Terraform stack outlives `just cluster-down`, so it has no down recipe. Requires
# `just grafana-cloud-setup` done.
# Apply the persistent grafana Terraform stack: its data sources (ADR-0026).
[group('obs')]
grafana-up: _bootstrap-init
    #!/usr/bin/env bash
    set -euo pipefail
    # Bare assignment so a failed lookup aborts rather than feeding an empty bucket
    # into terraform (see `cluster-up`).
    bucket="$(terraform -chdir={{ bootstrap_dir }} output -raw state_bucket_name)"
    [[ -n "${bucket}" ]] || { echo "bootstrap output state_bucket_name is empty" >&2; exit 1; }
    terraform -chdir={{ grafana_dir }} init -input=false -backend-config="bucket=${bucket}"
    # No -auto-approve: the Terraform stack persists across sessions, so its plan is
    # read before it changes what Grafana Cloud holds.
    terraform -chdir={{ grafana_dir }} apply -var="state_bucket=${bucket}"
