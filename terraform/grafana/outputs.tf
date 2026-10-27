# Outputs from the persistent grafana Terraform stack: the UIDs of the data sources, the
# identifier a dashboard panel references its data source by, the stack URL, and the
# hosted Prometheus remote_write target.
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

# Read by the eks stack's Prometheus remote_write (ADR-0026). Grafana Cloud's push
# endpoint sits at /push under the hosted data source's query URL, and the data source's
# basic-auth user is the stack's Prometheus instance ID, the username the write token
# authenticates as.
output "prometheus_remote_write_url" {
  description = "Grafana Cloud push endpoint for the hosted Prometheus, the eks stack's remote_write URL."
  value       = "${data.grafana_data_source.prometheus.url}/push"
}

output "prometheus_remote_write_username" {
  description = "Prometheus instance ID of the Grafana Cloud stack, the eks stack's remote_write basic-auth username."
  value       = data.grafana_data_source.prometheus.basic_auth_username
}
