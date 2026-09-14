# Inputs for the baseline stack.
variable "region" {
  description = "AWS region for the load balancer. Must match the eks stack's region so the LB sits in the cluster VPC."
  type        = string
  default     = "eu-west-1"
}

variable "cluster_name" {
  description = "Name of the EKS cluster this baseline fronts; used to prefix baseline resource names."
  type        = string
  default     = "slipstream"
}

variable "state_bucket" {
  description = "S3 bucket holding remote state, used to read the eks stack's outputs. Supplied from the bootstrap output at apply time."
  type        = string
}

variable "operator_cidr" {
  description = "CIDR allowed to reach the public load balancer, in addition to mTLS. A single operator /32 keeps the surface minimal; mTLS is the real gate."
  type        = string
}

variable "vllm_nodeport" {
  description = "NodePort the vLLM Service is exposed on. The load balancer target group forwards to this port on the node group."
  type        = number
  default     = 30800
}

variable "vllm_api_key" {
  description = "API key vLLM enforces via --api-key. The load balancer terminates mTLS; vLLM checks this key as the second, request-level lock."
  type        = string
  sensitive   = true
}
