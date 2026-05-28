"""
billing_check.py
================
AWS Lambda that monitors the $200 free-tier credit expiry and
publishes an SNS alert at 15, 10, 5, and 1 days before expiration.

Replaces the GitHub Actions billing_check.yml cron job.
Triggered daily at 09:00 UTC by EventBridge Scheduler.

Environment variables:
  ACCOUNT_CREATED_DATE — ISO date the AWS account was created (YYYY-MM-DD)
  SNS_TOPIC_ARN        — ARN of the SNS topic to publish alerts to
  AWS_DEFAULT_REGION   — AWS region (auto-set by Lambda runtime)
"""

import os
import calendar
import boto3
from datetime import date

SNS_TOPIC_ARN = os.environ.get("SNS_TOPIC_ARN", "")
AWS_REGION    = os.environ.get("AWS_DEFAULT_REGION", "us-east-1")

ALERT_THRESHOLDS = {15, 10, 5, 1}


def _add_months(d, months):
    """Add calendar months to a date, clamping day to month-end."""
    m     = d.month - 1 + months
    year  = d.year + m // 12
    month = m % 12 + 1
    day   = min(d.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


def _publish_sns(subject, body):
    """Publish an alert to SNS. Non-fatal if SNS_TOPIC_ARN is missing."""
    if not SNS_TOPIC_ARN:
        print("[BILLING] SNS_TOPIC_ARN not set — alert skipped.")
        return
    boto3.client("sns", region_name=AWS_REGION).publish(
        TopicArn=SNS_TOPIC_ARN,
        Subject=subject,
        Message=body,
    )
    print(f"[BILLING] SNS alert sent: {subject}")


def lambda_handler(event, context):
    created_str = os.environ.get("ACCOUNT_CREATED_DATE", "").strip()
    if not created_str:
        print("[BILLING] ACCOUNT_CREATED_DATE not set — nothing to check.")
        return {"status": "skipped"}

    created        = date.fromisoformat(created_str)
    expiration     = _add_months(created, 6)
    today          = date.today()
    days_remaining = (expiration - today).days

    print(f"[BILLING] Account created : {created}")
    print(f"[BILLING] Free tier ends  : {expiration}")
    print(f"[BILLING] Today           : {today}")
    print(f"[BILLING] Days remaining  : {days_remaining}")

    if days_remaining < 0:
        body = (
            f"Your AWS free-tier credit expired {abs(days_remaining)} day(s) ago "
            f"(on {expiration}).\n\n"
            "URGENT: Rotate to Account B immediately to avoid charges."
        )
        _publish_sns("META-ANALYST: AWS Free Tier EXPIRED", body)
        return {"status": "expired", "days_remaining": days_remaining}

    if days_remaining in ALERT_THRESHOLDS:
        body = (
            f"Your AWS free-tier credit expires in {days_remaining} day(s) "
            f"(on {expiration}).\n\n"
            "Action required: rotate to Account B.\n"
            "See aws/terraform/ for migration steps."
        )
        _publish_sns(
            f"META-ANALYST: AWS Free Tier Expires in {days_remaining} Days",
            body,
        )
        return {"status": "alert_sent", "days_remaining": days_remaining}

    print(f"[BILLING] OK — {days_remaining} day(s) remaining.")
    return {"status": "ok", "days_remaining": days_remaining}
