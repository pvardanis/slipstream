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
  operator_email      = "operator@example.com"
  grafana_external_id = "123456"
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
  # and every period in it must breach: a period with no GPU resets the window.
  assert {
    condition     = aws_cloudwatch_metric_alarm.gpu_spend_leak.period * aws_cloudwatch_metric_alarm.gpu_spend_leak.evaluation_periods == 20 * 3600
    error_message = "The GPU alarm's evaluation window must be 20 hours."
  }

  # Periods align to the clock, so a GPU touching N of them may have run little more than
  # N-1 periods. Short periods keep that slack to minutes: an hourly period would let a
  # run just over 18 hours page.
  assert {
    condition     = aws_cloudwatch_metric_alarm.gpu_spend_leak.period <= 300
    error_message = "The GPU alarm's period must be at most 5 minutes, so it pages within minutes of 20 hours of runtime, not after 18."
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
    condition     = aws_cloudwatch_metric_alarm.gpu_spend_leak.alarm_actions == toset([aws_sns_topic.spend_leak.arn])
    error_message = "The GPU alarm must page through the spend-leak SNS topic and nothing else."
  }
}

run "spend_leak_topic_emails_the_operator" {
  command = plan

  assert {
    condition     = aws_sns_topic.spend_leak.name == "slipstream-spend-leak"
    error_message = "The spend-leak topic must be named slipstream-spend-leak."
  }

  assert {
    condition     = aws_sns_topic_subscription.operator_email.topic_arn == aws_sns_topic.spend_leak.arn
    error_message = "The operator's email subscription must be on the spend-leak topic."
  }
  assert {
    condition     = aws_sns_topic_subscription.operator_email.protocol == "email" && aws_sns_topic_subscription.operator_email.endpoint == "operator@example.com"
    error_message = "The spend-leak topic must email var.operator_email."
  }

  # bench/bootstrap.sh reads the operator email back from this topic's subscription.
  assert {
    condition     = output.spend_leak_topic_arn == "arn:aws:sns:eu-west-1:111122223333:slipstream-spend-leak"
    error_message = "The spend_leak_topic_arn output must be the spend-leak topic's ARN."
  }
}

# Both layers publish as AWS services, so the topic policy must let CloudWatch alarms and
# Budgets publish, each only on behalf of this account.
run "topic_policy_lets_both_layers_publish" {
  command = plan

  assert {
    condition     = aws_sns_topic_policy.spend_leak.arn == aws_sns_topic.spend_leak.arn
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
        && s.Resource == aws_sns_topic.spend_leak.arn
        && s.Condition.StringEquals["aws:SourceAccount"] == "111122223333"
      ]) == 1
    ])
    error_message = "CloudWatch and Budgets must each be allowed to publish to the topic, only for this account."
  }

  # The source ARNs are built from the region and account id; each service may publish
  # only for this account's alarms in this region, and this account's budgets.
  assert {
    condition = toset([
      for s in jsondecode(aws_sns_topic_policy.spend_leak.policy).Statement : s.Condition.ArnLike["aws:SourceArn"]
      ]) == toset([
      "arn:aws:cloudwatch:eu-west-1:111122223333:alarm:*",
      "arn:aws:budgets::111122223333:*",
    ])
    error_message = "The topic policy must confine publishing to this account's alarms in the stack's region and this account's budgets."
  }
}

# The imported budget keeps its console limit and thresholds, and every notification
# pages through the topic.
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
    condition     = length(aws_budgets_budget.monthly.notification) == 4
    error_message = "The imported budget must carry exactly four notifications, none duplicated."
  }
  assert {
    condition = alltrue([
      for n in aws_budgets_budget.monthly.notification :
      n.subscriber_sns_topic_arns == toset([aws_sns_topic.spend_leak.arn]) && (n.subscriber_email_addresses == null ? 0 : length(n.subscriber_email_addresses)) == 0
    ])
    error_message = "Every budget notification must route to the spend-leak topic only, so the operator gets one email per alert."
  }
}

# A typo in the sensitive address is hidden in plan output, so it fails validation instead
# of sending the confirmation link nowhere.
run "operator_email_rejects_a_malformed_address" {
  command = plan

  variables {
    operator_email = "operator.example.com"
  }

  expect_failures = [var.operator_email]
}

# Until the operator clicks the confirmation link, both layers publish into nothing, so
# every plan and apply warns while the subscription is pending.
run "pending_email_subscription_warns" {
  command = plan

  override_resource {
    target          = aws_sns_topic_subscription.operator_email
    override_during = plan
    values = {
      pending_confirmation = true
    }
  }

  expect_failures = [check.operator_email_confirmed]
}
