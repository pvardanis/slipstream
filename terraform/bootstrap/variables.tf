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
