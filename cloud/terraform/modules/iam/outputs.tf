output "github_actions_role_arn" {
  description = "Add this as AWS_ROLE_ARN in GitHub Repository Secrets"
  value       = aws_iam_role.github_actions.arn
}

output "lambda_exec_role_arn" {
  description = "IAM role ARN for Lambda functions (Phase 4)"
  value       = aws_iam_role.lambda_exec.arn
}
