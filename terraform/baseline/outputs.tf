# Outputs from the baseline stack.
output "alb_dns_name" {
  description = "AWS-assigned DNS name of the load balancer. The bench client connects here, resolving server_dns_name to this name so the mTLS server certificate validates."
  value       = aws_lb.baseline.dns_name
}

output "server_dns_name" {
  description = "Name the load balancer's server certificate is issued for; the bench client uses it as the TLS SNI/host and resolves it to alb_dns_name."
  value       = var.server_dns_name
}

output "bench_client_secret_arn" {
  description = "Secrets Manager ARN holding the bench client certificate, key, CA, and vLLM api-key. The ephemeral bench host reads this at launch."
  value       = aws_secretsmanager_secret.bench_client.arn
}

output "bench_host_instance_id" {
  description = "Instance id of the ephemeral bench host. Used to open an SSM session to drive the sweep from the external vantage."
  value       = aws_instance.bench_host.id
}

output "results_bucket_name" {
  description = "Name of the S3 bucket the bench host writes measurement JSON to. Results outlive the host here until baseline-down removes the bucket."
  value       = aws_s3_bucket.results.id
}
