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
