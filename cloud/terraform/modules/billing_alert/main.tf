# ============================================================================
# Billing Alert — 3 countdown alerts before AWS free-tier expiry
#
# Fires at:
#   - 15 days before expiry  (EventBridge one-time rule)
#   - 10 days before expiry  (EventBridge one-time rule)
#   -  5 days before expiry  (EventBridge one-time rule)
#
# Backed up by GitHub Actions daily cron (billing_check.yml) which uses
# exact Python date math and sends SendGrid emails at the same thresholds.
# ============================================================================

locals {
  base_ts = "${var.account_created_date}T09:00:00Z"

  # Exact timestamps for each alert (using free_tier_days for precision)
  alert_ts_15 = timeadd(local.base_ts, "${(var.free_tier_days - 15) * 24}h")
  alert_ts_10 = timeadd(local.base_ts, "${(var.free_tier_days - 10) * 24}h")
  alert_ts_5  = timeadd(local.base_ts, "${(var.free_tier_days -  5) * 24}h")

  # Pre-extract date components (avoids nested quote issues in schedule_expression)
  d15_day  = formatdate("D",    local.alert_ts_15)
  d15_mon  = formatdate("M",    local.alert_ts_15)
  d15_year = formatdate("YYYY", local.alert_ts_15)

  d10_day  = formatdate("D",    local.alert_ts_10)
  d10_mon  = formatdate("M",    local.alert_ts_10)
  d10_year = formatdate("YYYY", local.alert_ts_10)

  d5_day   = formatdate("D",    local.alert_ts_5)
  d5_mon   = formatdate("M",    local.alert_ts_5)
  d5_year  = formatdate("YYYY", local.alert_ts_5)

  alert_ts_1 = timeadd(local.base_ts, "${(var.free_tier_days - 1) * 24}h")
  d1_day   = formatdate("D",    local.alert_ts_1)
  d1_mon   = formatdate("M",    local.alert_ts_1)
  d1_year  = formatdate("YYYY", local.alert_ts_1)

  expiry_ts   = timeadd(local.base_ts, "${var.free_tier_days * 24}h")
  expiry_date = formatdate("YYYY-MM-DD", local.expiry_ts)
}

# -- SNS Topic --
resource "aws_sns_topic" "billing_alert" {
  name = "meta-analyst-billing-alert-${var.environment}"
}

resource "aws_sns_topic_subscription" "email" {
  topic_arn = aws_sns_topic.billing_alert.arn
  protocol  = "email"
  endpoint  = var.alert_email
}

# -- EventBridge Rule: 15 days before expiry --
resource "aws_cloudwatch_event_rule" "alert_15" {
  name                = "meta-analyst-alert-15d-${var.environment}"
  description         = "Alert 15 days before AWS free tier expires (${local.expiry_date})"
  schedule_expression = "cron(0 9 ${local.d15_day} ${local.d15_mon} ? ${local.d15_year})"
  state               = "ENABLED"
}

resource "aws_cloudwatch_event_target" "alert_15" {
  rule      = aws_cloudwatch_event_rule.alert_15.name
  target_id = "BillingAlert15d"
  arn       = aws_sns_topic.billing_alert.arn

  input_transformer {
    input_template = "\"META-ANALYST: AWS free tier expires in 15 days (${local.expiry_date}). Start Account B rotation now — see terraform/environments/account_b.tfvars.\""
  }
}

# -- EventBridge Rule: 10 days before expiry --
resource "aws_cloudwatch_event_rule" "alert_10" {
  name                = "meta-analyst-alert-10d-${var.environment}"
  description         = "Alert 10 days before AWS free tier expires (${local.expiry_date})"
  schedule_expression = "cron(0 9 ${local.d10_day} ${local.d10_mon} ? ${local.d10_year})"
  state               = "ENABLED"
}

resource "aws_cloudwatch_event_target" "alert_10" {
  rule      = aws_cloudwatch_event_rule.alert_10.name
  target_id = "BillingAlert10d"
  arn       = aws_sns_topic.billing_alert.arn

  input_transformer {
    input_template = "\"META-ANALYST: AWS free tier expires in 10 days (${local.expiry_date}). Account B rotation must begin immediately.\""
  }
}

# -- EventBridge Rule: 5 days before expiry --
resource "aws_cloudwatch_event_rule" "alert_5" {
  name                = "meta-analyst-alert-5d-${var.environment}"
  description         = "Alert 5 days before AWS free tier expires (${local.expiry_date})"
  schedule_expression = "cron(0 9 ${local.d5_day} ${local.d5_mon} ? ${local.d5_year})"
  state               = "ENABLED"
}

resource "aws_cloudwatch_event_target" "alert_5" {
  rule      = aws_cloudwatch_event_rule.alert_5.name
  target_id = "BillingAlert5d"
  arn       = aws_sns_topic.billing_alert.arn

  input_transformer {
    input_template = "\"META-ANALYST URGENT: AWS free tier expires in 5 days (${local.expiry_date}). Rotate to Account B TODAY.\""
  }
}

# -- EventBridge Rule: 1 day before expiry --
resource "aws_cloudwatch_event_rule" "alert_1" {
  name                = "meta-analyst-alert-1d-${var.environment}"
  description         = "Final alert 1 day before AWS free tier expires (${local.expiry_date})"
  schedule_expression = "cron(0 9 ${local.d1_day} ${local.d1_mon} ? ${local.d1_year})"
  state               = "ENABLED"
}

resource "aws_cloudwatch_event_target" "alert_1" {
  rule      = aws_cloudwatch_event_rule.alert_1.name
  target_id = "BillingAlert1d"
  arn       = aws_sns_topic.billing_alert.arn

  input_transformer {
    input_template = "\"META-ANALYST FINAL WARNING: AWS free tier expires TOMORROW (${local.expiry_date}). Rotate to Account B immediately or you will be charged.\""
  }
}

# -- SNS Topic Policy: allow EventBridge to publish --
resource "aws_sns_topic_policy" "allow_eventbridge" {
  arn = aws_sns_topic.billing_alert.arn

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "AllowEventBridgePublish"
        Effect = "Allow"
        Principal = {
          Service = "events.amazonaws.com"
        }
        Action   = "SNS:Publish"
        Resource = aws_sns_topic.billing_alert.arn
      }
    ]
  })
}
