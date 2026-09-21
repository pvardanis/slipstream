# GitHub OIDC trust and the IAM role a GitHub Actions workflow assumes to push the
# bench-client image to ECR (see ecr.tf). It lives in the bootstrap stack because
# it is durable, account-wide identity that must exist before any workflow can
# publish and must outlive `just cluster-down`. No long-lived AWS keys: the
# workflow exchanges its short-lived OIDC token for this role via STS.

locals {
  github_oidc_host     = "token.actions.githubusercontent.com"
  github_oidc_url      = "https://${local.github_oidc_host}"
  github_oidc_audience = "sts.amazonaws.com"

  # Only workflow runs on this repo's main branch may assume the role; the OIDC
  # subject claim is matched exactly, so a fork or a run on another branch cannot.
  github_push_subject = "repo:${var.github_repository}:ref:refs/heads/main"

  bench_push_trust_policy = {
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Federated = aws_iam_openid_connect_provider.github.arn }
      Action    = "sts:AssumeRoleWithWebIdentity"
      Condition = {
        StringEquals = {
          "${local.github_oidc_host}:aud" = local.github_oidc_audience
          "${local.github_oidc_host}:sub" = local.github_push_subject
        }
      }
    }]
  }

  # GetAuthorizationToken is an account-level action with no resource scope, so it
  # must be granted on "*"; the write and read actions are each scoped to the
  # bench-client repo ARN so the role touches that repository and no other. The read
  # statement backs the workflow's content-hash existence check (does this tag
  # already exist?) before it builds and pushes.
  bench_push_permissions_policy = {
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "GetAuthorizationToken"
        Effect   = "Allow"
        Action   = "ecr:GetAuthorizationToken"
        Resource = "*"
      },
      {
        Sid    = "PushBenchClientImage"
        Effect = "Allow"
        Action = [
          "ecr:BatchCheckLayerAvailability",
          "ecr:InitiateLayerUpload",
          "ecr:UploadLayerPart",
          "ecr:CompleteLayerUpload",
          "ecr:PutImage",
        ]
        Resource = aws_ecr_repository.bench_client.arn
      },
      {
        Sid      = "CheckBenchClientImageExists"
        Effect   = "Allow"
        Action   = "ecr:DescribeImages"
        Resource = aws_ecr_repository.bench_client.arn
      },
    ]
  }
}

# AWS uses its own library of trusted root CAs to validate GitHub's issuer, so no
# thumbprint_list is configured (an explicit list would need a network read of the
# discovery certificate, which the offline plan tests must not do).
resource "aws_iam_openid_connect_provider" "github" {
  url            = local.github_oidc_url
  client_id_list = [local.github_oidc_audience]
}

resource "aws_iam_role" "bench_image_push" {
  name               = var.bench_image_push_role_name
  assume_role_policy = jsonencode(local.bench_push_trust_policy)
}

resource "aws_iam_role_policy" "bench_image_push" {
  name   = "bench-client-push"
  role   = aws_iam_role.bench_image_push.id
  policy = jsonencode(local.bench_push_permissions_policy)
}
