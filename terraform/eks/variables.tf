# Inputs for the eks stack.
variable "region" {
  description = "AWS region for the cluster and its VPC."
  type        = string
  default     = "eu-west-1"
}

variable "cluster_name" {
  description = "Name of the EKS cluster."
  type        = string
  default     = "slipstream"
}

variable "kubernetes_version" {
  description = "EKS control-plane Kubernetes version."
  type        = string
  default     = "1.36"
}

variable "vpc_cidr" {
  description = "CIDR block for the cluster VPC."
  type        = string
  default     = "10.0.0.0/16"
}

variable "node_instance_type" {
  description = "Instance type for the CPU node group. GPU/spot is a later layer."
  type        = string
  default     = "t3.medium"
}
