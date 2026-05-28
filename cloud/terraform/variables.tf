variable "aws_region" {
  description = "AWS region for all resources"
  type        = string
  default     = "us-east-1"
}

variable "environment" {
  description = "Deployment environment label (e.g. prod, staging)"
  type        = string
  default     = "prod"
}

variable "bucket_name" {
  description = "Globally unique S3 bucket name for the data lake"
  type        = string
}

variable "github_repo" {
  description = "GitHub repo in owner/repo format (e.g. johndoe/meta-analyst)"
  type        = string
}

variable "alert_email" {
  description = "Email address to receive the 10-month account rotation reminder"
  type        = string
}

variable "account_created_date" {
  description = "Date this AWS account was activated — used by billing alert (YYYY-MM-DD)"
  type        = string
}

variable "free_tier_days" {
  description = "Exact number of days the AWS free tier lasts (expiry_date - account_created_date)"
  type        = number
  default     = 181
}

variable "lambda_image_uri" {
  description = "ECR image URI for the Lambda API function (account.dkr.ecr.region.amazonaws.com/repo:tag)"
  type        = string
  default     = "YOUR_AWS_ACCOUNT_ID.dkr.ecr.us-east-1.amazonaws.com/meta-analyst-api:latest"
}

variable "pipeline_image_uri" {
  description = "ECR image URI for the pipeline Lambda function"
  type        = string
  default     = "YOUR_AWS_ACCOUNT_ID.dkr.ecr.us-east-1.amazonaws.com/meta-analyst-api:pipeline-latest"
}
