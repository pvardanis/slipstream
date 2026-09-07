# Outputs from the eks stack.
output "cluster_name" {
  description = "Name of the EKS cluster (input to aws eks update-kubeconfig)."
  value       = module.eks.cluster_name
}

output "cluster_endpoint" {
  description = "Kubernetes API server endpoint."
  value       = module.eks.cluster_endpoint
}

output "region" {
  description = "Region the cluster lives in."
  value       = var.region
}
