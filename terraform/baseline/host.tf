# The ephemeral EC2 bench host: the external measurement vantage. It sits outside
# the cluster and drives load at vLLM through the public mutual-TLS ALB, so the
# baseline latency it records is what a real off-cluster client would see rather
# than an in-cluster ClusterIP hop. It boots, pulls the baked bench-client image
# from ECR, and idles; the sweep itself is driven over an SSM session (a later
# layer), which is also how the operator reaches the host — there is no inbound
# SSH and the security group admits nothing. It reads its client certificate,
# key, CA and api-key from the bench-client secret at run time, never from a
# command line, and writes results to the results bucket so they outlive it.

variable "bench_image_tag" {
  description = "Tag of the bench-client image the host pulls from ECR. Matches the tag `just bench-image` pushes (reused across dev-loop rebuilds)."
  type        = string
  default     = "latest"
}

variable "bench_host_instance_type" {
  description = "Instance type for the bench host. A non-burstable type by default: burstable CPU credits would add variance to a latency measurement."
  type        = string
  default     = "c7i.large"
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

# Instance role, assumable only by EC2. Every grant below is a separate inline
# policy scoped to one resource, so least privilege is legible one grant at a time.
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
  ami                         = data.aws_ssm_parameter.al2023.value
  instance_type               = var.bench_host_instance_type
  subnet_id                   = local.eks.public_subnets[0]
  associate_public_ip_address = true
  vpc_security_group_ids      = [aws_security_group.bench_host.id]
  iam_instance_profile        = aws_iam_instance_profile.bench_host.name

  user_data = templatefile("${path.module}/user-data.sh.tftpl", {
    region       = var.region
    ecr_registry = local.ecr_registry
    image_ref    = local.bench_image_ref
  })
  user_data_replace_on_change = true

  tags = merge(local.tags, { Name = "${local.name}-host" })
}
