# Provider and Terraform version pins for the persistent grafana Terraform stack.
# The aws provider reads the Grafana provider's token from Secrets Manager; the grafana
# provider manages the Grafana Cloud data sources and dashboards (ADR-0026).
# required_version >= 1.16, the Terraform version pinned in mise.toml and tested in CI.
terraform {
  required_version = ">= 1.16"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
    grafana = {
      source  = "grafana/grafana"
      version = "~> 4.52"
    }
  }
}
