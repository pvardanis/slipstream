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
    # Creates the default gp3 StorageClass the EBS CSI driver provisions against
    # (the cluster ships no default class), so the Prefect PVC binds (#185).
    # Held below 3.4: under CI's Terraform 1.11.4, 3.4.0 leaves the worker Role's
    # rules unknown at plan, so tests/worker.tftest.hcl cannot evaluate its RBAC asserts.
    kubernetes = {
      source  = "hashicorp/kubernetes"
      version = "~> 3.2, < 3.4"
    }
  }
}
