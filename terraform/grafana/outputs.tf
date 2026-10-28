# Outputs from the persistent grafana Terraform stack: the UIDs of the data sources, the
# identifier a dashboard panel references its data source by, the stack URL, and the
# hosted Prometheus remote_write and Tempo OTLP targets.
output "prometheus_data_source_uid" {
  description = "UID of the stack's hosted Prometheus data source, grafanacloud-<stack>-prom."
  value       = data.grafana_data_source.prometheus.uid
}

output "tempo_data_source_uid" {
  description = "UID of the stack's hosted Tempo data source, grafanacloud-<stack>-traces."
  value       = data.grafana_data_source.tempo.uid
}

output "cloudwatch_data_source_uid" {
  description = "UID of the CloudWatch data source that reads through the bootstrap stack's Grafana Assume Role IAM role."
  value       = grafana_data_source.cloudwatch.uid
}

# Read by the Grafana MCP server launcher (scripts/grafana-mcp.sh), so a worktree without
# this stack's gitignored terraform.tfvars still finds the stack.
output "grafana_url" {
  description = "URL of the Grafana Cloud stack this stack manages, the Grafana MCP server's GRAFANA_URL."
  value       = var.grafana_url
}

# Read by the eks stack's Prometheus remote_write (ADR-0026) and its Collector endpoints
# ConfigMap. Each must be a public <host>.grafana.net endpoint: a Grafana-internal host
# such as <host>-local-nlb.internal.grafana.net resolves to a private address no sender
# can reach.
output "prometheus_remote_write_url" {
  description = "Grafana Cloud push endpoint for the hosted Prometheus, the eks stack's remote_write URL."
  value       = data.grafana_cloud_stack.this.prometheus_remote_write_endpoint

  precondition {
    condition     = can(regex("^https://[a-z0-9-]+\\.grafana\\.net/", data.grafana_cloud_stack.this.prometheus_remote_write_endpoint))
    error_message = "The Cloud API reports no public https://<host>.grafana.net Prometheus remote_write endpoint for the stack."
  }
}

output "prometheus_remote_write_username" {
  description = "Prometheus instance ID of the Grafana Cloud stack, the eks stack's remote_write basic-auth username."
  value       = tostring(data.grafana_cloud_stack.this.prometheus_user_id)

  # The Cloud API reports an unset instance ID as 0, a username Grafana Cloud rejects.
  precondition {
    condition     = data.grafana_cloud_stack.this.prometheus_user_id > 0
    error_message = "The Cloud API reports no Prometheus instance ID for the stack."
  }
}

# Grafana Cloud takes OTLP gRPC at the traces URL's host on port 443.
output "tempo_otlp_endpoint" {
  description = "Grafana Cloud OTLP gRPC endpoint for the hosted Tempo, host:443, the Collector's trace exporter endpoint."
  value       = "${regex("^https://([^/]+)", data.grafana_cloud_stack.this.traces_url)[0]}:443"

  precondition {
    condition     = can(regex("^https://[a-z0-9-]+\\.grafana\\.net(/|$)", data.grafana_cloud_stack.this.traces_url))
    error_message = "The Cloud API reports no public https://<host>.grafana.net traces URL for the stack."
  }
}

output "tempo_username" {
  description = "Tempo instance ID of the Grafana Cloud stack, the Collector's trace exporter basic-auth username."
  value       = tostring(data.grafana_cloud_stack.this.traces_user_id)

  # The Cloud API reports an unset instance ID as 0, a username Grafana Cloud rejects.
  precondition {
    condition     = data.grafana_cloud_stack.this.traces_user_id > 0
    error_message = "The Cloud API reports no Tempo instance ID for the stack."
  }
}
