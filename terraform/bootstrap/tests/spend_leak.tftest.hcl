# Plan-level tests for the spend-leak guard on the bootstrap stack (ADR-0025): the GPU
# alarm, the imported monthly budget, and the SNS topic both layers page through. They run
# offline (mocked aws provider) and assert the alarm watches running on-demand G-class
# vCPUs for 20 hours, the budget keeps its limit and thresholds, and both layers target
# the one topic that emails the operator. The end-to-end proof (alarm -> SNS -> inbox)
# is the set-alarm-state rehearsal, not this suite.
# Shared mocks (tests/mocks): a fixed account id, a fixed topic ARN set at plan, and the
# budget's overridden import.
mock_provider "aws" {
  source = "./tests/mocks"
}
mock_provider "random" {}

variables {
  operator_email = "operator@example.com"
}

run "gpu_alarm_watches_on_demand_g_vcpus_for_20_hours" {
  command = plan

  assert {
    condition     = aws_cloudwatch_metric_alarm.gpu_spend_leak.namespace == "AWS/Usage" && aws_cloudwatch_metric_alarm.gpu_spend_leak.metric_name == "ResourceCount"
    error_message = "The GPU alarm must watch AWS/Usage ResourceCount."
  }
  assert {
    condition = aws_cloudwatch_metric_alarm.gpu_spend_leak.dimensions == tomap({
      Service  = "EC2"
      Type     = "Resource"
      Resource = "vCPU"
      Class    = "G/OnDemand"
    })
    error_message = "The GPU alarm must be scoped to exactly the account's running on-demand G-class EC2 vCPUs."
  }
  assert {
    condition     = aws_cloudwatch_metric_alarm.gpu_spend_leak.statistic == "Maximum"
    error_message = "The GPU alarm must read the Maximum, so any running GPU vCPU in a period counts."
  }

  # 20 hours sits above the longest legitimate GPU session (an 18-hour benchmark run),
  # and every period in it must breach: a single hour with no GPU resets the window.
  assert {
    condition     = aws_cloudwatch_metric_alarm.gpu_spend_leak.period * aws_cloudwatch_metric_alarm.gpu_spend_leak.evaluation_periods == 20 * 3600
    error_message = "The GPU alarm's evaluation window must be 20 hours."
  }
  assert {
    condition     = aws_cloudwatch_metric_alarm.gpu_spend_leak.datapoints_to_alarm == aws_cloudwatch_metric_alarm.gpu_spend_leak.evaluation_periods
    error_message = "Every period in the 20-hour window must breach before the GPU alarm fires."
  }
  assert {
    condition     = aws_cloudwatch_metric_alarm.gpu_spend_leak.comparison_operator == "GreaterThanThreshold" && aws_cloudwatch_metric_alarm.gpu_spend_leak.threshold == 0
    error_message = "The GPU alarm must breach while running G-class vCPUs are above 0."
  }

  # No running GPU may publish no datapoint at all; that must count as no leak, not hold
  # the alarm in its last state.
  assert {
    condition     = aws_cloudwatch_metric_alarm.gpu_spend_leak.treat_missing_data == "notBreaching"
    error_message = "Missing GPU vCPU data must count as not breaching."
  }
  assert {
    condition     = aws_cloudwatch_metric_alarm.gpu_spend_leak.alarm_actions == toset(["arn:aws:sns:eu-west-1:111122223333:slipstream-spend-leak"])
    error_message = "The GPU alarm must page through the spend-leak SNS topic and nothing else."
  }
}

run "spend_leak_topic_emails_the_operator" {
  command = plan

  assert {
    condition     = aws_sns_topic_subscription.operator_email.topic_arn == "arn:aws:sns:eu-west-1:111122223333:slipstream-spend-leak"
    error_message = "The operator's email subscription must be on the spend-leak topic."
  }
  assert {
    condition     = aws_sns_topic_subscription.operator_email.protocol == "email" && aws_sns_topic_subscription.operator_email.endpoint == "operator@example.com"
    error_message = "The spend-leak topic must email var.operator_email."
  }
}

# Both layers publish as AWS services, so the topic policy must let CloudWatch alarms and
# Budgets publish, each only on behalf of this account.
run "topic_policy_lets_both_layers_publish" {
  command = plan

  assert {
    condition     = aws_sns_topic_policy.spend_leak.arn == "arn:aws:sns:eu-west-1:111122223333:slipstream-spend-leak"
    error_message = "The publish policy must be attached to the spend-leak topic."
  }
  assert {
    condition     = length(jsondecode(aws_sns_topic_policy.spend_leak.policy).Statement) == 2
    error_message = "The topic policy must carry exactly the CloudWatch and Budgets publish statements."
  }
  assert {
    condition = alltrue([
      for service in ["cloudwatch.amazonaws.com", "budgets.amazonaws.com"] :
      length([
        for s in jsondecode(aws_sns_topic_policy.spend_leak.policy).Statement : s
        if s.Principal.Service == service
        && s.Action == "sns:Publish"
        && s.Resource == "arn:aws:sns:eu-west-1:111122223333:slipstream-spend-leak"
        && s.Condition.StringEquals["aws:SourceAccount"] == "111122223333"
      ]) == 1
    ])
    error_message = "CloudWatch and Budgets must each be allowed to publish to the topic, only for this account."
  }
}

# The budget was made in the console and is imported with its limit and thresholds
# unchanged; only its notifications move from a direct email to the topic.
run "monthly_budget_keeps_its_limit_and_thresholds" {
  command = plan

  assert {
    condition     = aws_budgets_budget.monthly.name == "slipstream-monthly"
    error_message = "The imported budget must keep its name, slipstream-monthly."
  }
  assert {
    condition     = aws_budgets_budget.monthly.budget_type == "COST" && aws_budgets_budget.monthly.time_unit == "MONTHLY"
    error_message = "The imported budget must stay a monthly cost budget."
  }
  assert {
    condition     = tonumber(aws_budgets_budget.monthly.limit_amount) == 200 && aws_budgets_budget.monthly.limit_unit == "USD"
    error_message = "The imported budget must keep its $200 limit."
  }
  assert {
    condition = toset([
      for n in aws_budgets_budget.monthly.notification : "${n.notification_type}:${n.comparison_operator}:${n.threshold}:${n.threshold_type}"
      ]) == toset([
      "ACTUAL:GREATER_THAN:50:PERCENTAGE",
      "ACTUAL:GREATER_THAN:75:PERCENTAGE",
      "ACTUAL:GREATER_THAN:90:PERCENTAGE",
      "FORECASTED:GREATER_THAN:100:PERCENTAGE",
    ])
    error_message = "The imported budget must keep exactly its 50/75/90% actual and 100% forecast notifications."
  }
  assert {
    condition = alltrue([
      for n in aws_budgets_budget.monthly.notification :
      n.subscriber_sns_topic_arns == toset(["arn:aws:sns:eu-west-1:111122223333:slipstream-spend-leak"]) && try(length(n.subscriber_email_addresses), 0) == 0
    ])
    error_message = "Every budget notification must route to the spend-leak topic only, so the operator gets one email per alert."
  }
}
