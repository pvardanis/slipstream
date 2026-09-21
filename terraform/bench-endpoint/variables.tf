# Inputs for the bench endpoint stack.
variable "region" {
  description = "AWS region for the load balancer. Must match the eks stack's region so the LB sits in the cluster VPC."
  type        = string
  default     = "eu-west-1"
}

variable "cluster_name" {
  description = "Name of the EKS cluster this bench endpoint fronts; used to prefix bench endpoint resource names."
  type        = string
  default     = "slipstream"
}

variable "vpc_id" {
  description = "VPC the cluster runs in; the load balancer, target group and security groups attach to it. Supplied from the eks stack's outputs at apply time. Teardown reads it from this stack's own state, so it need not be set to destroy."
  type        = string
  default     = ""
}

variable "public_subnets" {
  description = "Internet-routable subnets the load balancer and bench host launch into. Supplied from the eks stack's outputs at apply time; unused on destroy."
  type        = list(string)
  default     = []
}

variable "node_security_group_id" {
  description = "The eks-owned node group security group the NodePort ingress rule attaches to. Supplied from the eks stack's outputs at apply time; unused on destroy."
  type        = string
  default     = ""
}

variable "node_autoscaling_groups" {
  description = "Autoscaling groups backing the node group, attached to the load balancer target group so nodes register automatically. Supplied from the eks stack's outputs at apply time; unused on destroy."
  type        = list(string)
  default     = []
}

variable "state_bucket" {
  description = "S3 bucket holding remote state, used to read the bootstrap stack's outputs. Supplied from the bootstrap output at apply time."
  type        = string

  validation {
    condition     = length(var.state_bucket) > 0
    error_message = "state_bucket must be non-empty; a failed bootstrap output lookup would otherwise read state from an empty bucket name."
  }
}

variable "operator_cidr" {
  description = "CIDR allowed to reach the public load balancer, in addition to mTLS. A single operator /32 keeps the surface minimal; mTLS is the real gate."
  type        = string

  validation {
    condition     = can(cidrhost(var.operator_cidr, 0))
    error_message = "operator_cidr must be a valid CIDR (e.g. 203.0.113.7/32); a malformed IP lookup would otherwise reach the security group."
  }
}

variable "vllm_nodeport" {
  description = "NodePort the vLLM Service is exposed on. The load balancer target group forwards to this port on the node group."
  type        = number
  default     = 30800
}

variable "vllm_api_key" {
  description = "API key vLLM enforces on its API routes (via the VLLM_API_KEY env var). The load balancer terminates mTLS as the complete gate; vLLM checks this key as a second, request-level lock on /v1."
  type        = string
  sensitive   = true

  validation {
    condition     = length(var.vllm_api_key) > 0
    error_message = "vllm_api_key must be non-empty; publishing an empty key to Secrets Manager would authenticate nothing."
  }
}
