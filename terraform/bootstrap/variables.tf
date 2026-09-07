# Inputs for the state-bootstrap stack.
variable "region" {
  description = "AWS region hosting the Terraform state bucket."
  type        = string
  default     = "eu-west-1"
}

variable "state_bucket_name" {
  description = "Name of the S3 bucket that stores remote state for the eks stack. Must match the bucket in terraform/eks/backend.tf."
  type        = string
  default     = "slipstream-tf-state"
}
