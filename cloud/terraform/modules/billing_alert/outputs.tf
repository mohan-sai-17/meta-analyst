output "sns_topic_arn" {
  description = "ARN of the billing alert SNS topic"
  value       = aws_sns_topic.billing_alert.arn
}

output "alert_15_days_rule_arn" {
  description = "EventBridge rule ARN — fires 15 days before free tier expiry"
  value       = aws_cloudwatch_event_rule.alert_15.arn
}

output "alert_10_days_rule_arn" {
  description = "EventBridge rule ARN — fires 10 days before free tier expiry"
  value       = aws_cloudwatch_event_rule.alert_10.arn
}

output "alert_5_days_rule_arn" {
  description = "EventBridge rule ARN — fires 5 days before free tier expiry"
  value       = aws_cloudwatch_event_rule.alert_5.arn
}

output "alert_1_day_rule_arn" {
  description = "EventBridge rule ARN — final alert 1 day before free tier expiry"
  value       = aws_cloudwatch_event_rule.alert_1.arn
}
