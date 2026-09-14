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

# Consumed by the baseline stack to attach an ephemeral public load balancer to
# this cluster's VPC and node group.
output "vpc_id" {
  description = "ID of the cluster VPC."
  value       = module.vpc.vpc_id
}

output "public_subnets" {
  description = "Public subnet IDs, where an internet-facing load balancer attaches."
  value       = module.vpc.public_subnets
}

output "node_security_group_id" {
  description = "Security group attached to the managed node group; a baseline load balancer adds an ingress rule here to reach the vLLM NodePort."
  value       = module.eks.node_security_group_id
}

output "node_autoscaling_groups" {
  description = "Autoscaling group names backing the managed node groups; a load balancer target group attaches to these to register node instances."
  value       = flatten([for ng in module.eks.eks_managed_node_groups : ng.node_group_autoscaling_group_names])
}
