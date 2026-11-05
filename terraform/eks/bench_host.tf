# The bench host's IAM: its EC2 role, the grants it runs under, and the instance profile
# the bench-endpoint stack launches it with. They live here, not in the bench-endpoint
# stack, so that stack declares no IAM at all and can be applied by a principal with no
# IAM create rights (launching the host under the profile still needs iam:PassRole on
# this role). Like the worker (worker.tf) they ride the cluster's duty-cycle: up with
# `cluster-up`, torn down by `cluster-down`, which `stack-down` runs only after
# `bench-endpoint-down` has destroyed the endpoint that used them.

locals {
  # The bench-endpoint stack's local.name (terraform/bench-endpoint/data.tf), the prefix
  # its client secret and trust-store bucket are named with.
  bench_endpoint_name = "${var.cluster_name}-bench-endpoint"

  # The bench-endpoint stack names its bench-client secret with this prefix
  # (terraform/bench-endpoint/secrets.tf). The secret is created per endpoint, after this
  # stack, so the read grant matches it by name.
  bench_client_secret_prefix = "${local.bench_endpoint_name}-client-"
}

# Instance role, assumable only by EC2. The secrets, results, and ECR-pull grants below
# are each a separate inline policy scoped to a single resource, so least privilege is
# legible one grant at a time. SSM access is an AWS-managed attachment, and ECR
# GetAuthorizationToken must be registry-wide (see below). A fixed name rather than a
# prefix, so its ARN is known before any endpoint exists and the iam:PassRole grant
# whoever launches the host needs can name exactly this role.
resource "aws_iam_role" "bench_host" {
  name = "${var.cluster_name}-bench-host"

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
# The secret's name carries a unique name_prefix suffix and Secrets Manager appends a
# random suffix to its ARN, so the ARN ends in a wildcard.
resource "aws_iam_role_policy" "bench_host_secrets" {
  name = "secrets"
  role = aws_iam_role.bench_host.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = "secretsmanager:GetSecretValue"
      Resource = "arn:aws:secretsmanager:${var.region}:${data.aws_caller_identity.current.account_id}:secret:${local.bench_client_secret_prefix}*"
    }]
  })
}

# Write measurement JSON to the bench-results bucket's objects, nothing else.
resource "aws_iam_role_policy" "bench_host_results" {
  name = "results"
  role = aws_iam_role.bench_host.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = "s3:PutObject"
      Resource = "${local.bench_results_arn}/*"
    }]
  })

  lifecycle {
    precondition {
      condition     = local.bench_results_arn != ""
      error_message = "The bootstrap stack has no bench_results_bucket_arn output, so the bench host has no bench-results bucket to write to. Run `just bootstrap` to create the bucket, then re-run this plan."
    }
  }
}

# Pull the bench-client image from its repository (and only that repository).
resource "aws_iam_role_policy" "bench_host_ecr_pull" {
  name = "ecr-pull"
  role = aws_iam_role.bench_host.id

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
  name = "ecr-auth"
  role = aws_iam_role.bench_host.id

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
  name = "${var.cluster_name}-bench-host"
  role = aws_iam_role.bench_host.name
  tags = local.tags
}
