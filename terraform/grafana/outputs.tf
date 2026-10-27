# Outputs from the persistent grafana Terraform stack: the UIDs of the data sources, the
# identifier a dashboard panel references its data source by, and the stack URL.
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
