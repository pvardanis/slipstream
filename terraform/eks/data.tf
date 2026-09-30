# Read-only references the Prefect worker layer (worker.tf) needs: the bootstrap stack's
# outputs and the orchestration image it holds. The orchestration ECR repository lives in
# bootstrap because it must outlive `just cluster-down`; the worker reads its URL here and
# resolves the current content-sha tag from ECR to pin the Deployment to an immutable ref.

# The bootstrap stack's outputs, for the orchestration image repository URL. Read only
# when the worker is enabled, so a cluster-up that only wants the base layers (karpenter,
# server) does not depend on bootstrap remote state being readable — and needs no state
# bucket supplied.
data "terraform_remote_state" "bootstrap" {
  count   = local.worker_enabled ? 1 : 0
  backend = "s3"
  config = {
    bucket = var.state_bucket
    key    = "bootstrap/terraform.tfstate"
    region = var.region
  }
}

# The orchestration image's :main pointer resolves to one manifest; the stack pins the
# worker Deployment to that manifest's immutable content-sha tag, never the floating :main
# (a floating tag + IfNotPresent would silently run a stale cached image across nodes). The
# :main pointer is read only to discover the current sha. Read only when the worker is
# enabled — the image need not exist to stand the cluster up.
data "aws_ecr_image" "orchestration" {
  count           = local.worker_enabled ? 1 : 0
  repository_name = local.orchestration_repo_name
  image_tag       = "main"

  # The publish workflow tags every build :main and :<sha>, so the :main manifest carries
  # exactly one non-main tag. Fail the plan loudly if it does not, rather than pinning the
  # worker to a blank or ambiguous tag Helm would resolve to something unintended.
  lifecycle {
    postcondition {
      condition     = length([for t in self.image_tags : t if t != "main"]) == 1
      error_message = "The orchestration :main image must carry exactly one content-sha tag to pin (the publish workflow tags every build :main and :<sha>)."
    }
  }
}
