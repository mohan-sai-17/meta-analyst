-- mart_firm_scorecard (DuckDB)
-- For every (Ticker, Firm) pair compute look-forward returns and win rates.
--
-- Logic: entry price = first close on/after GradeDate (within 7 days)
--        exit 3M    = first close >= entry_date + 90 days (within 7-day window)
--        exit 6M    = first close >= entry_date + 180 days (within 7-day window)
--        Aggregate per (Ticker, Firm): avg returns, win rates

WITH entry_prices AS (
    SELECT
        r.Ticker,
        r.Firm,
        r.GradeDate,
        o.Date   AS entry_date,
        o.Close  AS entry_price
    FROM stg_ratings r
    JOIN stg_ohlc o
        ON  r.Ticker = o.Ticker
        AND o.Date  >= r.GradeDate
        AND o.Date  <  r.GradeDate + INTERVAL 7 DAY
    QUALIFY ROW_NUMBER() OVER (
        PARTITION BY r.Ticker, r.Firm, r.GradeDate
        ORDER BY o.Date
    ) = 1
),

exit_3m AS (
    SELECT
        e.Ticker,
        e.Firm,
        e.GradeDate,
        o.Close AS price_3m
    FROM entry_prices e
    JOIN stg_ohlc o
        ON  e.Ticker = o.Ticker
        AND o.Date  >= e.entry_date + INTERVAL 90 DAY
        AND o.Date  <  e.entry_date + INTERVAL 97 DAY
    QUALIFY ROW_NUMBER() OVER (
        PARTITION BY e.Ticker, e.Firm, e.GradeDate
        ORDER BY o.Date
    ) = 1
),

exit_6m AS (
    SELECT
        e.Ticker,
        e.Firm,
        e.GradeDate,
        o.Close AS price_6m
    FROM entry_prices e
    JOIN stg_ohlc o
        ON  e.Ticker = o.Ticker
        AND o.Date  >= e.entry_date + INTERVAL 180 DAY
        AND o.Date  <  e.entry_date + INTERVAL 187 DAY
    QUALIFY ROW_NUMBER() OVER (
        PARTITION BY e.Ticker, e.Firm, e.GradeDate
        ORDER BY o.Date
    ) = 1
),

returns AS (
    SELECT
        e.Ticker,
        e.Firm,
        e.GradeDate,
        (e3.price_3m - e.entry_price) / NULLIF(e.entry_price, 0) * 100 AS return_3m,
        (e6.price_6m - e.entry_price) / NULLIF(e.entry_price, 0) * 100 AS return_6m
    FROM entry_prices e
    LEFT JOIN exit_3m e3
        ON  e.Ticker    = e3.Ticker
        AND e.Firm      = e3.Firm
        AND e.GradeDate = e3.GradeDate
    LEFT JOIN exit_6m e6
        ON  e.Ticker    = e6.Ticker
        AND e.Firm      = e6.Firm
        AND e.GradeDate = e6.GradeDate
    WHERE e3.price_3m IS NOT NULL
       OR e6.price_6m IS NOT NULL
)

SELECT
    Ticker,
    Firm,
    count(*)                                                                    AS signal_count,
    round(avg(return_3m), 2)                                                    AS avg_return_3m,
    round(avg(return_6m), 2)                                                    AS avg_return_6m,
    round(count(*) FILTER (WHERE return_3m > 0)::DOUBLE / count(*) * 100, 1)   AS win_rate_3m,
    round(count(*) FILTER (WHERE return_6m > 0)::DOUBLE / count(*) * 100, 1)   AS win_rate_6m
FROM returns
GROUP BY Ticker, Firm
