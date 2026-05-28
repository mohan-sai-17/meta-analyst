-- mart_daily_candidates (DuckDB)
-- Pre-screened buy candidates: Top Tier upgrades in the last 3 days.
-- Built daily by compute_marts.py -> powers Today's Buy Signals tab (cloud).
--
-- 3-day window (not 2) so Monday's run catches Friday's upgrades across the weekend.
--
-- Dependencies (must run after):
--   mart_firm_scorecard -> mart_top_tier -> mart_price_targets -> mart_seasonality
--
-- Uses stg_stocks view (created in compute_marts.py alongside stg_ohlc/stg_ratings).

WITH top_tier_firms AS (
    SELECT DISTINCT Ticker, Firm
    FROM mart_top_tier
),

recent_upgrades AS (
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
    WHERE r.GradeDate >= CURRENT_DATE - INTERVAL 3 DAY
),

latest_price AS (
    SELECT
        Ticker,
        Close AS current_price
    FROM stg_ohlc
    QUALIFY ROW_NUMBER() OVER (PARTITION BY Ticker ORDER BY Date DESC) = 1
),

hist_vol AS (
    -- 252-day annualised historical volatility per ticker
    SELECT
        Ticker,
        round(stddev(log_ret) * sqrt(252), 4) AS sigma
    FROM (
        SELECT
            Ticker,
            ln(Close / lag(Close, 1) OVER (PARTITION BY Ticker ORDER BY Date)) AS log_ret,
            ROW_NUMBER() OVER (PARTITION BY Ticker ORDER BY Date DESC) AS rn
        FROM stg_ohlc
    ) t
    WHERE rn <= 253 AND log_ret IS NOT NULL
    GROUP BY Ticker
),

seasonality_this_month AS (
    SELECT
        Ticker,
        win_rate   AS season_win_rate,
        avg_return AS season_avg_return
    FROM mart_seasonality
    WHERE month = extract('month' FROM CURRENT_DATE)::INTEGER
)

-- Deduplicate: one row per ticker, keeping the most recent signal.
-- Without this, multiple Top Tier firms upgrading the same stock in the
-- same window would produce duplicate rows (and duplicate emails).
SELECT
    u.Ticker,
    u.Firm,
    u.GradeDate,
    u.Action,
    u.ToGrade,
    lp.current_price,
    pt.target_price_3m                          AS target_3m,
    pt.target_price_6m                          AS target_6m,
    coalesce(sm.season_win_rate,   0)            AS season_win_rate,
    coalesce(sm.season_avg_return, 0)            AS season_avg_return,
    coalesce(hv.sigma,             0.25)         AS hist_vol,
    coalesce(sl.Friends,           '')           AS friends,
    CASE
        WHEN sl.Friends IS NOT NULL
         AND sl.Friends != ''
         AND lower(sl.Friends) LIKE '%' || lower(u.Firm) || '%'
        THEN true
        ELSE false
    END                                          AS is_biased,
    -- Phase 1: Financial health gate (null = data not yet fetched)
    fh.passes_health_screen,
    fh.debt_equity,
    fh.current_ratio,
    fh.interest_coverage,
    -- Phase 3: Insider signal (null = data not yet fetched)
    ins.insider_buy_flag,
    ins.insider_strength,
    ins.net_insider_buy_90d
FROM recent_upgrades u
JOIN latest_price             lp  ON u.Ticker = lp.Ticker
JOIN mart_price_targets       pt  ON u.Ticker = pt.Ticker
LEFT JOIN seasonality_this_month sm  ON u.Ticker = sm.Ticker
LEFT JOIN hist_vol             hv  ON u.Ticker = hv.Ticker
LEFT JOIN stg_stocks           sl  ON u.Ticker = sl.Ticker
LEFT JOIN mart_fundamental_health fh  ON u.Ticker = fh.ticker
LEFT JOIN mart_insider_summary    ins ON u.Ticker = ins.ticker
QUALIFY ROW_NUMBER() OVER (PARTITION BY u.Ticker ORDER BY u.GradeDate DESC, u.Firm) = 1
ORDER BY u.GradeDate DESC, u.Ticker
