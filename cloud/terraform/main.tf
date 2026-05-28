terraform {
  required_version = ">= 1.0"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

provider "aws" {
  region = var.aws_region

  default_tags {
    tags = {
      Project     = "meta-analyst"
      Environment = var.environment
      ManagedBy   = "terraform"
    }
  }
}

# ============================================================================
# MODULE: S3 Data Lake
# ============================================================================

module "s3_lake" {
  source      = "./modules/s3_lake"
  bucket_name = var.bucket_name
  environment = var.environment
}

# ============================================================================
# MODULE: IAM
# ============================================================================

module "iam" {
  source      = "./modules/iam"
  bucket_name = var.bucket_name
  github_repo = var.github_repo
  environment = var.environment

  depends_on = [module.s3_lake]
}

# ============================================================================
# ECR — Container registry for the Lambda API image
# ============================================================================

resource "aws_ecr_repository" "api" {
  name                 = "meta-analyst-api"
  image_tag_mutability = "MUTABLE"

  image_scanning_configuration {
    scan_on_push = true
  }
}

resource "aws_ecr_lifecycle_policy" "api" {
  repository = aws_ecr_repository.api.name

  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "Keep only the 5 most recent images"
      selection = {
        tagStatus   = "any"
        countType   = "imageCountMoreThan"
        countNumber = 5
      }
      action = { type = "expire" }
    }]
  })
}

# Allow Lambda service to pull images from this ECR repository.
# Required for Lambda container image functions — Lambda uses its own
# service credentials (not the execution role) to fetch the image on deploy.
resource "aws_ecr_repository_policy" "api" {
  repository = aws_ecr_repository.api.name

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "LambdaECRImageRetrievalPolicy"
        Effect = "Allow"
        Principal = {
          Service = "lambda.amazonaws.com"
        }
        Action = [
          "ecr:BatchGetImage",
          "ecr:GetDownloadUrlForLayer"
        ]
      }
    ]
  })
}

# ============================================================================
# LAMBDA — API function (container image from ECR)
# ============================================================================
# The image URI is pinned via the lambda_image_uri variable (defaults to :latest).
# The GitHub Actions deploy workflow updates the function code after each push.

resource "aws_lambda_function" "api" {
  function_name = "meta-analyst-api"
  role          = module.iam.lambda_exec_role_arn
  package_type  = "Image"
  image_uri     = var.lambda_image_uri
  timeout       = 120
  memory_size   = 512

  environment {
    variables = {
      S3_BUCKET                  = var.bucket_name
      DUCKDB_EXTENSION_DIRECTORY = "/var/task/.duckdb_ext"
    }
  }

  depends_on = [aws_ecr_repository.api, module.iam]
}

# Public Function URL — no API Gateway needed (free tier friendly)
resource "aws_lambda_function_url" "api" {
  function_name      = aws_lambda_function.api.function_name
  authorization_type = "NONE"

  cors {
    allow_origins = ["*"]
    allow_methods = ["GET"]
    allow_headers = ["*"]
  }
}

# Statement 1 — Allow the Function URL endpoint to be called publicly.
resource "aws_lambda_permission" "api_url_invoke_url" {
  function_name          = aws_lambda_function.api.function_name
  statement_id           = "FunctionURLPublicAccess"
  action                 = "lambda:InvokeFunctionUrl"
  principal              = "*"
  function_url_auth_type = "NONE"
}

# Statement 2 — Allow anonymous callers to actually invoke the Lambda function.
# Lambda Function URLs require BOTH permissions: InvokeFunctionUrl (above) AND
# InvokeFunction (this one). Without this, the Function URL returns 403.
resource "aws_lambda_permission" "api_url_invoke_fn" {
  function_name = aws_lambda_function.api.function_name
  statement_id  = "AllowPublicInvoke"
  action        = "lambda:InvokeFunction"
  principal     = "*"
}

# ============================================================================
# SNS + CloudWatch Alarms — Pipeline health notifications
# ============================================================================

resource "aws_sns_topic" "pipeline_alerts" {
  name = "meta-analyst-pipeline-alerts-${var.environment}"
}

resource "aws_sns_topic_subscription" "pipeline_alerts_email" {
  topic_arn = aws_sns_topic.pipeline_alerts.arn
  protocol  = "email"
  endpoint  = var.alert_email
}

resource "aws_cloudwatch_metric_alarm" "no_candidates" {
  alarm_name          = "meta-analyst-no-candidates-${var.environment}"
  namespace           = "MetaAnalyst/Pipeline"
  metric_name         = "DailyCandidatesBuilt"
  statistic           = "Sum"
  period              = 86400
  evaluation_periods  = 1
  threshold           = 1
  comparison_operator = "LessThanThreshold"
  treat_missing_data  = "breaching"
  alarm_description   = "Pipeline built 0 daily candidates"
  alarm_actions       = [aws_sns_topic.pipeline_alerts.arn]
}

resource "aws_cloudwatch_metric_alarm" "data_quality" {
  alarm_name          = "meta-analyst-data-quality-${var.environment}"
  namespace           = "MetaAnalyst/Pipeline"
  metric_name         = "DataQualityScore"
  statistic           = "Minimum"
  period              = 86400
  evaluation_periods  = 1
  threshold           = 100
  comparison_operator = "LessThanThreshold"
  treat_missing_data  = "breaching"
  alarm_description   = "Data quality check failed"
  alarm_actions       = [aws_sns_topic.pipeline_alerts.arn]
}

# ============================================================================
# MODULE: Billing Alert (CloudWatch-based countdown before free tier expiry)
# ============================================================================

module "billing_alert" {
  source               = "./modules/billing_alert"
  alert_email          = var.alert_email
  account_created_date = var.account_created_date
  free_tier_days       = var.free_tier_days
  environment          = var.environment
}

# ============================================================================
# LAMBDA — Pipeline function (zip from S3, dispatches 9 pipeline steps)
# ============================================================================
# GitHub Actions builds the zip (pip install + scripts + sql/) and uploads it
# to s3://BUCKET/lambda/pipeline.zip before calling update-function-code.
# Terraform manages the function config; CI manages the code content.

resource "aws_lambda_function" "pipeline" {
  function_name = "meta-analyst-pipeline"
  role          = module.iam.lambda_exec_role_arn
  package_type  = "Image"
  image_uri     = var.pipeline_image_uri
  timeout       = 900    # 15 minutes — backtest + mart compute can be slow
  memory_size   = 1024   # 1 GB — gives DuckDB headroom for 10-year datasets

  environment {
    variables = {
      S3_BUCKET              = var.bucket_name
      SNS_TOPIC_ARN          = aws_sns_topic.pipeline_alerts.arn
      SQL_ROOT               = "/var/task"
      DUCKDB_EXTENSION_DIRECTORY = "/var/task/.duckdb_ext"
    }
  }

  # Terraform manages config; GitHub Actions updates the image via update-function-code.
  lifecycle {
    ignore_changes = [image_uri]
  }

  depends_on = [aws_ecr_repository.api, module.iam]
}

# ============================================================================
# LAMBDA — Billing check function (stdlib + boto3 only, no extra deps)
# ============================================================================

resource "aws_lambda_function" "billing_check" {
  function_name = "meta-analyst-billing-check"
  role          = module.iam.lambda_exec_role_arn
  package_type  = "Zip"
  s3_bucket     = var.bucket_name
  s3_key        = "lambda/billing_check.zip"
  handler       = "billing_check.lambda_handler"
  runtime       = "python3.11"
  timeout       = 60
  memory_size   = 128

  environment {
    variables = {
      ACCOUNT_CREATED_DATE = var.account_created_date
      SNS_TOPIC_ARN        = aws_sns_topic.pipeline_alerts.arn
    }
  }

  lifecycle {
    ignore_changes = [s3_key, s3_object_version]
  }

  depends_on = [module.iam]
}

# ============================================================================
# IAM — Step Functions execution role (invokes pipeline Lambda)
# ============================================================================

resource "aws_iam_role" "sfn" {
  name = "meta-analyst-sfn-exec-${var.environment}"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "states.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy" "sfn" {
  name = "meta-analyst-sfn-invoke-lambda"
  role = aws_iam_role.sfn.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["lambda:InvokeFunction"]
      Resource = [aws_lambda_function.pipeline.arn]
    }]
  })
}

# ============================================================================
# IAM — EventBridge Scheduler execution role
# ============================================================================

resource "aws_iam_role" "scheduler" {
  name = "meta-analyst-scheduler-exec-${var.environment}"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "scheduler.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy" "scheduler" {
  name = "meta-analyst-scheduler-targets"
  role = aws_iam_role.scheduler.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "StartPipelineStateMachine"
        Effect   = "Allow"
        Action   = ["states:StartExecution"]
        Resource = [aws_sfn_state_machine.pipeline.arn]
      },
      {
        Sid      = "InvokeBillingCheck"
        Effect   = "Allow"
        Action   = ["lambda:InvokeFunction"]
        Resource = [aws_lambda_function.billing_check.arn]
      }
    ]
  })
}

# ============================================================================
# STEP FUNCTIONS — Daily pipeline state machine (9 sequential steps)
# ============================================================================

resource "aws_sfn_state_machine" "pipeline" {
  name     = "meta-analyst-pipeline-${var.environment}"
  role_arn = aws_iam_role.sfn.arn

  definition = jsonencode({
    Comment = "Meta-Analyst daily pipeline — 9 sequential steps"
    StartAt = "UpdateOHLC"
    States = {
      UpdateOHLC = {
        Type     = "Task"
        Resource = "arn:aws:states:::lambda:invoke"
        Parameters = {
          FunctionName = aws_lambda_function.pipeline.arn
          Payload      = { step = "ohlc" }
        }
        ResultPath = null
        Next       = "UpdateRatings"
      }
      UpdateRatings = {
        Type     = "Task"
        Resource = "arn:aws:states:::lambda:invoke"
        Parameters = {
          FunctionName = aws_lambda_function.pipeline.arn
          Payload      = { step = "ratings" }
        }
        ResultPath = null
        Next       = "BootstrapSectors"
      }
      BootstrapSectors = {
        Type     = "Task"
        Resource = "arn:aws:states:::lambda:invoke"
        Parameters = {
          FunctionName = aws_lambda_function.pipeline.arn
          Payload      = { step = "sectors" }
        }
        ResultPath = null
        Next       = "UpdateFundamentals"
      }
      UpdateFundamentals = {
        Type     = "Task"
        Resource = "arn:aws:states:::lambda:invoke"
        Parameters = {
          FunctionName = aws_lambda_function.pipeline.arn
          Payload      = { step = "fundamentals" }
        }
        ResultPath = null
        Next       = "UpdateInsider"
      }
      UpdateInsider = {
        Type     = "Task"
        Resource = "arn:aws:states:::lambda:invoke"
        Parameters = {
          FunctionName = aws_lambda_function.pipeline.arn
          Payload      = { step = "insider" }
        }
        ResultPath = null
        Next       = "ComputeMarts"
      }
      ComputeMarts = {
        Type     = "Task"
        Resource = "arn:aws:states:::lambda:invoke"
        Parameters = {
          FunctionName = aws_lambda_function.pipeline.arn
          Payload      = { step = "marts" }
        }
        ResultPath = null
        Next       = "ValidateData"
      }
      ValidateData = {
        Type     = "Task"
        Resource = "arn:aws:states:::lambda:invoke"
        Parameters = {
          FunctionName = aws_lambda_function.pipeline.arn
          Payload      = { step = "validate" }
        }
        ResultPath = null
        Next       = "RunBacktest"
      }
      RunBacktest = {
        Type     = "Task"
        Resource = "arn:aws:states:::lambda:invoke"
        Parameters = {
          FunctionName = aws_lambda_function.pipeline.arn
          Payload      = { step = "backtest" }
        }
        ResultPath = null
        Next       = "SendAlerts"
      }
      SendAlerts = {
        Type     = "Task"
        Resource = "arn:aws:states:::lambda:invoke"
        Parameters = {
          FunctionName = aws_lambda_function.pipeline.arn
          Payload      = { step = "alerts" }
        }
        ResultPath = null
        End        = true
      }
    }
  })

  depends_on = [aws_iam_role_policy.sfn]
}

# ============================================================================
# EVENTBRIDGE SCHEDULER — Daily pipeline (weekdays 14:00 UTC)
# ============================================================================

resource "aws_scheduler_schedule" "pipeline" {
  name                         = "meta-analyst-daily-pipeline-${var.environment}"
  schedule_expression          = "cron(0 14 ? * MON-FRI *)"
  schedule_expression_timezone = "UTC"

  flexible_time_window {
    mode = "OFF"
  }

  target {
    arn      = aws_sfn_state_machine.pipeline.arn
    role_arn = aws_iam_role.scheduler.arn
    input    = jsonencode({})
  }

  depends_on = [aws_iam_role_policy.scheduler]
}

# ============================================================================
# EVENTBRIDGE SCHEDULER — Billing check (daily 09:00 UTC)
# ============================================================================

resource "aws_scheduler_schedule" "billing_check" {
  name                         = "meta-analyst-billing-check-${var.environment}"
  schedule_expression          = "cron(0 9 * * ? *)"
  schedule_expression_timezone = "UTC"

  flexible_time_window {
    mode = "OFF"
  }

  target {
    arn      = aws_lambda_function.billing_check.arn
    role_arn = aws_iam_role.scheduler.arn
    input    = jsonencode({})
  }

  depends_on = [aws_iam_role_policy.scheduler]
}
