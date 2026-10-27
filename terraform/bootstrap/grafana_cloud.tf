# Grafana Cloud's footing in AWS (ADR-0026): the Secrets Manager secrets its tokens live
# in, and the Grafana Assume Role IAM role its CloudWatch data source reads through. They
# live in the bootstrap stack because they must exist before anything consumes them and
# outlive `just cluster-down`, like the dashboards and history they back.

# Both secrets are created empty: no secret version is managed here, so no token enters
# Terraform state. scripts/grafana-cloud-setup.sh has the operator mint each token in
# Grafana Cloud and writes it with `aws secretsmanager put-secret-value`.

# One access-policy token scoped metrics:write and traces:write, for Prometheus
# remote_write and the Collector's OTLP exporter (ADR-0026).
resource "aws_secretsmanager_secret" "grafana_cloud_write" {
  name        = "grafana-cloud/write"
  description = "Grafana Cloud access-policy token scoped metrics:write and traces:write, for Prometheus remote_write and the OTel Collector."
}

# The Grafana Terraform provider's token, for the terraform/grafana stack (ADR-0026).
resource "aws_secretsmanager_secret" "grafana_cloud_api" {
  name        = "grafana-cloud/api"
  description = "Grafana Cloud token for the Grafana Terraform provider that pushes the dashboards."
}

# Grafana Assume Role: Grafana Cloud's CloudWatch data source assumes this role from
# Grafana's shared AWS account, so no long-lived AWS key exists. That account serves every
# Grafana Cloud customer, so the trust also requires this stack's external ID, which
# Grafana sends only for this stack's own data source requests.
resource "aws_iam_role" "grafana_cloudwatch" {
  name        = var.grafana_cloudwatch_role_name
  description = "Assumed by Grafana Cloud's CloudWatch data source (Grafana Assume Role) to read CloudWatch metrics."
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { AWS = "arn:aws:iam::${var.grafana_account_id}:root" }
      Action    = "sts:AssumeRole"
      Condition = {
        StringEquals = { "sts:ExternalId" = var.grafana_external_id }
      }
    }]
  })
}

# Read-only CloudWatch metrics: ListMetrics fills the data source's query editor and
# GetMetricData runs the queries, such as the spend panel's AWS/Usage G-class vCPUs. Neither
# action takes a resource scope, so the grant is on "*". No logs actions are granted, so the
# data source's health check reports its logs half as failing while metrics queries work.
resource "aws_iam_role_policy" "grafana_cloudwatch" {
  name = "cloudwatch-metrics-read"
  role = aws_iam_role.grafana_cloudwatch.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid      = "ReadCloudWatchMetrics"
      Effect   = "Allow"
      Action   = ["cloudwatch:GetMetricData", "cloudwatch:ListMetrics"]
      Resource = "*"
    }]
  })
}
