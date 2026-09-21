# The bench client's credentials, published to Secrets Manager so the ephemeral
# bench host (a later layer) pulls them at launch rather than receiving them on a
# command line, where they would show in process args and shell history. The
# client certificate, its key, the CA to verify the server, and the vLLM api-key
# travel together as one JSON secret. recovery_window_in_days = 0 lets
# bench-endpoint-down delete them immediately instead of leaving a 30-day tombstone.

resource "aws_secretsmanager_secret" "bench_client" {
  name_prefix             = "${local.name}-client-"
  recovery_window_in_days = 0
  tags                    = local.tags
}

resource "aws_secretsmanager_secret_version" "bench_client" {
  secret_id = aws_secretsmanager_secret.bench_client.id
  secret_string = jsonencode({
    client_cert_pem = tls_locally_signed_cert.client.cert_pem
    client_key_pem  = tls_private_key.client.private_key_pem
    ca_cert_pem     = tls_self_signed_cert.ca.cert_pem
    api_key         = var.vllm_api_key
    server_dns_name = var.server_dns_name
  })
}
