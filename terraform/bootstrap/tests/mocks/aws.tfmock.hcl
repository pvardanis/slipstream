# Shared mock values for the aws provider across the bootstrap stack's plan tests.

# A fixed account id so account-scoped ARNs and policy conditions are known at plan and
# assertable exactly.
mock_data "aws_caller_identity" {
  defaults = {
    account_id = "111122223333"
    arn        = "arn:aws:iam::111122223333:user/test"
  }
}

# A fixed spend-leak topic ARN, set at plan, so the alarm action, budget subscribers and
# topic policy resource are known and comparable against it.
mock_resource "aws_sns_topic" {
  override_during = plan
  defaults = {
    arn = "arn:aws:sns:eu-west-1:111122223333:slipstream-spend-leak"
  }
}

# A mock provider cannot import, so every plan overrides the imported state of the
# monthly budget instead. Terraform builds that state from the budget's configuration,
# which is why the topic ARN it references must be known at plan; assertions read the
# planned values.
override_resource {
  target          = aws_budgets_budget.monthly
  override_during = plan
  values = {
    id = "111122223333:slipstream-monthly"
  }
}
