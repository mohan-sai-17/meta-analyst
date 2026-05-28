# ============================================================================
# IAM — GitHub Actions OIDC (keyless auth from CI/CD to AWS)
# ============================================================================
# GitHub Actions uses OpenID Connect to assume this role.
# No long-lived AWS credentials stored in GitHub — tokens are short-lived
# and auto-expire after each workflow run.

data "aws_caller_identity" "current" {}

# OIDC provider — trust GitHub's token endpoint
resource "aws_iam_openid_connect_provider" "github" {
  url             = "https://token.actions.githubusercontent.com"
  client_id_list  = ["sts.amazonaws.com"]

  # GitHub's OIDC thumbprint (stable, published by GitHub)
  thumbprint_list = ["6938fd4d98bab03faadb97b34396831e3780aea1"]
}

# IAM role — assumed by GitHub Actions workflows
resource "aws_iam_role" "github_actions" {
  name = "meta-analyst-github-actions-${var.environment}"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Principal = {
          Federated = aws_iam_openid_connect_provider.github.arn
        }
        Action = "sts:AssumeRoleWithWebIdentity"
        Condition = {
          StringLike = {
            # Scope to your specific repo — prevents other repos assuming this role
            "token.actions.githubusercontent.com:sub" = "repo:${var.github_repo}:*"
          }
          StringEquals = {
            "token.actions.githubusercontent.com:aud" = "sts.amazonaws.com"
          }
        }
      }
    ]
  })
}

# Policy — permissions for GitHub Actions CI/CD
resource "aws_iam_role_policy" "github_actions_s3" {
  name = "meta-analyst-cicd-policy"
  role = aws_iam_role.github_actions.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "S3DataLakeAccess"
        Effect = "Allow"
        Action = [
          "s3:PutObject",
          "s3:GetObject",
          "s3:DeleteObject",
          "s3:ListBucket",
          "s3:GetBucketLocation"
        ]
        Resource = [
          "arn:aws:s3:::${var.bucket_name}",
          "arn:aws:s3:::${var.bucket_name}/*"
        ]
      },
      {
        Sid    = "ECRAuth"
        Effect = "Allow"
        Action = ["ecr:GetAuthorizationToken"]
        Resource = ["*"]
      },
      {
        Sid    = "ECRPush"
        Effect = "Allow"
        Action = [
          "ecr:BatchCheckLayerAvailability",
          "ecr:InitiateLayerUpload",
          "ecr:UploadLayerPart",
          "ecr:CompleteLayerUpload",
          "ecr:PutImage",
          "ecr:BatchGetImage",
          "ecr:GetDownloadUrlForLayer"
        ]
        Resource = ["arn:aws:ecr:*:${data.aws_caller_identity.current.account_id}:repository/meta-analyst-api"]
      },
      {
        # Deploy code to all three Lambda functions
        Sid    = "LambdaDeploy"
        Effect = "Allow"
        Action = [
          "lambda:CreateFunction",
          "lambda:UpdateFunctionCode",
          "lambda:UpdateFunctionConfiguration",
          "lambda:GetFunction",
          "lambda:GetFunctionConfiguration",
          "lambda:AddPermission",
          "lambda:CreateFunctionUrlConfig",
          "lambda:GetFunctionUrlConfig",
          "lambda:UpdateFunctionUrlConfig",
          "lambda:WaitForFunctionActive",
          "lambda:WaitForFunctionUpdated"
        ]
        Resource = [
          "arn:aws:lambda:*:${data.aws_caller_identity.current.account_id}:function:meta-analyst-api",
          "arn:aws:lambda:*:${data.aws_caller_identity.current.account_id}:function:meta-analyst-pipeline",
          "arn:aws:lambda:*:${data.aws_caller_identity.current.account_id}:function:meta-analyst-billing-check",
        ]
      },
      {
        # Manual fallback: billing_check.yml invokes the billing-check Lambda directly
        Sid    = "LambdaInvoke"
        Effect = "Allow"
        Action = ["lambda:InvokeFunction"]
        Resource = [
          "arn:aws:lambda:*:${data.aws_caller_identity.current.account_id}:function:meta-analyst-billing-check",
        ]
      },
      {
        # Manual fallback: daily_pipeline.yml starts the Step Functions pipeline
        Sid    = "StepFunctionsManualTrigger"
        Effect = "Allow"
        Action = [
          "states:ListStateMachines",
          "states:StartExecution"
        ]
        Resource = ["*"]
      },
      {
        Sid    = "PassLambdaRole"
        Effect = "Allow"
        Action = ["iam:PassRole"]
        Resource = [aws_iam_role.lambda_exec.arn]
      },
      {
        Sid    = "CloudWatchMetrics"
        Effect = "Allow"
        Action = ["cloudwatch:PutMetricData"]
        Resource = ["*"]
      }
    ]
  })
}

# ============================================================================
# IAM — Lambda execution role (API + Pipeline + Billing-check functions)
# ============================================================================

resource "aws_iam_role" "lambda_exec" {
  name = "meta-analyst-lambda-exec-${var.environment}"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Principal = {
          Service = "lambda.amazonaws.com"
        }
        Action = "sts:AssumeRole"
      }
    ]
  })
}

resource "aws_iam_role_policy_attachment" "lambda_basic" {
  role       = aws_iam_role.lambda_exec.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

resource "aws_iam_role_policy" "lambda_data_access" {
  name = "meta-analyst-lambda-data-access"
  role = aws_iam_role.lambda_exec.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        # API Lambda: read mart Parquet — Pipeline Lambda: read raw + write mart + write zip
        Sid    = "S3DataLakeAccess"
        Effect = "Allow"
        Action = [
          "s3:GetObject",
          "s3:PutObject",
          "s3:DeleteObject",
          "s3:ListBucket",
          "s3:GetBucketLocation"
        ]
        Resource = [
          "arn:aws:s3:::${var.bucket_name}",
          "arn:aws:s3:::${var.bucket_name}/*"
        ]
      },
      {
        # Pipeline Lambda (send_alerts) + Billing-check Lambda publish to SNS topic
        Sid    = "SNSPublish"
        Effect = "Allow"
        Action = ["sns:Publish"]
        Resource = ["*"]
      },
      {
        # Pipeline Lambda publishes CloudWatch metrics (OHLCRowsWritten, DailyCandidatesBuilt, etc.)
        Sid    = "CloudWatchMetrics"
        Effect = "Allow"
        Action = ["cloudwatch:PutMetricData"]
        Resource = ["*"]
      }
    ]
  })
}
