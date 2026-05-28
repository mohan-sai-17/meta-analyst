-- mart_insider_summary (DuckDB)
-- Aggregated Form 4 insider transaction signals per ticker (last 90 days).
-- Source: stg_insider (built from raw/insider/ in compute_marts.py).
--
-- insider_buy_flag  : true when net purchases > net sales in last 90 days
-- insider_strength  : categorical label based on net_insider_buy_90d magnitude
--     strong_buy    : net > $5M  (significant personal commitment)
--     mild_buy      : net > $500K
--     neutral       : net between -$500K and $500K
--     mild_sell     : net between -$5M and -$500K
--     strong_sell   : net < -$5M
--     no_activity   : no transactions in 90d window (not necessarily bearish)
--     no_data       : EDGAR CIK not found or fetch error
--
-- Downstream: mart_daily_candidates LEFT JOINs this table to surface
--   insider_buy_flag and insider_strength alongside analyst signals.

SELECT
    ticker::VARCHAR                                                      AS ticker,
    TRY_CAST(as_of_date AS DATE)                                        AS as_of_date,
    ROUND(TRY_CAST(net_insider_buy_90d   AS DOUBLE), 2)                 AS net_insider_buy_90d,
    COALESCE(TRY_CAST(insider_buy_count_90d  AS INTEGER), 0)            AS insider_buy_count_90d,
    COALESCE(TRY_CAST(insider_sell_count_90d AS INTEGER), 0)            AS insider_sell_count_90d,
    last_transaction_date::VARCHAR                                       AS last_transaction_date,
    insider_buy_flag::BOOLEAN                                            AS insider_buy_flag,

    -- Strength label
    CASE
        WHEN fetch_error = 'not_in_edgar_map'
          OR fetch_error IS NOT NULL                                      THEN 'no_data'
        WHEN COALESCE(TRY_CAST(insider_buy_count_90d  AS INTEGER), 0) = 0
         AND COALESCE(TRY_CAST(insider_sell_count_90d AS INTEGER), 0) = 0
                                                                          THEN 'no_activity'
        WHEN TRY_CAST(net_insider_buy_90d AS DOUBLE) >  5000000          THEN 'strong_buy'
        WHEN TRY_CAST(net_insider_buy_90d AS DOUBLE) >   500000          THEN 'mild_buy'
        WHEN TRY_CAST(net_insider_buy_90d AS DOUBLE) >  -500000          THEN 'neutral'
        WHEN TRY_CAST(net_insider_buy_90d AS DOUBLE) > -5000000          THEN 'mild_sell'
        ELSE                                                               'strong_sell'
    END                                                                   AS insider_strength

FROM stg_insider
ORDER BY ticker
