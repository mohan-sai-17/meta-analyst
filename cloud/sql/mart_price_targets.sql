-- mart_price_targets (DuckDB)
-- Per-ticker 3M and 6M Oracle price targets from Top Tier firm averages.
-- A ticker appears only if at least one Top Tier firm covers it.

WITH latest_price AS (
    SELECT
        Ticker,
        Close AS current_price,
        Date  AS price_date
    FROM stg_ohlc
    QUALIFY ROW_NUMBER() OVER (PARTITION BY Ticker ORDER BY Date DESC) = 1
),

top_tier_agg AS (
    SELECT
        Ticker,
        count(DISTINCT Firm)   AS top_tier_firm_count,
        avg(avg_return_3m)     AS avg_top_tier_return_3m,
        avg(avg_return_6m)     AS avg_top_tier_return_6m,
        avg(win_rate_3m)       AS avg_win_rate_3m,
        avg(win_rate_6m)       AS avg_win_rate_6m
    FROM mart_top_tier
    GROUP BY Ticker
)

SELECT
    t.Ticker,
    lp.current_price,
    lp.price_date,
    t.top_tier_firm_count,
    round(t.avg_top_tier_return_3m, 2)                                        AS expected_return_3m_pct,
    round(t.avg_top_tier_return_6m, 2)                                        AS expected_return_6m_pct,
    round(t.avg_win_rate_3m,        1)                                        AS avg_win_rate_3m,
    round(t.avg_win_rate_6m,        1)                                        AS avg_win_rate_6m,
    round(lp.current_price * (1 + t.avg_top_tier_return_3m / 100), 2)        AS target_price_3m,
    round(lp.current_price * (1 + t.avg_top_tier_return_6m / 100), 2)        AS target_price_6m
FROM top_tier_agg t
JOIN latest_price lp ON t.Ticker = lp.Ticker
