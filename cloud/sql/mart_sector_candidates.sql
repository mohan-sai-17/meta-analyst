-- mart_sector_candidates (DuckDB)
-- Non-tech tickers with Oracle green signals (Top Tier analyst coverage
-- and a positive 3M price target). Sector comes from stg_stocks — no
-- yfinance.info calls needed at dashboard or pipeline time.
--
-- Powers the Sector Rotation tab on both cloud and local dashboards.
-- Cloud: served via GET /sector-candidates
-- Local: built by build_sector_candidates.py -> Sector_Candidates.json
--
-- Dependencies (run after): mart_firm_scorecard -> mart_top_tier -> mart_price_targets
-- Requires: stg_stocks must have a Sector column (run bootstrap_sectors_s3.py once).

WITH hist_vol AS (
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
)

SELECT
    pt.Ticker,
    sl.Stock_Name,
    sl.Sector,
    pt.current_price,
    pt.target_price_3m                   AS target_3m,
    pt.target_price_6m                   AS target_6m,
    pt.top_tier_firm_count,
    pt.expected_return_3m_pct,
    pt.expected_return_6m_pct,
    pt.avg_win_rate_3m,
    pt.avg_win_rate_6m,
    coalesce(hv.sigma, 0.25)             AS hist_vol
FROM mart_price_targets pt
JOIN stg_stocks sl ON pt.Ticker = sl.Ticker
LEFT JOIN hist_vol hv ON pt.Ticker = hv.Ticker
WHERE sl.Sector NOT IN ('Technology', 'Communication Services', 'Unknown', '')
  AND sl.Sector IS NOT NULL
  AND pt.target_price_3m > pt.current_price   -- oracle green: target above current price
ORDER BY sl.Sector, pt.expected_return_3m_pct DESC
