# The public application load balancer that fronts vLLM for one baseline run.
# It terminates mutual TLS (the ALB is the only AWS load balancer that can),
# verifying the client certificate against a trust store, then forwards plain
# HTTP to the vLLM NodePort inside the VPC. The mTLS verify is the complete,
# connection-level gate; vLLM's api-key is a second lock on its API routes.

provider "aws" {
  region = var.region
}

# Server certificate imported into ACM for the HTTPS listener to present.
resource "aws_acm_certificate" "server" {
  private_key       = tls_private_key.server.private_key_pem
  certificate_body  = tls_locally_signed_cert.server.cert_pem
  certificate_chain = tls_self_signed_cert.ca.cert_pem

  tags = local.tags

  lifecycle {
    create_before_destroy = true
  }
}

# The trust store needs the CA bundle in S3. A dedicated, force-destroyed bucket
# keeps it out of the long-lived state bucket and lets bench-endpoint-down remove it
# cleanly with the rest of the stack.
resource "aws_s3_bucket" "trust_store" {
  bucket_prefix = "${local.name}-truststore-"
  force_destroy = true
  tags          = local.tags
}

resource "aws_s3_bucket_public_access_block" "trust_store" {
  bucket = aws_s3_bucket.trust_store.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_object" "ca_bundle" {
  bucket  = aws_s3_bucket.trust_store.id
  key     = "ca-bundle.pem"
  content = tls_self_signed_cert.ca.cert_pem
}

resource "aws_lb_trust_store" "mtls" {
  name_prefix                      = "bench-"
  ca_certificates_bundle_s3_bucket = aws_s3_bucket.trust_store.id
  ca_certificates_bundle_s3_key    = aws_s3_object.ca_bundle.key

  tags = local.tags
}

resource "aws_lb" "bench_endpoint" {
  name_prefix        = "bench-"
  internal           = false
  load_balancer_type = "application"
  security_groups    = [aws_security_group.alb.id]
  subnets            = var.public_subnets

  tags = local.tags
}

# Forwards to the vLLM NodePort on the node group over plain HTTP; TLS is already
# terminated at the listener, and the hop stays inside the VPC.
resource "aws_lb_target_group" "vllm" {
  name_prefix = "bench-"
  port        = var.vllm_nodeport
  protocol    = "HTTP"
  vpc_id      = var.vpc_id
  target_type = "instance"

  health_check {
    path                = "/health"
    port                = "traffic-port"
    healthy_threshold   = 2
    unhealthy_threshold = 2
    interval            = 10
    timeout             = 5
  }

  tags = local.tags
}

# Register the node group's instances with the target group by attaching its
# autoscaling group; new nodes join automatically.
resource "aws_autoscaling_attachment" "vllm" {
  for_each = toset(var.node_autoscaling_groups)

  autoscaling_group_name = each.value
  lb_target_group_arn    = aws_lb_target_group.vllm.arn
}

resource "aws_lb_listener" "https" {
  load_balancer_arn = aws_lb.bench_endpoint.arn
  port              = 443
  protocol          = "HTTPS"
  ssl_policy        = "ELBSecurityPolicy-TLS13-1-2-2021-06"
  certificate_arn   = aws_acm_certificate.server.arn

  # Mutual TLS in verify mode: the listener rejects any client whose certificate
  # the trust store's CA did not sign. This is the connection-level gate; there
  # is no per-request auth proxy that would add latency to every request.
  mutual_authentication {
    mode            = "verify"
    trust_store_arn = aws_lb_trust_store.mtls.arn
  }

  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.vllm.arn
  }
}
