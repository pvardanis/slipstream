# Where the OpenTelemetry Collector (k8s/otel-collector.yaml, deployed by `just obs-up`)
# sends to Grafana Cloud: traces over OTLP to the hosted Tempo and span metrics by
# remote_write to the hosted Prometheus. Published as a ConfigMap in the Collector's
# namespace from the grafana stack's outputs, so `just obs-up` needs no Terraform state of
# its own and nothing is copied by hand. The write token both authenticate with is the
# grafana-cloud-write Secret ESO syncs into the same namespace (external_secrets.tf).

locals {
  # try() turns a grafana stack applied before it published these outputs into the
  # ConfigMap's actionable precondition failure below.
  tempo_otlp_endpoint = try(data.terraform_remote_state.grafana.outputs.tempo_otlp_endpoint, "")
  tempo_username      = try(data.terraform_remote_state.grafana.outputs.tempo_username, "")
}

resource "kubernetes_config_map_v1" "grafana_cloud_endpoints" {
  metadata {
    name      = "grafana-cloud-endpoints"
    namespace = kubernetes_namespace_v1.slipstream.metadata[0].name
  }

  # The Prometheus push URL and username are the ones remote_write uses (monitoring.tf),
  # whose release's precondition already rejects them missing.
  data = {
    traces-endpoint  = local.tempo_otlp_endpoint
    traces-username  = local.tempo_username
    metrics-url      = local.prometheus_remote_write_url
    metrics-username = local.prometheus_remote_write_username
  }

  lifecycle {
    precondition {
      condition     = local.tempo_otlp_endpoint != "" && local.tempo_username != ""
      error_message = "The grafana stack has no tempo_otlp_endpoint or no tempo_username output, so the Collector has nowhere to export traces. Run `just grafana-up`, then `just cluster-up` again."
    }
  }
}
