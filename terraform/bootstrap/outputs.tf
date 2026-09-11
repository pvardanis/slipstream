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
