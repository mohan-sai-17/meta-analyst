-- mart_seasonality (DuckDB)
-- Monthly win rate and avg return per ticker across all historical years.
-- Powers the Weatherman tab.

WITH monthly_boundaries AS (
    SELECT
        Ticker,
        extract('year'  FROM Date)::INTEGER AS year,
        extract('month' FROM Date)::INTEGER AS month,
        min(Date)                           AS first_date,
        max(Date)                           AS last_date
    FROM stg_ohlc
    GROUP BY Ticker,
             extract('year'  FROM Date),
             extract('month' FROM Date)
),

with_prices AS (
    SELECT
        mb.Ticker,
        mb.year,
        mb.month,
        o_open.Close  AS month_open,
        o_close.Close AS month_close
    FROM monthly_boundaries mb
    JOIN stg_ohlc o_open
        ON mb.Ticker = o_open.Ticker AND mb.first_date = o_open.Date
    JOIN stg_ohlc o_close
        ON mb.Ticker = o_close.Ticker AND mb.last_date  = o_close.Date
),

monthly_returns AS (
    SELECT
        Ticker,
        year,
        month,
        (month_close - month_open) / NULLIF(month_open, 0) * 100 AS monthly_return
    FROM with_prices
    WHERE month_open > 0
)

SELECT
    Ticker,
    month,
    count(*)                                                                       AS years_observed,
    round(count(*) FILTER (WHERE monthly_return > 0)::DOUBLE / count(*) * 100, 1) AS win_rate,
    round(avg(monthly_return), 2)                                                  AS avg_return
FROM monthly_returns
GROUP BY Ticker, month
ORDER BY Ticker, month
