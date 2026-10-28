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

variable "state_bucket" {
  description = "Name of the S3 bucket holding the bootstrap stack's remote state, read for the orchestration image repository URL, the sweep-dispatch role ARN, and the cluster name and Prefect namespace the sweep-dispatch preconditions check. Supplied by `just cluster-up` from the bootstrap state_bucket_name output; the backend itself is configured separately at init (backend.tf)."
  type        = string

  validation {
    condition     = length(var.state_bucket) > 0
    error_message = "state_bucket must be non-empty; a failed bootstrap output lookup would otherwise read state from an empty bucket name."
  }
}

variable "prefect_work_pool" {
  description = "Name of the process work pool the worker polls; matches the pool `just prefect-up` creates (justfile prefect_work_pool)."
  type        = string
  default     = "sweep-pool"
}

variable "external_secrets_chart_version" {
  description = "Pinned external-secrets Helm chart version from the https://charts.external-secrets.io repo (appVersion v2.14.0)."
  type        = string
  default     = "2.14.0"
}

variable "kube_prometheus_stack_chart_version" {
  description = "Pinned kube-prometheus-stack Helm chart version from the https://prometheus-community.github.io/helm-charts repo (appVersion v0.94.2, the Prometheus Operator)."
  type        = string
  default     = "92.14.0"
}

variable "dcgm_exporter_chart_version" {
  description = "Pinned dcgm-exporter Helm chart version from the https://nvidia.github.io/dcgm-exporter/helm-charts repo (appVersion 4.8.4, image 4.6.1-4.8.4)."
  type        = string
  default     = "4.8.4"
}
