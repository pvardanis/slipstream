# Outputs from the state-bootstrap stack.
output "state_bucket_name" {
  description = "Name of the S3 bucket holding remote state; matches the bucket literal in terraform/eks/backend.tf."
  value       = aws_s3_bucket.state.id
}

output "region" {
  description = "Region the state bucket lives in."
  value       = var.region
}
