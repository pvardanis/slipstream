# Provider and Terraform version pins for the bench endpoint stack.
# required_version >= 1.11 for the GA native S3 state lockfile (use_lockfile)
# and the native `terraform test` framework used under terraform/bench-endpoint/tests.
# The tls provider generates the ephemeral self-signed CA and the server/client
# certificates that back the load balancer's mutual-TLS listener.
terraform {
  required_version = ">= 1.11"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
    tls = {
      source  = "hashicorp/tls"
      version = "~> 4.0"
    }
  }
}
