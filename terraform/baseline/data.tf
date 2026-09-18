# Read-only reference to the eks stack's outputs. Cross-stack via remote state
# keeps the two stacks in separate state files: nothing the baseline does can
# mutate the cluster, and baseline-down destroys only this stack. The bucket is
# supplied at apply time (same value the eks stack itself uses); the key is the
# eks stack's fixed state key.
data "terraform_remote_state" "eks" {
  backend = "s3"
  config = {
    bucket = var.state_bucket
    key    = "eks/terraform.tfstate"
    region = var.region
  }
}

# Read-only reference to the bootstrap stack's outputs, for the bench-client
# image the host pulls and the ECR repository ARN its pull policy is scoped to.
# The repository lives in bootstrap because it must outlive `just cluster-down`; reading
# it here keeps that ownership while letting the host target it.
data "terraform_remote_state" "bootstrap" {
  backend = "s3"
  config = {
    bucket = var.state_bucket
    key    = "bootstrap/terraform.tfstate"
    region = var.region
  }
}

locals {
  eks = data.terraform_remote_state.eks.outputs

  name = "${var.cluster_name}-baseline"

  tags = {
    Project   = "slipstream"
    ManagedBy = "terraform"
    Component = "baseline"
  }
}
