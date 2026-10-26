# Plan-level tests for Grafana Cloud's footing in the bootstrap stack (ADR-0026): the two
# Secrets Manager secrets the Grafana Cloud tokens live in, and the Grafana Assume Role
# IAM role Grafana Cloud reads CloudWatch through. They run offline (mocked aws provider):
# the assertions check that Terraform creates the secrets but never a value for them, and
# that only Grafana Cloud's account, presenting this stack's external ID, can assume a
# role that reads CloudWatch metrics and nothing else. Grafana actually assuming it is
# checked by the data source's Save & test in the setup wizard.
mock_provider "aws" {
  source = "./tests/mocks"
}
mock_provider "random" {}

# No defaults, so every run supplies the spend-leak topic's email address and the Grafana
# Cloud stack's external ID.
variables {
  operator_email      = "operator@example.com"
  grafana_external_id = "123456"
}

run "grafana_cloud_secrets_hold_no_terraform_value" {
  command = plan

  assert {
    condition     = aws_secretsmanager_secret.grafana_cloud_write.name == "grafana-cloud/write"
    error_message = "The write token's secret must be named grafana-cloud/write."
  }
  assert {
    condition     = aws_secretsmanager_secret.grafana_cloud_api.name == "grafana-cloud/api"
    error_message = "The Terraform provider token's secret must be named grafana-cloud/api."
  }

  # The wizard writes each token with `aws secretsmanager put-secret-value`, so no token
  # enters this stack's state. A secret version resource anywhere in the stack would put
  # a value there, so the stack's own files are scanned for one.
  assert {
    condition = length([
      for f in fileset(path.module, "*.tf") : f
      if can(regex("resource\\s+\"aws_secretsmanager_secret_version\"", file("${path.module}/${f}")))
    ]) == 0
    error_message = "The bootstrap stack must not manage an aws_secretsmanager_secret_version: the wizard writes the Grafana Cloud tokens so they stay out of Terraform state."
  }
}

# The assertions decode the role's trust document and its inline policy's document, so
# loosening either fails them; a scan of the stack's files fails any other grant to it.
run "grafana_assume_role_trust_and_permissions" {
  command = plan

  assert {
    condition     = length(jsondecode(aws_iam_role.grafana_cloudwatch.assume_role_policy).Statement) == 1
    error_message = "The Grafana Assume Role trust policy must carry only the Grafana Cloud statement."
  }
  # Grafana Cloud's shared AWS account is the principal; the external ID is what stops
  # another Grafana customer from assuming this role through that account.
  assert {
    condition     = jsondecode(aws_iam_role.grafana_cloudwatch.assume_role_policy).Statement[0].Principal.AWS == "arn:aws:iam::008923505280:root"
    error_message = "The Grafana Assume Role role must trust Grafana Cloud's AWS account, 008923505280."
  }
  assert {
    condition     = jsondecode(aws_iam_role.grafana_cloudwatch.assume_role_policy).Statement[0].Action == "sts:AssumeRole"
    error_message = "The Grafana Assume Role trust must allow only sts:AssumeRole."
  }
  assert {
    condition     = jsondecode(aws_iam_role.grafana_cloudwatch.assume_role_policy).Statement[0].Condition.StringEquals["sts:ExternalId"] == "123456"
    error_message = "The Grafana Assume Role trust must require var.grafana_external_id as sts:ExternalId."
  }

  # Read-only CloudWatch metrics: the two actions Grafana's CloudWatch data source needs
  # for metrics queries, and nothing else. Neither takes a resource scope.
  assert {
    condition     = length(jsondecode(aws_iam_role_policy.grafana_cloudwatch.policy).Statement) == 1
    error_message = "The Grafana Assume Role policy must carry exactly one statement."
  }
  assert {
    condition = (
      jsondecode(aws_iam_role_policy.grafana_cloudwatch.policy).Statement[0].Effect == "Allow"
      && toset(jsondecode(aws_iam_role_policy.grafana_cloudwatch.policy).Statement[0].Action) == toset(["cloudwatch:GetMetricData", "cloudwatch:ListMetrics"])
      && jsondecode(aws_iam_role_policy.grafana_cloudwatch.policy).Statement[0].Resource == "*"
    )
    error_message = "The Grafana Assume Role policy must grant exactly cloudwatch:GetMetricData and cloudwatch:ListMetrics on *."
  }

  # A second inline policy or a managed policy attachment would widen the role past the
  # policy above, and each names the role as its `role`, as the policy above does. The
  # attachments are not known at plan, so the stack's files are scanned instead.
  assert {
    condition = sum([
      for f in fileset(path.module, "*.tf") :
      length(regexall("role\\s*=\\s*aws_iam_role\\.grafana_cloudwatch\\.", file("${path.module}/${f}")))
    ]) == 1
    error_message = "Only the CloudWatch metrics policy may grant the Grafana Assume Role role permissions."
  }
  assert {
    condition = length([
      for f in fileset(path.module, "*.tf") : f
      if can(regex("managed_policy_arns", file("${path.module}/${f}")))
    ]) == 0
    error_message = "The Grafana Assume Role role must carry no managed policies."
  }
}

# The external ID is the only thing scoping Grafana Cloud's shared account to this
# stack, so an empty one is rejected rather than planned into the trust.
run "grafana_external_id_must_not_be_empty" {
  command = plan

  variables {
    grafana_external_id = " "
  }

  expect_failures = [var.grafana_external_id]
}

# The account ID is the trust's principal, so anything but a 12-digit AWS account ID is
# rejected.
run "grafana_account_id_must_be_an_account_id" {
  command = plan

  variables {
    grafana_account_id = "12345"
  }

  expect_failures = [var.grafana_account_id]
}
