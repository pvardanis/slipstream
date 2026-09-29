# The Prefect Cloud control plane for the unattended knob sweep
# (ADR-0015-unattended-sweep-cloud-server-and-parent-flow): the hosted workspace the
# sweep is observed from and the work pool the EKS worker polls. Cloud holds the
# schedule and the run timeline only; result and cache-key storage stay on S3
# (ADR-0012, src/slipstream_bench/orchestration/storage.py), so no measurement has a
# competing copy here. Cluster lifecycle and the worker itself live in other stacks;
# this one is Cloud wiring plus the secret that carries its address to them.

provider "prefect" {
  api_key    = var.prefect_api_key
  account_id = var.prefect_account_id
}

provider "aws" {
  region = var.region
}

locals {
  tags = {
    Project   = "slipstream"
    ManagedBy = "terraform"
    Component = "prefect-cloud"
  }

  # Prefect Cloud's account+workspace-scoped API URL: what PREFECT_API_URL is set to
  # so a worker or CI job talks to this workspace. The account id is a variable and
  # the workspace id is resolved from the existing workspace below.
  prefect_api_url = "https://api.prefect.cloud/api/accounts/${var.prefect_account_id}/workspaces/${data.prefect_workspace.sweep.id}"
}

# The Hobby workspace, created once on signup, referenced (not managed) here: the
# free tier caps the account at one workspace, so a managed resource would collide
# with the auto-created default. Looked up by its id (from the login profile).
data "prefect_workspace" "sweep" {
  id = var.workspace_id
}

# The work pool the EKS worker polls. Kubernetes type so a Kubernetes worker on the
# cluster picks up runs and launches each sweep as a Job
# (ADR-0015-unattended-sweep-cloud-server-and-parent-flow). Left unpaused so
# a worker can pull work the moment it connects. The default base job template for the
# type applies; the worker stack customizes it if it needs to.
resource "prefect_work_pool" "sweep" {
  name         = var.work_pool_name
  type         = "kubernetes"
  workspace_id = data.prefect_workspace.sweep.id
  paused       = false
}
