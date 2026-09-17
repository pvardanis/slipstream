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
  description = "Instance type for the CPU node group. 8 GiB fits a CPU vLLM replica; GPU/spot is a later layer."
  type        = string
  default     = "t3.large"
}

variable "karpenter_chart_version" {
  description = "Pinned Karpenter Helm chart version (matches the karpenter-provider-aws release), from the oci://public.ecr.aws/karpenter registry."
  type        = string
  default     = "1.8.0"
}
