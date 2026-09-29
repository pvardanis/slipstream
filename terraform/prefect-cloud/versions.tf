# Provider and Terraform version pins for the Prefect Cloud stack.
# required_version >= 1.11 matches the other stacks (GA native S3 state lockfile).
# The prefect provider manages the work pool the EKS worker polls and reads the
# Hobby workspace; the aws provider publishes the API URL + key to Secrets Manager
# so the worker and CI read them from a secret rather than a committed file.
terraform {
  required_version = ">= 1.11"

  required_providers {
    prefect = {
      source  = "prefecthq/prefect"
      version = "~> 3.0"
    }
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
  }
}
