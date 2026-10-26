# The spend-leak guard (ADR-0025): two AWS-native layers that email the operator through
# one SNS topic when the account bills with no session running. They live in the
# bootstrap stack, outside the cluster they watch, so they survive the failed
# `just cluster-down` they exist to catch. The fast layer is a CloudWatch alarm on
# running on-demand G-class vCPUs; the slow catch-all is the monthly AWS Budget, which
# also sees the NAT, the ALBs, the bench host and the control plane.

resource "aws_sns_topic" "spend_leak" {
  name = "slipstream-spend-leak"
}

# Email subscriptions stay pending until the operator clicks the confirmation link AWS
# sends on apply; nothing is delivered before that.
resource "aws_sns_topic_subscription" "operator_email" {
  topic_arn = aws_sns_topic.spend_leak.arn
  protocol  = "email"
  endpoint  = var.operator_email
}

# A pending subscription leaves both layers publishing into nothing, and apply still
# succeeds, so every plan and apply warns until the link is clicked.
check "operator_email_confirmed" {
  assert {
    condition     = !aws_sns_topic_subscription.operator_email.pending_confirmation
    error_message = "The spend-leak email subscription is pending: click the confirmation link AWS emailed to var.operator_email, or no alarm or budget alert is delivered."
  }
}

# This policy replaces the topic's default, so it must allow both publishers: CloudWatch
# alarms and Budgets, each as an AWS service principal confined to this account's alarms
# and budgets.
resource "aws_sns_topic_policy" "spend_leak" {
  arn = aws_sns_topic.spend_leak.arn

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "CloudWatchAlarmsPublish"
        Effect    = "Allow"
        Principal = { Service = "cloudwatch.amazonaws.com" }
        Action    = "sns:Publish"
        Resource  = aws_sns_topic.spend_leak.arn
        Condition = {
          StringEquals = { "aws:SourceAccount" = local.account_id }
          ArnLike      = { "aws:SourceArn" = "arn:aws:cloudwatch:${var.region}:${local.account_id}:alarm:*" }
        }
      },
      {
        Sid       = "BudgetsPublish"
        Effect    = "Allow"
        Principal = { Service = "budgets.amazonaws.com" }
        Action    = "sns:Publish"
        Resource  = aws_sns_topic.spend_leak.arn
        Condition = {
          StringEquals = { "aws:SourceAccount" = local.account_id }
          ArnLike      = { "aws:SourceArn" = "arn:aws:budgets::${local.account_id}:*" }
        }
      },
    ]
  })
}

# AWS/Usage publishes the account's running on-demand G-family vCPUs at one-minute
# resolution. Five-minute Maximum periods, all 240 of which must breach, page once a GPU
# has touched 20 hours of consecutive clock-aligned periods, so after 19h55m to 20h of
# runtime: above the longest legitimate session (an 18-hour benchmark run), and about $22
# of g5.xlarge on-demand spend. The count covers on-demand capacity only; a spot GPU leak
# reaches only the budget.
resource "aws_cloudwatch_metric_alarm" "gpu_spend_leak" {
  alarm_name        = "slipstream-gpu-spend-leak"
  alarm_description = "On-demand G-class vCPUs have been running for 20 hours: a GPU is likely left up with no session running (ADR-0025)."

  namespace   = "AWS/Usage"
  metric_name = "ResourceCount"
  dimensions = {
    Service  = "EC2"
    Type     = "Resource"
    Resource = "vCPU"
    Class    = "G/OnDemand"
  }

  statistic           = "Maximum"
  period              = 300
  evaluation_periods  = 240
  datapoints_to_alarm = 240
  comparison_operator = "GreaterThanThreshold"
  threshold           = 0

  # No running GPU can mean no datapoint at all, which is no leak.
  treat_missing_data = "notBreaching"

  alarm_actions = [aws_sns_topic.spend_leak.arn]
}

# The budget exists outside Terraform; the import adopts it in place with its limit and
# thresholds, so it keeps its spend history.
import {
  to = aws_budgets_budget.monthly
  id = "${local.account_id}:${local.monthly_budget_name}"
}

locals {
  # The existing budget's name, which the import binds to.
  monthly_budget_name = "slipstream-monthly"

  monthly_budget_notifications = [
    { type = "ACTUAL", threshold = 50 },
    { type = "ACTUAL", threshold = 75 },
    { type = "ACTUAL", threshold = 90 },
    { type = "FORECASTED", threshold = 100 },
  ]
}

# Budgets refreshes up to three times a day, so this layer pages hours after spend
# crosses a threshold; it is the catch-all, not the fast path.
resource "aws_budgets_budget" "monthly" {
  name         = local.monthly_budget_name
  budget_type  = "COST"
  time_unit    = "MONTHLY"
  limit_amount = "200.0"
  limit_unit   = "USD"

  # Budgets checks it may publish to the topic when it saves an SNS subscriber.
  depends_on = [aws_sns_topic_policy.spend_leak]

  dynamic "notification" {
    for_each = local.monthly_budget_notifications
    content {
      notification_type         = notification.value.type
      comparison_operator       = "GREATER_THAN"
      threshold                 = notification.value.threshold
      threshold_type            = "PERCENTAGE"
      subscriber_sns_topic_arns = [aws_sns_topic.spend_leak.arn]
    }
  }
}
