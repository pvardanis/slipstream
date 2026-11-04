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
# named after its slug. They are read rather than managed: the dashboards need only the
# Tempo data source's UID and the Prometheus data source's JSON data, which the managed
# Prometheus data source below copies.
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

# The Prometheus data source the dashboards query (ADR-0026, #456 amendment). The hosted
# one is provisioned read-only and links an exemplar to Tempo only through a traceID
# label, while the Collector's span metrics exemplars carry trace_id. This one queries the
# stack's public Prometheus as its instance ID with the metrics:read token, keeps the
# hosted data source's JSON data so queries behave the same, and links trace_id to Tempo.
resource "grafana_data_source" "prometheus" {
  type                = "prometheus"
  name                = "Prometheus"
  url                 = "${data.grafana_cloud_stack.this.prometheus_url}/api/prom"
  basic_auth_enabled  = true
  basic_auth_username = tostring(data.grafana_cloud_stack.this.prometheus_user_id)

  json_data_encoded = jsonencode(merge(
    jsondecode(data.grafana_data_source.prometheus.json_data_encoded),
    {
      exemplarTraceIdDestinations = [{
        name          = "trace_id"
        datasourceUid = data.grafana_data_source.tempo.uid
      }]
    },
  ))

  secure_json_data_encoded = jsonencode({
    basicAuthPassword = data.aws_secretsmanager_secret_version.grafana_metrics_read.secret_string
  })

  lifecycle {
    precondition {
      condition     = can(regex("^https://[a-z0-9-]+\\.grafana\\.net$", data.grafana_cloud_stack.this.prometheus_url))
      error_message = "The Cloud API reports no public https://<host>.grafana.net Prometheus URL for the stack."
    }
  }
}
