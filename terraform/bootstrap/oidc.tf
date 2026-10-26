# GitHub OIDC trust and the IAM roles GitHub Actions workflows assume: the image-push
# roles (see ecr.tf) and the sweep-dispatch role the CI flow triggers use to reach the
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

  # Shared by every role here (the bench-client and orchestration image-push roles and
  # the sweep-dispatch role): the trust is scoped to this repo's main branch, not to any
  # one workflow, so the same policy backs each role. Each role's *permissions* are what
  # narrow it to its one job.
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

  # The actions a docker push makes against one repository: the layer-upload sequence,
  # PutImage, and BatchGetImage because Docker 29 HEADs the manifest by digest after
  # uploading the layers, and ECR authorises a manifest HEAD as BatchGetImage. Both push
  # roles grant this set, each on its own repository.
  ecr_push_actions = [
    "ecr:BatchCheckLayerAvailability",
    "ecr:InitiateLayerUpload",
    "ecr:UploadLayerPart",
    "ecr:CompleteLayerUpload",
    "ecr:PutImage",
    "ecr:BatchGetImage",
  ]

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
        Sid      = "PushBenchClientImage"
        Effect   = "Allow"
        Action   = local.ecr_push_actions
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
        Sid      = "PushOrchestrationImage"
        Effect   = "Allow"
        Action   = local.ecr_push_actions
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

  # The flow-dispatch workflow's AWS reads, made before it port-forwards to the Prefect
  # server: DescribeCluster backs `aws eks update-kubeconfig` and is scoped to this one
  # cluster's ARN; DescribeInstances resolves the bench host from its tags. Describe
  # actions take no resource scope, so that grant is on "*", narrowed to the stack's
  # region by condition. ListAllMyBuckets resolves the results bucket the workflow
  # registers the deployment against: the bench-endpoint stack names it by prefix, so
  # its full name changes whenever the bucket is recreated and is found by listing. The
  # action is account-wide and takes no resource scope, and it returns bucket names
  # only, never objects. The port-forward itself is Kubernetes access, granted by the
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
      {
        Sid      = "ListAllMyBuckets"
        Effect   = "Allow"
        Action   = "s3:ListAllMyBuckets"
        Resource = "*"
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
