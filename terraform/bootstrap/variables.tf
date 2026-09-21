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

variable "github_repository" {
  description = "The owner/name of the GitHub repository whose Actions workflows may assume the bench-client push role via OIDC."
  type        = string
  default     = "pvardanis/slipstream"
}

variable "bench_image_push_role_name" {
  description = "Name of the IAM role GitHub Actions assumes via OIDC to push the bench-client image."
  type        = string
  default     = "slipstream-bench-image-push"
}
