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
  default     = "1.8.8"
}

variable "prefect_server_chart_version" {
  description = "Pinned prefect-server Helm chart version from the https://prefecthq.github.io/prefect-helm repo (appVersion 3.8.7, Prefect 3)."
  type        = string
  default     = "2026.9.26234203"
}

variable "prefect_sqlite_volume_size" {
  description = "Size of the EBS-backed PVC holding the Prefect server's SQLite state. Run history is disposable — S3 is the source of truth (ADR-0012) — so a small volume suffices."
  type        = string
  default     = "1Gi"
}

variable "prefect_worker_chart_version" {
  description = "Pinned prefect-worker Helm chart version from the https://prefecthq.github.io/prefect-helm repo (matches the prefect-server chart, appVersion 3.8.7, Prefect 3)."
  type        = string
  default     = "2026.9.26234203"
}

variable "prefect_worker_image" {
  description = "Full ECR image reference (repo:tag) for the Prefect worker — a lean image carrying the orchestration extra (Prefect + the sweep flow code), built by its own image stack. Empty leaves the worker layer uncreated so the cluster stands up before that image exists; supplying it brings the worker up."
  type        = string
  default     = ""
}

variable "prefect_work_pool" {
  description = "Name of the process work pool the worker polls; matches the pool `just prefect-up` creates (justfile prefect_work_pool)."
  type        = string
  default     = "sweep-pool"
}
