# Outputs from the Prefect Cloud stack.
output "workspace_id" {
  description = "Id of the referenced Hobby workspace. Embedded in the API URL and used to address the workspace from the worker and CI."
  value       = data.prefect_workspace.sweep.id
}

output "prefect_api_url" {
  description = "Account+workspace-scoped Prefect Cloud API URL. Set as PREFECT_API_URL to point a worker or CI job at this workspace. Not sensitive; the API key that pairs with it lives only in the secret."
  value       = local.prefect_api_url
}

output "work_pool_name" {
  description = "Name of the Kubernetes work pool the EKS worker polls."
  value       = prefect_work_pool.sweep.name
}

output "prefect_api_secret_arn" {
  description = "Secrets Manager ARN holding PREFECT_API_URL and PREFECT_API_KEY. The EKS worker and CI read this at launch to reach the workspace."
  value       = aws_secretsmanager_secret.prefect_api.arn
}
