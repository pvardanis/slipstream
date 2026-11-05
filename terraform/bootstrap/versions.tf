# Provider and Terraform version pins for the state-bootstrap stack.
# required_version >= 1.17, the Terraform version pinned in mise.toml and tested in CI.
terraform {
  required_version = ">= 1.17"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.0"
    }
  }
}
