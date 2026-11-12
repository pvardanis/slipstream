# Read-only references the Prefect worker layer (worker.tf), Image Updater
# (image_updater.tf) and the sweep-dispatch access (sweep_dispatch.tf) need: the bootstrap
# stack's outputs. The orchestration ECR repository lives in bootstrap because it must
# outlive `just cluster-down`; the stack reads its URL and ARN here.

# The bootstrap stack's outputs: the orchestration image repository URL and ARN, the
# sweep-dispatch role ARN, and the cluster name and Prefect namespace the sweep-dispatch
# preconditions check.
data "terraform_remote_state" "bootstrap" {
  backend = "s3"
  config = {
    bucket = var.state_bucket
    key    = "bootstrap/terraform.tfstate"
    region = var.region
  }
}
