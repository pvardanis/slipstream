# Plan-level tests for the bench host's IAM on the eks stack: the EC2 role, its
# resource-scoped grants (bench-client secret read, bench-results writes, bench-client
# image pull, registry auth, SSM core), and the instance profile the bench-endpoint stack
# launches the host under. They run offline — the aws, helm, and kubernetes providers are
# mocked and the bootstrap remote state is stubbed — so the assertions check least
# privilege one grant at a time without creating a role (ADR-0002 tier-1). The host
# actually booting under this profile is the hand-run `just bench-endpoint-up`.
#
# The policies are jsonencode()'d inline, so their statements are known at plan and
# assertable here.

mock_provider "aws" {
  mock_data "aws_iam_policy_document" {
    defaults = { json = "{\"Version\":\"2012-10-17\",\"Statement\":[]}" }
  }
  mock_data "aws_partition" {
    defaults = {
      partition  = "aws"
      dns_suffix = "amazonaws.com"
    }
  }
  mock_data "aws_availability_zones" {
    defaults = { names = ["eu-west-1a", "eu-west-1b", "eu-west-1c"] }
  }
  mock_data "aws_caller_identity" {
    defaults = {
      account_id = "111122223333"
      arn        = "arn:aws:iam::111122223333:user/test"
    }
  }
  mock_data "aws_iam_session_context" {
    defaults = { issuer_arn = "arn:aws:iam::111122223333:role/test" }
  }
  mock_data "aws_ecr_image" {
    defaults = { image_tags = ["main", "orchestrator-abc123"] }
  }
}

mock_provider "aws" {
  alias = "virginia"
}
mock_provider "helm" {}
mock_provider "kubernetes" {}

override_data {
  target = data.terraform_remote_state.bootstrap
  values = {
    outputs = {
      orchestration_image_repo_url    = "111122223333.dkr.ecr.eu-west-1.amazonaws.com/slipstream/orchestration"
      bench_image_repo_arn            = "arn:aws:ecr:eu-west-1:111122223333:repository/slipstream-bench"
      bench_results_bucket_arn        = "arn:aws:s3:::slipstream-bench-results-abc123"
      sweep_dispatch_role_arn         = "arn:aws:iam::111122223333:role/slipstream-sweep-dispatch"
      grafana_cloud_write_secret_name = "grafana-cloud/write"
      cluster_name                    = "slipstream"
      prefect_namespace               = "prefect"
    }
  }
}

override_data {
  target = data.terraform_remote_state.grafana
  values = {
    outputs = {
      prometheus_remote_write_url      = "https://prometheus-prod-24-prod-eu-west-2.grafana.net/api/prom/push"
      prometheus_remote_write_username = "1234567"
      tempo_otlp_endpoint              = "tempo-prod-10-prod-eu-west-2.grafana.net:443"
      tempo_username                   = "7654321"
    }
  }
}

variables {
  state_bucket = "slipstream-tfstate-abc123"
}

run "bench_host_iam_shape" {
  command = plan

  # A fixed name, not a prefix: the role's ARN is then known before the bench-endpoint
  # stack exists, so a grant to pass it to EC2 can name exactly this one role.
  assert {
    condition     = aws_iam_role.bench_host.name == "slipstream-bench-host"
    error_message = "The bench host role must carry the fixed name <cluster>-bench-host."
  }

  # The instance role is assumable only by EC2: exactly one Allow statement whose
  # principal is the EC2 service. A second statement could widen who can assume it.
  assert {
    condition     = length(jsondecode(aws_iam_role.bench_host.assume_role_policy).Statement) == 1
    error_message = "Bench host assume-role policy must have exactly one statement."
  }
  assert {
    condition     = jsondecode(aws_iam_role.bench_host.assume_role_policy).Statement[0].Effect == "Allow" && jsondecode(aws_iam_role.bench_host.assume_role_policy).Statement[0].Principal.Service == "ec2.amazonaws.com"
    error_message = "Bench host role must be assumable only by the EC2 service."
  }

  # The instance profile wraps exactly this role, under a fixed name the bench-endpoint
  # stack receives as an input variable.
  assert {
    condition     = aws_iam_instance_profile.bench_host.role == aws_iam_role.bench_host.name
    error_message = "The bench host instance profile must wrap the bench host role."
  }
  assert {
    condition     = output.bench_host_instance_profile == aws_iam_instance_profile.bench_host.name && aws_iam_instance_profile.bench_host.name == "slipstream-bench-host"
    error_message = "The stack must output the fixed-name bench host instance profile the bench-endpoint stack launches the host under."
  }

  # Secrets read is scoped to the bench-client secrets the bench-endpoint stack names
  # with the <cluster>-bench-endpoint-client- prefix, never "*", and to the read action
  # only: the host reads its cert/key/CA/api-key and must not mutate or delete the secret.
  # The secret is created per endpoint, after this stack, so its ARN is matched by prefix.
  assert {
    condition     = jsondecode(aws_iam_role_policy.bench_host_secrets.policy).Statement[0].Resource == "arn:aws:secretsmanager:eu-west-1:111122223333:secret:slipstream-bench-endpoint-client-*"
    error_message = "Secrets read must be scoped to the bench-endpoint client secret name prefix in this account and region."
  }
  assert {
    condition     = jsondecode(aws_iam_role_policy.bench_host_secrets.policy).Statement[0].Action == "secretsmanager:GetSecretValue"
    error_message = "Secrets policy must grant only GetSecretValue, not a wider secretsmanager action."
  }
  assert {
    condition     = length(jsondecode(aws_iam_role_policy.bench_host_secrets.policy).Statement) == 1 && jsondecode(aws_iam_role_policy.bench_host_secrets.policy).Statement[0].Effect == "Allow"
    error_message = "Secrets policy must be a single Allow statement; a second statement could grant more."
  }

  # Results write is scoped to the bootstrap stack's bench-results bucket objects and to
  # PutObject only: the host writes measurement JSON, it does not read or delete run data.
  assert {
    condition     = jsondecode(aws_iam_role_policy.bench_host_results.policy).Statement[0].Resource == "arn:aws:s3:::slipstream-bench-results-abc123/*"
    error_message = "Results write must be scoped to the bootstrap stack's bench-results bucket objects."
  }
  assert {
    condition     = jsondecode(aws_iam_role_policy.bench_host_results.policy).Statement[0].Action == "s3:PutObject"
    error_message = "Results policy must grant only PutObject, not a wider s3 action."
  }
  assert {
    condition     = length(jsondecode(aws_iam_role_policy.bench_host_results.policy).Statement) == 1 && jsondecode(aws_iam_role_policy.bench_host_results.policy).Statement[0].Effect == "Allow"
    error_message = "Results policy must be a single Allow statement; a second statement could grant more."
  }

  # ECR image pull is scoped to the bench-client repository ARN, never "*", and to
  # the read/pull actions only: the host pulls the image, it must not push.
  assert {
    condition     = jsondecode(aws_iam_role_policy.bench_host_ecr_pull.policy).Statement[0].Resource == "arn:aws:ecr:eu-west-1:111122223333:repository/slipstream-bench"
    error_message = "ECR pull must be scoped to the bench-client repository ARN."
  }
  assert {
    condition     = tolist(jsondecode(aws_iam_role_policy.bench_host_ecr_pull.policy).Statement[0].Action) == tolist(["ecr:GetDownloadUrlForLayer", "ecr:BatchGetImage", "ecr:BatchCheckLayerAvailability"])
    error_message = "ECR pull policy must grant only the layer/image read actions, not push."
  }
  assert {
    condition     = length(jsondecode(aws_iam_role_policy.bench_host_ecr_pull.policy).Statement) == 1 && jsondecode(aws_iam_role_policy.bench_host_ecr_pull.policy).Statement[0].Effect == "Allow"
    error_message = "ECR pull policy must be a single Allow statement; a second statement could grant more."
  }

  # The one deliberately wildcard-scoped policy: ecr:GetAuthorizationToken is a
  # registry-wide action AWS won't let you scope to a repository. Pin it to that
  # single action so a regression can't widen the wildcard grant to, say, ecr:*.
  assert {
    condition     = jsondecode(aws_iam_role_policy.bench_host_ecr_auth.policy).Statement[0].Resource == "*"
    error_message = "ECR auth policy is registry-wide by necessity; resource must be exactly \"*\"."
  }
  assert {
    condition     = jsondecode(aws_iam_role_policy.bench_host_ecr_auth.policy).Statement[0].Action == "ecr:GetAuthorizationToken"
    error_message = "ECR auth policy must grant only GetAuthorizationToken on the registry-wide wildcard."
  }
  assert {
    condition     = length(jsondecode(aws_iam_role_policy.bench_host_ecr_auth.policy).Statement) == 1 && jsondecode(aws_iam_role_policy.bench_host_ecr_auth.policy).Statement[0].Effect == "Allow"
    error_message = "ECR auth policy must be a single Allow statement; a second statement could widen the wildcard grant."
  }

  # SSM access is the AWS-managed core policy; it is what lets the operator and the
  # worker reach the host without any inbound rule.
  assert {
    condition     = aws_iam_role_policy_attachment.bench_host_ssm.policy_arn == "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
    error_message = "Bench host must attach the SSM managed instance core policy for keyless access."
  }
}
