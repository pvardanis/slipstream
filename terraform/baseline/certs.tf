# An ephemeral, self-signed PKI for the mutual-TLS listener, generated fresh
# each baseline-up and destroyed on baseline-down. A private CA signs both the
# load balancer's server certificate and the bench client's certificate; the
# CA bundle populates the ALB trust store so the listener accepts only clients
# holding a certificate this CA signed. No public ACM domain or ACM Private CA
# is involved — the endpoint lives for minutes and only the bench client and
# operator ever connect. All private keys land in state in plaintext (Terraform
# always stores arguments plaintext), which is why the baseline state file is
# isolated and the bucket is encrypted and IAM-locked.

variable "server_dns_name" {
  description = "SAN the load balancer server certificate is issued for. The bench client reaches the ALB under this name (via --resolve/--connect-to), since the self-signed cert cannot cover the AWS-assigned ALB DNS name."
  type        = string
  default     = "vllm.baseline.slipstream.internal"
}

variable "cert_validity_hours" {
  description = "Lifetime of the generated certificates. A baseline run is far shorter; this is a generous ceiling so a long sweep never outlives its certs."
  type        = number
  default     = 72
}

# Private certificate authority.
resource "tls_private_key" "ca" {
  algorithm   = "ECDSA"
  ecdsa_curve = "P256"
}

resource "tls_self_signed_cert" "ca" {
  private_key_pem = tls_private_key.ca.private_key_pem

  is_ca_certificate     = true
  validity_period_hours = var.cert_validity_hours

  subject {
    common_name  = "${local.name}-ca"
    organization = "slipstream"
  }

  allowed_uses = [
    "cert_signing",
    "crl_signing",
  ]
}

# Server certificate for the load balancer, signed by the CA.
resource "tls_private_key" "server" {
  algorithm   = "ECDSA"
  ecdsa_curve = "P256"
}

resource "tls_cert_request" "server" {
  private_key_pem = tls_private_key.server.private_key_pem

  dns_names = [var.server_dns_name]

  subject {
    common_name  = var.server_dns_name
    organization = "slipstream"
  }
}

resource "tls_locally_signed_cert" "server" {
  cert_request_pem   = tls_cert_request.server.cert_request_pem
  ca_private_key_pem = tls_private_key.ca.private_key_pem
  ca_cert_pem        = tls_self_signed_cert.ca.cert_pem

  validity_period_hours = var.cert_validity_hours

  allowed_uses = [
    "key_encipherment",
    "digital_signature",
    "server_auth",
  ]
}

# Client certificate the bench host presents to the mutual-TLS listener.
resource "tls_private_key" "client" {
  algorithm   = "ECDSA"
  ecdsa_curve = "P256"
}

resource "tls_cert_request" "client" {
  private_key_pem = tls_private_key.client.private_key_pem

  subject {
    common_name  = "${local.name}-client"
    organization = "slipstream"
  }
}

resource "tls_locally_signed_cert" "client" {
  cert_request_pem   = tls_cert_request.client.cert_request_pem
  ca_private_key_pem = tls_private_key.ca.private_key_pem
  ca_cert_pem        = tls_self_signed_cert.ca.cert_pem

  validity_period_hours = var.cert_validity_hours

  allowed_uses = [
    "key_encipherment",
    "digital_signature",
    "client_auth",
  ]
}
