"""
send_alerts.py
==============
Post-pipeline alerting via Amazon SNS.

Reads mart_daily_candidates from S3 and publishes two types of alerts:

  1. Daily summary  — one SNS message listing all new Top Tier signals today
  2. Urgent alerts  — one SNS message per high-confidence signal
                      (clean + positive seasonality this month)

High-confidence criteria:
  - is_biased        = False  (no underwriter conflict)
  - season_win_rate >= 50     (historically positive this month)
  - season_avg_return > 0     (positive average return this month)

Setup (Lambda environment variables / GitHub Secrets for local runs):
  SNS_TOPIC_ARN — ARN of the SNS topic (set in Terraform → Lambda env vars)
"""

import os
import sys
import boto3
import pandas as pd
from io import BytesIO
from datetime import date

S3_BUCKET     = "meta-analyst-data-lake-YOUR_AWS_ACCOUNT_ID"
AWS_REGION    = os.environ.get("AWS_DEFAULT_REGION", "us-east-1")
SNS_TOPIC_ARN = os.environ.get("SNS_TOPIC_ARN", "")
MART_KEY      = "mart/mart_daily_candidates.parquet"


def _publish(metrics: dict):
    """Non-fatal CloudWatch metric push. metrics = {name: value}."""
    try:
        cw = boto3.client("cloudwatch", region_name=AWS_REGION)
        cw.put_metric_data(
            Namespace="MetaAnalyst/Pipeline",
            MetricData=[{"MetricName": k, "Value": float(v), "Unit": "Count"}
                        for k, v in metrics.items()]
        )
        print(f"[METRICS] Published: {metrics}")
    except Exception as e:
        print(f"[METRICS] CloudWatch publish skipped: {e}")


def load_candidates():
    s3  = boto3.client("s3", region_name=AWS_REGION)
    buf = BytesIO()
    s3.download_fileobj(S3_BUCKET, MART_KEY, buf)
    buf.seek(0)
    return pd.read_parquet(buf)


def send_sns(subject, body):
    """Publish a message to the SNS topic. Non-fatal if topic ARN is missing."""
    if not SNS_TOPIC_ARN:
        print(f"[ALERTS] SNS_TOPIC_ARN not set — skipping: {subject}")
        return
    boto3.client("sns", region_name=AWS_REGION).publish(
        TopicArn=SNS_TOPIC_ARN,
        Subject=subject[:100],   # SNS subject max is 100 chars
        Message=body,
    )


def format_daily_summary(df, high_conf_count):
    today = date.today().strftime("%Y-%m-%d")
    n     = len(df)

    lines = [
        f"META-ANALYST DAILY REPORT — {today}",
        f"{'=' * 50}",
        f"{n} NEW TOP TIER SIGNAL{'S' if n != 1 else ''} TODAY",
        f"{'-' * 50}",
        "",
    ]

    for _, row in df.iterrows():
        upside_3m = (float(row["target_3m"]) / float(row["current_price"]) - 1) * 100
        upside_6m = (float(row["target_6m"]) / float(row["current_price"]) - 1) * 100
        bias_tag  = "BIASED" if row["is_biased"] else "CLEAN"
        near_earn = row.get("near_earnings", False)
        earn_days = row.get("days_to_earnings", None)
        earn_tag  = f"NEAR EARNINGS ({earn_days}d)" if near_earn else "OK"
        lines += [
            f"  {row['Ticker']} — {row['Firm']}",
            f"  Action  : {row['Action']} -> {row['ToGrade']}",
            f"  3M Target: ${float(row['target_3m']):.2f} (+{upside_3m:.1f}%)"
            f"  |  6M Target: ${float(row['target_6m']):.2f} (+{upside_6m:.1f}%)",
            f"  Season Win Rate: {float(row['season_win_rate']):.0f}%"
            f"  |  Avg Return: {float(row['season_avg_return']):.1f}%"
            f"  |  {bias_tag}  |  Earnings: {earn_tag}",
            "",
        ]

    lines += [
        f"{'-' * 50}",
        f"High-confidence signals (clean + positive seasonality): {high_conf_count}",
    ]
    if high_conf_count > 0:
        lines.append("Separate urgent emails sent for each.")

    return "\n".join(lines)


def format_urgent_alert(row):
    today     = date.today().strftime("%Y-%m-%d")
    upside_3m = (float(row["target_3m"]) / float(row["current_price"]) - 1) * 100
    upside_6m = (float(row["target_6m"]) / float(row["current_price"]) - 1) * 100
    earn_days = row.get("days_to_earnings", None)
    earn_line = f"  Earnings      : {earn_days}d away" if earn_days is not None else "  Earnings      : N/A"
    return "\n".join([
        f"META-ANALYST — HIGH-CONFIDENCE SIGNAL — {today}",
        f"{'=' * 50}",
        f"",
        f"  Ticker   : {row['Ticker']}",
        f"  Firm     : {row['Firm']}",
        f"  Action   : {row['Action']} -> {row['ToGrade']}",
        f"  Grade Date: {str(row['GradeDate'])[:10]}",
        f"",
        f"  Current Price : ${float(row['current_price']):.2f}",
        f"  3M Target     : ${float(row['target_3m']):.2f} (+{upside_3m:.1f}%)",
        f"  6M Target     : ${float(row['target_6m']):.2f} (+{upside_6m:.1f}%)",
        f"",
        f"  Season Win Rate : {float(row['season_win_rate']):.0f}%",
        f"  Season Avg Ret  : +{float(row['season_avg_return']):.1f}%",
        f"  Hist Vol (sigma): {float(row['hist_vol']):.2%}",
        earn_line,
        f"",
        f"  Bias Check : CLEAN (no underwriter conflict)",
        f"{'=' * 50}",
    ])


def run():
    if not SNS_TOPIC_ARN:
        print("[ALERTS] SNS_TOPIC_ARN not set — alerts will be skipped.")

    print("[ALERTS] Loading mart_daily_candidates from S3...")
    try:
        df = load_candidates()
    except Exception as e:
        print(f"[ALERTS] Could not load candidates: {e}")
        sys.exit(0)

    print(f"[ALERTS] {len(df)} candidate(s) found.")

    # High-confidence filter — exclude biased, poor seasonality, and near earnings
    if df.empty:
        high_conf = pd.DataFrame()
    else:
        near_earnings_mask = df.get("near_earnings", pd.Series([False] * len(df), index=df.index))
        high_conf = df[
            (~df["is_biased"]) &
            (df["season_win_rate"]   >= 50) &
            (df["season_avg_return"]  > 0)  &
            (~near_earnings_mask)
        ].reset_index(drop=True)

    print(f"[ALERTS] {len(high_conf)} high-confidence signal(s).")

    today = date.today().strftime("%Y-%m-%d")

    # Daily summary SNS message
    if df.empty:
        subject = f"META-ANALYST | {today} — No new signals today"
        body    = f"META-ANALYST DAILY REPORT — {today}\n\nNo new Top Tier signals today."
    else:
        subject = f"META-ANALYST | {today} — {len(df)} new Top Tier signal{'s' if len(df) != 1 else ''}"
        body    = format_daily_summary(df, len(high_conf))

    try:
        send_sns(subject, body)
        print("[ALERTS] Daily summary SNS message sent.")
    except Exception as e:
        print(f"[ALERTS] Daily summary SNS publish failed: {e}")

    # Urgent SNS message per high-confidence signal
    alerts_sent = 1  # daily summary counts as 1
    for _, row in high_conf.iterrows():
        subj = f"URGENT | META-ANALYST — {row['Ticker']} High-Confidence Signal"
        body = format_urgent_alert(row)
        try:
            send_sns(subj, body)
            print(f"[ALERTS] Urgent SNS message sent for {row['Ticker']}.")
            alerts_sent += 1
        except Exception as e:
            print(f"[ALERTS] Urgent SNS publish failed for {row['Ticker']}: {e}")

    _publish({
        "CandidatesFound"       : len(df),
        "HighConfidenceSignals" : len(high_conf),
        "AlertsSent"            : alerts_sent,
    })


if __name__ == "__main__":
    run()
