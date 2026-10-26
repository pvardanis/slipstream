# Outputs from the persistent Grafana Cloud stack: the UIDs dashboards reference their
# data sources by.
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
