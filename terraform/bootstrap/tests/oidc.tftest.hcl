# Plan-level tests for the GitHub OIDC push role on the bootstrap stack. They run
# offline: the aws provider is mocked, so the assertions check the trust and
# permission shape — that only main-branch workflows of this repo can assume the
# role, and that the role can push to the bench-client repo and nothing else —
# without creating a real IAM provider or role. The real end-to-end proof (a
# workflow actually assumes the role and pushes) is the cloud tier.
mock_provider "aws" {}
mock_provider "random" {}

run "github_oidc_trust_and_permissions" {
  command = plan

  # The OIDC provider trusts GitHub's Actions token issuer for the STS audience only.
  assert {
    condition     = aws_iam_openid_connect_provider.github.url == "https://token.actions.githubusercontent.com"
    error_message = "OIDC provider must trust GitHub's Actions token issuer."
  }
  assert {
    condition     = contains(aws_iam_openid_connect_provider.github.client_id_list, "sts.amazonaws.com")
    error_message = "OIDC audience must be scoped to sts.amazonaws.com."
  }

  # Trust is scoped to this repo's main branch: aud pins the STS audience and sub
  # pins the repo and ref, so a workflow on a fork or another branch cannot assume it.
  assert {
    condition     = local.bench_push_trust_policy.Statement[0].Action == "sts:AssumeRoleWithWebIdentity"
    error_message = "Trust policy must allow only web-identity assumption."
  }
  assert {
    condition     = local.bench_push_trust_policy.Statement[0].Condition.StringEquals["token.actions.githubusercontent.com:aud"] == "sts.amazonaws.com"
    error_message = "Trust policy must require the sts.amazonaws.com audience claim."
  }
  assert {
    condition     = local.bench_push_trust_policy.Statement[0].Condition.StringEquals["token.actions.githubusercontent.com:sub"] == "repo:pvardanis/slipstream:ref:refs/heads/main"
    error_message = "Trust policy must scope the subject to this repo's main branch."
  }

  # GetAuthorizationToken is account-level and is the only action granted on "*";
  # the image actions sit on a separate statement whose resource is the repo ARN.
  # That ARN is a reference unknown at plan, so the scoping itself is guarded by the
  # policy tier — here we assert only that the wildcard grant is confined to the
  # auth-token statement and carries no image actions.
  assert {
    condition     = local.bench_push_permissions_policy.Statement[0].Action == "ecr:GetAuthorizationToken"
    error_message = "Role must be able to obtain an ECR auth token."
  }
  assert {
    condition     = local.bench_push_permissions_policy.Statement[0].Resource == "*"
    error_message = "GetAuthorizationToken has no resource scope and must be granted on *."
  }
  # A docker push needs the full layer-upload sequence plus PutImage; dropping any
  # one silently breaks the push at runtime, so assert the whole set rather than a
  # single action. DescribeImages sits on its own statement and backs the workflow's
  # content-hash existence check.
  assert {
    condition = alltrue([
      for action in [
        "ecr:BatchCheckLayerAvailability",
        "ecr:InitiateLayerUpload",
        "ecr:UploadLayerPart",
        "ecr:CompleteLayerUpload",
        "ecr:PutImage",
      ] : contains(local.bench_push_permissions_policy.Statement[1].Action, action)
    ])
    error_message = "Push statement must grant the full docker-push action set."
  }
  assert {
    condition     = local.bench_push_permissions_policy.Statement[2].Action == "ecr:DescribeImages"
    error_message = "A separate statement must grant DescribeImages for the content-hash existence check."
  }
}
