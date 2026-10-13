# Plan-level tests for the GitHub OIDC roles on the bootstrap stack: the bench-client
# and orchestration image-push roles and the sweep-dispatch role. They run offline: the
# aws provider is mocked, so the assertions check the trust and permission shape — that
# only main-branch workflows of this repo can assume each role, and that each role can do
# its one job and nothing else — without creating a real IAM provider or role. The real
# end-to-end proof (a workflow actually assumes a role and uses it) is the cloud tier.
mock_provider "aws" {
  # A fixed account id so the sweep-dispatch policy's cluster ARN is known at plan and
  # assertable exactly.
  mock_data "aws_caller_identity" {
    defaults = {
      account_id = "111122223333"
      arn        = "arn:aws:iam::111122223333:user/test"
    }
  }
}
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
  # pins the repo and ref, so a workflow on a fork or another branch cannot assume
  # it. The subject uses GitHub's immutable IDs (repo:<owner>@<owner_id>/<repo>@<repo_id>),
  # which the repo emits when immutable subject claims are enabled, so a mutable
  # "repo:owner/name" subject would no longer match the token and must fail here.
  assert {
    condition     = local.github_push_trust_policy.Statement[0].Action == "sts:AssumeRoleWithWebIdentity"
    error_message = "Trust policy must allow only web-identity assumption."
  }
  assert {
    condition     = local.github_push_trust_policy.Statement[0].Condition.StringEquals["token.actions.githubusercontent.com:aud"] == "sts.amazonaws.com"
    error_message = "Trust policy must require the sts.amazonaws.com audience claim."
  }
  assert {
    condition     = local.github_push_trust_policy.Statement[0].Condition.StringEquals["token.actions.githubusercontent.com:sub"] == "repo:pvardanis@37624791/slipstream@1357264197:ref:refs/heads/main"
    error_message = "Trust policy must scope the subject to this repo's main branch using GitHub's immutable owner/repo IDs."
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

# The orchestration-image push role shares the repo-main trust (one identity, both
# workflows) but carries its own permissions scoped to the orchestration repo ARN, so
# a compromised orchestration workflow cannot push the bench-client image, and vice
# versa.
run "orchestration_push_permissions" {
  command = plan

  assert {
    condition     = local.orchestration_push_permissions_policy.Statement[0].Action == "ecr:GetAuthorizationToken"
    error_message = "Role must be able to obtain an ECR auth token."
  }
  assert {
    condition     = local.orchestration_push_permissions_policy.Statement[0].Resource == "*"
    error_message = "GetAuthorizationToken has no resource scope and must be granted on *."
  }
  assert {
    condition = alltrue([
      for action in [
        "ecr:BatchCheckLayerAvailability",
        "ecr:InitiateLayerUpload",
        "ecr:UploadLayerPart",
        "ecr:CompleteLayerUpload",
        "ecr:PutImage",
      ] : contains(local.orchestration_push_permissions_policy.Statement[1].Action, action)
    ])
    error_message = "Push statement must grant the full docker-push action set."
  }
  assert {
    condition     = local.orchestration_push_permissions_policy.Statement[2].Action == "ecr:DescribeImages"
    error_message = "A separate statement must grant DescribeImages for the content-hash existence check."
  }
}

# The sweep-dispatch role shares the repo-main trust, so only a main-branch run of this
# repo's sweep-dispatch workflow can assume it. Its permissions are the two reads the
# workflow makes before the port-forward: DescribeCluster for `aws eks update-kubeconfig`,
# scoped to this one cluster, and DescribeInstances to resolve the bench host from its
# tags. Kubernetes access (the port-forward itself) is not IAM — it is the eks stack's
# access entry + namespaced RBAC.
run "sweep_dispatch_permissions" {
  command = plan

  # A fixed provider ARN so the role's rendered trust policy is known at plan and can be
  # compared against the shared main-branch trust.
  override_resource {
    target          = aws_iam_openid_connect_provider.github
    override_during = plan
    values = {
      arn = "arn:aws:iam::111122223333:oidc-provider/token.actions.githubusercontent.com"
    }
  }

  assert {
    condition     = aws_iam_role.sweep_dispatch.name == "slipstream-sweep-dispatch"
    error_message = "The sweep-dispatch role must be named slipstream-sweep-dispatch."
  }

  # The role's own trust document, not just the shared local, is the main-scoped OIDC
  # trust — so pointing the role at a looser document fails here.
  assert {
    condition     = jsondecode(aws_iam_role.sweep_dispatch.assume_role_policy).Statement[0].Condition.StringEquals["token.actions.githubusercontent.com:sub"] == "repo:pvardanis@37624791/slipstream@1357264197:ref:refs/heads/main"
    error_message = "The sweep-dispatch role must trust only this repo's main branch."
  }
  assert {
    condition     = jsondecode(aws_iam_role.sweep_dispatch.assume_role_policy).Statement[0].Condition.StringEquals["token.actions.githubusercontent.com:aud"] == "sts.amazonaws.com"
    error_message = "The sweep-dispatch role must require the sts.amazonaws.com audience claim."
  }
  assert {
    condition     = length(jsondecode(aws_iam_role.sweep_dispatch.assume_role_policy).Statement) == 1
    error_message = "The sweep-dispatch trust policy must carry only the main-branch OIDC statement."
  }

  # The assertions read the policy attached to the role, so swapping the attachment to a
  # broader document fails them. Exactly two statements, so no grant beyond the two reads
  # slips in alongside them.
  assert {
    condition     = length(jsondecode(aws_iam_role_policy.sweep_dispatch.policy).Statement) == 2
    error_message = "The sweep-dispatch policy must carry exactly the DescribeCluster and DescribeInstances statements."
  }
  assert {
    condition     = length([for s in jsondecode(aws_iam_role_policy.sweep_dispatch.policy).Statement : s if s.Sid == "DescribeCluster" && s.Action == "eks:DescribeCluster" && s.Resource == "arn:aws:eks:eu-west-1:111122223333:cluster/slipstream"]) == 1
    error_message = "eks:DescribeCluster must be scoped to the slipstream cluster ARN."
  }
  # Describe actions take no resource scope, so the grant is on "*" and narrowed to the
  # stack's region by condition instead.
  assert {
    condition     = length([for s in jsondecode(aws_iam_role_policy.sweep_dispatch.policy).Statement : s if s.Sid == "DescribeInstances" && s.Action == "ec2:DescribeInstances" && s.Resource == "*" && try(s.Condition.StringEquals["aws:RequestedRegion"], null) == "eu-west-1"]) == 1
    error_message = "ec2:DescribeInstances must be granted on * and limited to the stack's region."
  }
}
