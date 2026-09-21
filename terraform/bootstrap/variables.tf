# Inputs for the state-bootstrap stack.
variable "region" {
  description = "AWS region hosting the Terraform state bucket."
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
