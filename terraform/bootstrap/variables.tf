# Inputs for the state-bootstrap stack.
variable "region" {
  description = "AWS region hosting the Terraform state bucket and the stacks the bootstrap roles reach (the sweep-dispatch role's cluster ARN and DescribeInstances region condition)."
  type        = string
  default     = "eu-west-1"
}

variable "state_bucket_prefix" {
  description = "Prefix for the remote-state bucket; a random suffix is appended for global uniqueness."
  type        = string
  default     = "slipstream-tf-state"
}

variable "bench_image_repo_name" {
  description = "Name of the ECR repository holding the bench-client image."
  type        = string
  default     = "slipstream/bench-client"
}

variable "bench_image_keep_count" {
  description = "Number of most-recent content-tagged (-sha) bench-client images the ECR lifecycle policy retains before expiring older ones."
  type        = number
  default     = 10
}

variable "orchestration_image_repo_name" {
  description = "Name of the ECR repository holding the orchestration image (the baked Prefect-worker image that drives the knob sweep)."
  type        = string
  default     = "slipstream/orchestration"
}

variable "orchestration_image_keep_count" {
  description = "Number of most-recent content-tagged (-sha) orchestration images the ECR lifecycle policy retains before expiring older ones."
  type        = number
  default     = 10
}

variable "github_oidc_sub_prefix" {
  description = "Immutable OIDC subject prefix for the GitHub repository whose Actions workflows may assume the bench-client push role, in the form repo:<owner>@<owner_id>/<repo>@<repo_id>. Read once from the repo's OIDC sub-claim customization (gh api repos/OWNER/REPO/actions/oidc/customization/sub); the numeric IDs are immutable, so the trust survives owner or repo renames."
  type        = string
  default     = "repo:pvardanis@37624791/slipstream@1357264197"
}

variable "bench_image_push_role_name" {
  description = "Name of the IAM role GitHub Actions assumes via OIDC to push the bench-client image."
  type        = string
  default     = "slipstream-bench-image-push"
}

variable "orchestration_image_push_role_name" {
  description = "Name of the IAM role GitHub Actions assumes via OIDC to push the orchestration image."
  type        = string
  default     = "slipstream-orchestration-image-push"
}

variable "sweep_dispatch_role_name" {
  description = "Name of the IAM role the sweep-dispatch GitHub Actions workflow assumes via OIDC to reach the cluster and trigger the knob sweep."
  type        = string
  default     = "slipstream-sweep-dispatch"
}

variable "cluster_name" {
  description = "Name of the EKS cluster the sweep-dispatch role may describe. Must match the eks stack's cluster_name (both default to slipstream)."
  type        = string
  default     = "slipstream"
}

variable "prefect_namespace" {
  description = "Namespace the Prefect server runs in, which the sweep-dispatch workflow port-forwards into. Must match the eks stack's prefect_namespace local (prefect)."
  type        = string
  default     = "prefect"
}
