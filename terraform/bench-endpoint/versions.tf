# Provider and Terraform version pins for the bench endpoint stack.
# required_version >= 1.16, the Terraform version pinned in .tool-versions and tested in
# CI; this also covers the GA native S3 state lockfile (use_lockfile, 1.11+) and the
# native `terraform test` framework used under terraform/bench-endpoint/tests.
# The tls provider generates the ephemeral self-signed CA and the server/client
# certificates that back the load balancer's mutual-TLS listener.
terraform {
  required_version = ">= 1.16"

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
