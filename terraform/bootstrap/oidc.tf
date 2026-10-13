# GitHub OIDC trust and the IAM roles GitHub Actions workflows assume: the image-push
# roles (see ecr.tf) and the sweep-dispatch role the CI sweep trigger uses to reach the
# cluster. They live in the bootstrap stack because they are durable, account-wide
# identity that must exist before any workflow runs and must outlive
# `just cluster-down`. No long-lived AWS keys: each workflow exchanges its short-lived
# OIDC token for its role via STS.

locals {
  github_oidc_host     = "token.actions.githubusercontent.com"
  github_oidc_url      = "https://${local.github_oidc_host}"
  github_oidc_audience = "sts.amazonaws.com"

  # Only workflow runs on this repo's main branch may assume the role; the OIDC
  # subject claim is matched exactly, so a fork or a run on another branch cannot.
  # The prefix carries GitHub's immutable owner/repo IDs, which the token emits
  # when the repo has immutable subject claims enabled; a mutable "repo:owner/name"
  # subject would not match that token.
  github_push_subject = "${var.github_oidc_sub_prefix}:ref:refs/heads/main"

  # Shared by both image-push roles (bench-client and orchestration): the trust is
  # scoped to this repo's main branch, not to any one image, so the same policy backs
  # every push role. Each role's *permissions* are what narrow it to a single repo.
  github_push_trust_policy = {
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

  # Same shape as the bench push policy, scoped to the orchestration repo ARN so this
  # role touches that repository and no other.
  orchestration_push_permissions_policy = {
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "GetAuthorizationToken"
        Effect   = "Allow"
        Action   = "ecr:GetAuthorizationToken"
        Resource = "*"
      },
      {
        Sid    = "PushOrchestrationImage"
        Effect = "Allow"
        Action = [
          "ecr:BatchCheckLayerAvailability",
          "ecr:InitiateLayerUpload",
          "ecr:UploadLayerPart",
          "ecr:CompleteLayerUpload",
          "ecr:PutImage",
        ]
        Resource = aws_ecr_repository.orchestration.arn
      },
      {
        Sid      = "CheckOrchestrationImageExists"
        Effect   = "Allow"
        Action   = "ecr:DescribeImages"
        Resource = aws_ecr_repository.orchestration.arn
      },
    ]
  }

  # The sweep-dispatch workflow's AWS reads, made before it port-forwards to the Prefect
  # server: DescribeCluster backs `aws eks update-kubeconfig` and is scoped to this one
  # cluster's ARN; DescribeInstances resolves the bench host from its tags. Describe
  # actions take no resource scope, so that grant is on "*", narrowed to the stack's
  # region by condition. The port-forward itself is Kubernetes access, granted by the
  # eks stack's access entry + namespaced RBAC, not by this policy.
  sweep_dispatch_permissions_policy = {
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "DescribeCluster"
        Effect   = "Allow"
        Action   = "eks:DescribeCluster"
        Resource = "arn:aws:eks:${var.region}:${data.aws_caller_identity.current.account_id}:cluster/${var.cluster_name}"
      },
      {
        Sid      = "DescribeInstances"
        Effect   = "Allow"
        Action   = "ec2:DescribeInstances"
        Resource = "*"
        Condition = {
          StringEquals = { "aws:RequestedRegion" = var.region }
        }
      },
    ]
  }
}

data "aws_caller_identity" "current" {}

# AWS uses its own library of trusted root CAs to validate GitHub's issuer, so no
# thumbprint_list is configured (an explicit list would need a network read of the
# discovery certificate, which the offline plan tests must not do).
resource "aws_iam_openid_connect_provider" "github" {
  url            = local.github_oidc_url
  client_id_list = [local.github_oidc_audience]
}

resource "aws_iam_role" "bench_image_push" {
  name               = var.bench_image_push_role_name
  assume_role_policy = jsonencode(local.github_push_trust_policy)
}

resource "aws_iam_role_policy" "bench_image_push" {
  name   = "bench-client-push"
  role   = aws_iam_role.bench_image_push.id
  policy = jsonencode(local.bench_push_permissions_policy)
}

resource "aws_iam_role" "orchestration_image_push" {
  name               = var.orchestration_image_push_role_name
  assume_role_policy = jsonencode(local.github_push_trust_policy)
}

resource "aws_iam_role_policy" "orchestration_image_push" {
  name   = "orchestration-push"
  role   = aws_iam_role.orchestration_image_push.id
  policy = jsonencode(local.orchestration_push_permissions_policy)
}

# Shares the repo-main trust with the push roles: only a main-branch run of this repo
# can assume it, which is where the workflow_dispatch runs.
resource "aws_iam_role" "sweep_dispatch" {
  name               = var.sweep_dispatch_role_name
  assume_role_policy = jsonencode(local.github_push_trust_policy)
}

resource "aws_iam_role_policy" "sweep_dispatch" {
  name   = "sweep-dispatch"
  role   = aws_iam_role.sweep_dispatch.id
  policy = jsonencode(local.sweep_dispatch_permissions_policy)
}
