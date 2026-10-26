# Provider and Terraform version pins for the persistent grafana Terraform stack.
# The aws provider reads the bootstrap stack's outputs and the Grafana provider's token;
# the grafana provider manages the Grafana Cloud data sources (ADR-0026).
terraform {
  required_version = ">= 1.11"

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
