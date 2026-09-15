# The ephemeral EC2 bench host: the external measurement vantage. It sits outside
# the cluster and drives load at vLLM through the public mutual-TLS ALB, so the
# baseline latency it records is what a real off-cluster client would see rather
# than an in-cluster ClusterIP hop. It boots, pulls the baked bench-client image
# from ECR, and idles; the sweep itself is driven over an SSM session (a later
# layer), which is also how the operator reaches the host — there is no inbound
# SSH and the security group admits nothing. At sweep time it will read its
# client certificate, key, CA and api-key from the bench-client secret rather
# than a command line, and write results to the results bucket so they outlive it.

variable "bench_image_tag" {
  description = "Tag of the bench-client image the host pulls from ECR. Matches the tag `just bench-image` pushes (reused across dev-loop rebuilds)."
  type        = string
  default     = "latest"

  validation {
    condition     = length(var.bench_image_tag) > 0
    error_message = "bench_image_tag must be non-empty; an empty tag renders an invalid image reference the host only discovers at pull time."
  }
}

variable "bench_host_instance_type" {
  description = "Instance type for the bench host. A non-burstable type by default: burstable CPU credits would add variance to a latency measurement."
  type        = string
  default     = "c7i.large"

  validation {
    condition     = !can(regex("^t[0-9]", var.bench_host_instance_type))
    error_message = "bench_host_instance_type must be non-burstable; burstable CPU credits add variance to the latency measurement."
  }
}

# Latest Amazon Linux 2023 x86_64 AMI, resolved from the public SSM parameter so
# the host tracks patched images without a hard-coded AMI id. x86_64 matches the
# baked bench-client image (bench/Dockerfile).
data "aws_ssm_parameter" "al2023" {
  name = "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64"
}

locals {
  bench_image_ref = "${data.terraform_remote_state.bootstrap.outputs.bench_image_repo_url}:${var.bench_image_tag}"
  # The registry host `docker login` authenticates against is the repo URL up to
  # the first slash (account.dkr.ecr.<region>.amazonaws.com).
  ecr_registry = split("/", data.terraform_remote_state.bootstrap.outputs.bench_image_repo_url)[0]

  # Loopback port the mutual-TLS proxy listens on: the sweep container drives plain
  # HTTP here and the proxy speaks mTLS to the ALB. Shared between the rendered
  # nginx config and the proxy.env the up-script reads.
  bench_proxy_port     = 8000
  bench_proxy_cert_dir = "/etc/bench-proxy"

  # Rendered nginx stream config for the client-side mTLS proxy. A local so a test
  # can assert its shape (loopback listen, proxy_ssl to the ALB DNS name, verify
  # against the CA under the issued server name) offline.
  bench_proxy_conf = templatefile("${path.module}/bench-proxy.conf.tftpl", {
    listen_port      = local.bench_proxy_port
    alb_dns_name     = aws_lb.baseline.dns_name
    server_dns_name  = var.server_dns_name
    client_cert_path = "${local.bench_proxy_cert_dir}/client.crt"
    client_key_path  = "${local.bench_proxy_cert_dir}/client.key"
    ca_cert_path     = "${local.bench_proxy_cert_dir}/ca.crt"
  })

  # The proxy up-script and its env, as locals so a test can assert the rendered
  # content is embedded in the boot script (not merely that the path is mentioned).
  bench_proxy_up_script = file("${path.module}/bench-proxy-up.sh")
  bench_proxy_env       = "SECRET_ARN=${aws_secretsmanager_secret.bench_client.arn}\nREGION=${var.region}\nPROXY_PORT=${local.bench_proxy_port}\n"

  # The secret splitter the up-script runs, as a local so a test can assert it is
  # dropped on the host rather than merely referenced by path.
  bench_proxy_secret_split = file("${path.module}/bench_proxy_secret_split.py")

  # The sweep script `just bench` runs over SSM, as a local so a test can assert it
  # is embedded in the boot script rather than merely referenced by path.
  bench_sweep_script = file("${path.module}/bench-sweep.sh")

  # The secret field reader the sweep script runs to pull the api-key, as a local so a
  # test can assert it is dropped on the host rather than merely referenced by path.
  bench_secret_field = file("${path.module}/bench_secret_field.py")

  # The prefix-cache script `just prefix-cache` runs over SSM, as a local so a test can
  # assert it is embedded in the boot script rather than merely referenced by path.
  bench_prefix_cache_script = file("${path.module}/bench-prefix-cache.sh")

  # Rendered boot script. A local (not inline on the instance) so a test can
  # assert the right bucket, registry and image reference were templated in. It
  # also installs and drops the mTLS proxy (config + up-script + env) but does not
  # start it: the client cert lives in Secrets Manager and the ALB targets are not
  # healthy at boot, so the up-script starts nginx and smokes /health at sweep time.
  bench_user_data = templatefile("${path.module}/user-data.sh.tftpl", {
    region              = var.region
    ecr_registry        = local.ecr_registry
    image_ref           = local.bench_image_ref
    results_bucket      = aws_s3_bucket.results.id
    proxy_conf          = local.bench_proxy_conf
    proxy_up_script     = local.bench_proxy_up_script
    proxy_env           = local.bench_proxy_env
    proxy_cert_dir      = local.bench_proxy_cert_dir
    proxy_secret_split  = local.bench_proxy_secret_split
    sweep_script        = local.bench_sweep_script
    secret_field        = local.bench_secret_field
    prefix_cache_script = local.bench_prefix_cache_script
  })
}

# Outbound-only security group: the host reaches ECR, Secrets Manager, SSM, S3
# and the ALB listener, all over HTTPS. Zero ingress — access is an outbound SSM
# session, so there is no inbound surface to defend.
resource "aws_security_group" "bench_host" {
  name_prefix = "${local.name}-host-"
  description = "Egress-only security group for the ephemeral bench host"
  vpc_id      = local.eks.vpc_id

  egress {
    description = "HTTPS to ECR, Secrets Manager, SSM, S3 and the ALB listener"
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = local.tags
}

# Instance role, assumable only by EC2. The secrets, results, and ECR-pull grants
# below are each a separate inline policy scoped to a single resource, so least
# privilege is legible one grant at a time. SSM access is an AWS-managed
# attachment, and ECR GetAuthorizationToken must be registry-wide (see below).
resource "aws_iam_role" "bench_host" {
  name_prefix = "${local.name}-host-"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Action    = "sts:AssumeRole"
      Principal = { Service = "ec2.amazonaws.com" }
    }]
  })

  tags = local.tags
}

# Keyless operator and orchestration access: an SSM session is an outbound
# connection from the host, so it needs no inbound rule and no SSH key pair.
resource "aws_iam_role_policy_attachment" "bench_host_ssm" {
  role       = aws_iam_role.bench_host.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

# Read the bench-client credentials (cert, key, CA, api-key) from the one secret.
resource "aws_iam_role_policy" "bench_host_secrets" {
  name_prefix = "secrets-"
  role        = aws_iam_role.bench_host.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = "secretsmanager:GetSecretValue"
      Resource = aws_secretsmanager_secret.bench_client.arn
    }]
  })
}

# Write measurement JSON to the results bucket's objects, nothing else.
resource "aws_iam_role_policy" "bench_host_results" {
  name_prefix = "results-"
  role        = aws_iam_role.bench_host.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = "s3:PutObject"
      Resource = "${aws_s3_bucket.results.arn}/*"
    }]
  })
}

# Pull the bench-client image from its repository (and only that repository).
resource "aws_iam_role_policy" "bench_host_ecr_pull" {
  name_prefix = "ecr-pull-"
  role        = aws_iam_role.bench_host.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Action = [
        "ecr:GetDownloadUrlForLayer",
        "ecr:BatchGetImage",
        "ecr:BatchCheckLayerAvailability",
      ]
      Resource = data.terraform_remote_state.bootstrap.outputs.bench_image_repo_arn
    }]
  })
}

# GetAuthorizationToken is a registry-wide action that AWS does not let you scope
# to a repository, so it stands alone rather than widening the pull policy above.
resource "aws_iam_role_policy" "bench_host_ecr_auth" {
  name_prefix = "ecr-auth-"
  role        = aws_iam_role.bench_host.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = "ecr:GetAuthorizationToken"
      Resource = "*"
    }]
  })
}

resource "aws_iam_instance_profile" "bench_host" {
  name_prefix = "${local.name}-host-"
  role        = aws_iam_role.bench_host.name
}

resource "aws_instance" "bench_host" {
  ami           = data.aws_ssm_parameter.al2023.value
  instance_type = var.bench_host_instance_type
  # First eks public subnet: the host needs its IGW route so the public IP can
  # reach ECR, Secrets Manager, SSM, S3 and the internet-facing ALB. This trusts
  # the eks stack to keep those subnets internet-routable.
  subnet_id                   = local.eks.public_subnets[0]
  associate_public_ip_address = true
  vpc_security_group_ids      = [aws_security_group.bench_host.id]
  iam_instance_profile        = aws_iam_instance_profile.bench_host.name

  # Require IMDSv2: the host is public and carries role credentials that read the
  # bench-client secret, so a token-less IMDSv1 endpoint would be an SSRF path to
  # those credentials.
  metadata_options {
    http_endpoint = "enabled"
    http_tokens   = "required"
  }

  # gzip the boot script: it embeds the proxy config and four host scripts, which
  # together exceed EC2's 16 KB user-data limit uncompressed. cloud-init detects the
  # gzip magic bytes and decompresses before running it.
  user_data_base64            = base64gzip(local.bench_user_data)
  user_data_replace_on_change = true

  tags = merge(local.tags, { Name = "${local.name}-host" })
}

# The host reaches the internet-facing ALB by its public IP over the IGW hairpin,
# so it arrives at the ALB as a public source address, not a VPC-internal one — a
# security-group reference would never match it. Admit exactly the host's public
# /32 on 443 alongside the operator rule (security.tf). mTLS stays the real gate;
# this is the CIDR pinhole the operator rule already models, extended to the host.
# The ALB security group manages all its rules as standalone rule resources (no
# inline blocks), so this rule coexists with them instead of being revoked on
# every apply. The host is created fresh each baseline-up and destroyed on
# baseline-down — never stopped — so its auto-assigned public IP does not change
# under it; a host meant to survive a stop/start would need an Elastic IP.
resource "aws_vpc_security_group_ingress_rule" "alb_from_host" {
  security_group_id = aws_security_group.alb.id
  cidr_ipv4         = "${aws_instance.bench_host.public_ip}/32"
  ip_protocol       = "tcp"
  from_port         = 443
  to_port           = 443
  description       = "Bench host to the mutual-TLS listener"

  tags = local.tags
}
