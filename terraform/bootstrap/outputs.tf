# Outputs from the state-bootstrap stack.
output "state_bucket_name" {
  description = "Name of the S3 bucket holding remote state; passed to the eks backend via -backend-config at init."
  value       = aws_s3_bucket.state.id
}

output "region" {
  description = "Region the state bucket lives in."
  value       = var.region
}

output "bench_image_repo_url" {
  description = "Registry URL of the bench-client ECR repository; the base for `docker push` and the pod image reference."
  value       = aws_ecr_repository.bench_client.repository_url
}

output "bench_image_repo_arn" {
  description = "ARN of the bench-client ECR repository. The bench endpoint's bench host IAM policy scopes image pulls to this repository rather than granting pull on every repo."
  value       = aws_ecr_repository.bench_client.arn
}

output "bench_image_push_role_arn" {
  description = "ARN of the IAM role the bench-image GitHub Actions workflow assumes via OIDC to push to ECR; set as the role-to-assume in that workflow."
  value       = aws_iam_role.bench_image_push.arn
}

output "orchestration_image_repo_url" {
  description = "Registry URL of the orchestration ECR repository; the base for `docker push` and the repository the eks worker layer reads to resolve its image."
  value       = aws_ecr_repository.orchestration.repository_url
}

output "orchestration_image_repo_arn" {
  description = "ARN of the orchestration ECR repository."
  value       = aws_ecr_repository.orchestration.arn
}

output "orchestration_image_push_role_arn" {
  description = "ARN of the IAM role the orchestration-image GitHub Actions workflow assumes via OIDC to push to ECR; set as the role-to-assume in that workflow."
  value       = aws_iam_role.orchestration_image_push.arn
}

output "sweep_dispatch_role_arn" {
  description = "ARN of the IAM role the flow-dispatch GitHub Actions workflow assumes via OIDC; set as the AWS_SWEEP_DISPATCH_ROLE_ARN repo variable, and read by the eks stack to grant it Kubernetes access."
  value       = aws_iam_role.sweep_dispatch.arn
}

output "cluster_name" {
  description = "Name of the EKS cluster the flow-dispatch workflow targets; set as the EKS_CLUSTER_NAME repo variable."
  value       = var.cluster_name
}

output "prefect_namespace" {
  description = "Namespace of the Prefect server the flow-dispatch workflow port-forwards into; set as the PREFECT_NAMESPACE repo variable."
  value       = var.prefect_namespace
}

output "grafana_cloud_write_secret_name" {
  description = "Name of the Secrets Manager secret holding the Grafana Cloud metrics:write and traces:write token; the setup wizard writes it and the External Secrets Operator syncs it into the cluster."
  value       = aws_secretsmanager_secret.grafana_cloud_write.name
}

output "grafana_cloud_api_secret_name" {
  description = "Name of the Secrets Manager secret holding the Grafana Terraform provider's token; the setup wizard writes it and the terraform/grafana stack reads it."
  value       = aws_secretsmanager_secret.grafana_cloud_api.name
}

output "grafana_cloudwatch_role_arn" {
  description = "ARN of the IAM role Grafana Cloud's CloudWatch data source assumes; pasted into the data source's Assume Role ARN field."
  value       = aws_iam_role.grafana_cloudwatch.arn
}

output "spend_leak_topic_arn" {
  description = "ARN of the SNS topic both spend-leak layers page through; `just bootstrap` reads the operator email back from its email subscription."
  value       = aws_sns_topic.spend_leak.arn
}
