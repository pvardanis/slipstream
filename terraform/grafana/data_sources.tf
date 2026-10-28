# The data sources the dashboards query, and the stack's public send endpoints as the
# Cloud API reports them (ADR-0026).

# CloudWatch through Grafana Assume Role: Grafana Cloud assumes the bootstrap stack's
# read-only IAM role, so no AWS key is stored in Grafana. A spend panel reads AWS/Usage
# through it, so it keeps working while the cluster is down.
resource "grafana_data_source" "cloudwatch" {
  type = "cloudwatch"
  name = "CloudWatch"

  json_data_encoded = jsonencode({
    authType      = "grafana_assume_role"
    assumeRoleArn = data.terraform_remote_state.bootstrap.outputs.grafana_cloudwatch_role_arn
    defaultRegion = data.terraform_remote_state.bootstrap.outputs.region
  })
}

# Grafana Cloud provisions the hosted Prometheus and Tempo data sources with every stack,
# named after its slug. They are read rather than managed: a managed copy would need a
# metrics:read and traces:read token of its own, and the dashboards need only their UIDs.
# var.grafana_url's validation guarantees the https://<stack>.grafana.net shape.
locals {
  stack_slug = split(".", trimprefix(var.grafana_url, "https://"))[0]
}

data "grafana_data_source" "prometheus" {
  name = "grafanacloud-${local.stack_slug}-prom"
}

data "grafana_data_source" "tempo" {
  name = "grafanacloud-${local.stack_slug}-traces"
}

# The stack as the Cloud API reports it: the public endpoints Prometheus remote_write and
# the Collector send to, and the instance IDs they authenticate as. The hosted data
# sources above name Grafana's internal hosts instead, which resolve to private addresses
# outside Grafana Cloud.
data "grafana_cloud_stack" "this" {
  slug = local.stack_slug
}
