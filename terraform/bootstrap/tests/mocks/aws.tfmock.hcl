# Shared mock values for the aws provider across the bootstrap stack's plan tests.

# A fixed account id so account-scoped ARNs and policy conditions are known at plan and
# assertable exactly.
mock_data "aws_caller_identity" {
  defaults = {
    account_id = "111122223333"
    arn        = "arn:aws:iam::111122223333:user/test"
  }
}

# A fixed ARN for the spend-leak topic alone, set at plan, so the alarm action, budget
# subscribers and topic policy resource are known there; any other topic stays unknown and
# cannot pass for it.
override_resource {
  target          = aws_sns_topic.spend_leak
  override_during = plan
  values = {
    arn = "arn:aws:sns:eu-west-1:111122223333:slipstream-spend-leak"
  }
}

# A confirmed email subscription at plan, so the confirmation check evaluates instead of
# waiting on apply; a run overrides it to pending to prove the check fires.
mock_resource "aws_sns_topic_subscription" {
  override_during = plan
  defaults = {
    pending_confirmation = false
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
