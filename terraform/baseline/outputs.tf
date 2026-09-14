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
