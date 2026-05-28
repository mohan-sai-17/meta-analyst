-- mart_recent_signals (DuckDB)
-- Top Tier analyst signals from the last 90 days with current price and targets.
-- Powers the Buy Signals tab.

WITH top_tier_firms AS (
    SELECT DISTINCT Ticker, Firm
    FROM mart_top_tier
),

latest_price AS (
    SELECT
        Ticker,
        Close AS current_price,
        Date  AS price_date
    FROM stg_ohlc
    QUALIFY ROW_NUMBER() OVER (PARTITION BY Ticker ORDER BY Date DESC) = 1
),

recent_signals AS (
    SELECT
        r.Ticker,
        r.Firm,
        r.GradeDate,
        r.Action,
        r.ToGrade
    FROM stg_ratings r
    JOIN top_tier_firms ttf
        ON  r.Ticker = ttf.Ticker
        AND r.Firm   = ttf.Firm
    WHERE r.GradeDate >= CURRENT_DATE - INTERVAL 90 DAY
)

SELECT
    s.Ticker,
    s.Firm,
    s.GradeDate,
    s.Action,
    s.ToGrade,
    lp.current_price,
    lp.price_date,
    tt.avg_return_3m,
    tt.avg_return_6m,
    tt.win_rate_6m,
    round(lp.current_price * (1 + tt.avg_return_3m / 100), 2) AS est_target_3m
FROM recent_signals s
JOIN latest_price   lp ON s.Ticker = lp.Ticker
JOIN mart_top_tier  tt ON s.Ticker = tt.Ticker AND s.Firm = tt.Firm
ORDER BY s.GradeDate DESC
