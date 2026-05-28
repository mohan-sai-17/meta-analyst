variable "alert_email" {
  description = "Email address to receive free-tier expiry reminders"
  type        = string
}

variable "account_created_date" {
  description = "Date this AWS account was activated (YYYY-MM-DD)"
  type        = string
}

variable "free_tier_days" {
  description = "Exact number of days the AWS free tier lasts (date(expiry) - date(created))"
  type        = number
  default     = 181
}

variable "environment" {
  description = "Environment label"
  type        = string
}
