-- mart_top_tier (DuckDB)
-- Firms with consistent bullish predictive power per ticker.
-- Mirrors check_analyst_bias() in master_execution.py.
--
-- Criteria: win_rate_6m > 50%, both avg returns > 0, signal_count >= 2

SELECT
    Ticker,
    Firm,
    signal_count,
    avg_return_3m,
    avg_return_6m,
    win_rate_3m,
    win_rate_6m
FROM mart_firm_scorecard
WHERE win_rate_6m   > 50
  AND avg_return_6m > 0
  AND avg_return_3m > 0
  AND signal_count  >= 2
