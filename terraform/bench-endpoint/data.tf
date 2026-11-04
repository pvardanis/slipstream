# The cluster's network topology (VPC, public subnets, node security group,
# node autoscaling groups) enters as input variables the bench-endpoint-up recipe
# reads from the eks stack's outputs at apply time — not a remote-state read.
# Teardown then destroys this stack from its own state, so bench-endpoint-down needs
# no live eks stack; an interrupted cluster-up that leaves the eks stack without
# outputs can no longer strand this endpoint billing.

# Read-only reference to the bootstrap stack's outputs, for the bench-client image
# the host pulls and the bench-results bucket it writes to. They live in bootstrap
# because they must outlive `just cluster-down` and `bench-endpoint-down`; reading
# them here keeps that ownership while letting the host target them.
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

  # The bootstrap stack's bench-results bucket the host writes to. try() turns a
  # bootstrap applied before the bucket existed into the bench host's precondition
  # failure rather than an unsupported-attribute error.
  bench_results_bucket_name = try(data.terraform_remote_state.bootstrap.outputs.bench_results_bucket_name, "")

  tags = {
    Project   = "slipstream"
    ManagedBy = "terraform"
    Component = "bench-endpoint"
  }
}
