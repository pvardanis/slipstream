# Provider and Terraform version pins for the eks stack.
# required_version >= 1.11 for the GA native S3 state lockfile (use_lockfile).
terraform {
  required_version = ">= 1.11"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
    # Installs the Karpenter controller chart; v3 for the nested `kubernetes = {}`
    # provider config the eks module's Karpenter path expects.
    helm = {
      source  = "hashicorp/helm"
      version = "~> 3.0"
    }
  }
}
