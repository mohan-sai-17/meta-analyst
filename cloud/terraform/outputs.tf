output "s3_bucket_name" {
  description = "Name of the data lake S3 bucket"
  value       = module.s3_lake.bucket_name
}

output "s3_bucket_arn" {
  description = "ARN of the data lake S3 bucket"
  value       = module.s3_lake.bucket_arn
}

output "github_actions_role_arn" {
  description = "ARN of the IAM role assumed by GitHub Actions (add to GitHub Secrets as AWS_ROLE_ARN)"
  value       = module.iam.github_actions_role_arn
}

output "sns_billing_alert_arn" {
  description = "ARN of the SNS billing alert topic"
  value       = module.billing_alert.sns_topic_arn
}

output "billing_alert_15d_rule_arn" {
  description = "EventBridge rule — 15 days before free tier expiry"
  value       = module.billing_alert.alert_15_days_rule_arn
}

output "billing_alert_10d_rule_arn" {
  description = "EventBridge rule — 10 days before free tier expiry"
  value       = module.billing_alert.alert_10_days_rule_arn
}

output "billing_alert_5d_rule_arn" {
  description = "EventBridge rule — 5 days before free tier expiry"
  value       = module.billing_alert.alert_5_days_rule_arn
}

output "billing_alert_1d_rule_arn" {
  description = "EventBridge rule — 1 day before free tier expiry (final warning)"
  value       = module.billing_alert.alert_1_day_rule_arn
}

output "lambda_exec_role_arn" {
  description = "ARN of the Lambda execution IAM role (add to GitHub Secrets as LAMBDA_EXEC_ROLE_ARN)"
  value       = module.iam.lambda_exec_role_arn
}

output "ecr_repository_url" {
  description = "ECR repository URL for the Lambda API image"
  value       = aws_ecr_repository.api.repository_url
}

output "api_function_url" {
  description = "Public URL of the Lambda API (no API Gateway needed)"
  value       = aws_lambda_function_url.api.function_url
}
