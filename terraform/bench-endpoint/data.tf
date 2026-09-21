# The cluster's network topology (VPC, public subnets, node security group,
# node autoscaling groups) enters as input variables the bench-endpoint-up recipe
# reads from the eks stack's outputs at apply time — not a remote-state read.
# Teardown then destroys this stack from its own state, so bench-endpoint-down needs
# no live eks stack; an interrupted cluster-up that leaves the eks stack without
# outputs can no longer strand this endpoint billing.

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
  name = "${var.cluster_name}-bench-endpoint"

  tags = {
    Project   = "slipstream"
    ManagedBy = "terraform"
    Component = "bench-endpoint"
  }
}
