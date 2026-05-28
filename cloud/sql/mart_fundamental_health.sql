-- mart_fundamental_health (DuckDB)
-- Financial health gate: one row per ticker (latest snapshot).
-- Source: stg_fundamentals (built from raw/fundamentals/ in compute_marts.py).
--
-- passes_health_screen gate rules (Lynch-derived, S&P 500 calibrated):
--
--   Debt/Equity  > 2.0   : FAIL — always. Excess leverage is a genuine risk.
--
--   Current Ratio < 1.2  : FAIL — BUT only when FCF_yr0 <= $1B.
--                          Large-cap companies (AAPL, AMZN, COST) deliberately
--                          run lean current ratios because massive free cash flow
--                          makes it unnecessary. Lynch's liquidity concern was
--                          aimed at small companies that could actually go bankrupt.
--                          A company generating >$1B FCF can pay its current
--                          liabilities many times over from operating cash alone.
--
--   Interest Cov < 3.0   : FAIL — only when non-null (skip for banks/zero-debt firms).
--
--   FCF < 2/3 positive   : FAIL — only when all 3 years available. Requires
--                          consistent cash generation, not just GAAP profits.
--
--   Net Income <= 0      : FAIL — both most recent years must be profitable.
--                          Only evaluated when both years are present.
--
--   No balance sheet data: passes_health_screen = NULL (unknown, not a fail).
--
-- Banks: CR and IC are both NULL for banks (no traditional current assets/liabilities).
--   Gate evaluates D/E only — GS/MS correctly fail at D/E > 3.0; most regional
--   banks pass. This is correct Lynch behaviour: bank debt is structural, not distress.
--
-- Downstream: mart_daily_candidates LEFT JOINs this table to expose the gate result.

SELECT
    ticker::VARCHAR                                                     AS ticker,
    TRY_CAST(as_of_date AS DATE)                                       AS as_of_date,
    ROUND(TRY_CAST(debt_equity        AS DOUBLE), 4)                   AS debt_equity,
    ROUND(TRY_CAST(current_ratio      AS DOUBLE), 4)                   AS current_ratio,
    ROUND(TRY_CAST(interest_coverage  AS DOUBLE), 4)                   AS interest_coverage,
    TRY_CAST(fcf_yr0 AS DOUBLE)                                        AS fcf_yr0,
    TRY_CAST(fcf_yr1 AS DOUBLE)                                        AS fcf_yr1,
    TRY_CAST(fcf_yr2 AS DOUBLE)                                        AS fcf_yr2,
    TRY_CAST(ni_yr0  AS DOUBLE)                                        AS ni_yr0,
    TRY_CAST(ni_yr1  AS DOUBLE)                                        AS ni_yr1,

    -- FCF helper: count of positive years out of the available 3
    (CASE WHEN TRY_CAST(fcf_yr0 AS DOUBLE) > 0 THEN 1 ELSE 0 END
   + CASE WHEN TRY_CAST(fcf_yr1 AS DOUBLE) > 0 THEN 1 ELSE 0 END
   + CASE WHEN TRY_CAST(fcf_yr2 AS DOUBLE) > 0 THEN 1 ELSE 0 END)     AS fcf_positive_years,

    -- Health gate (NULL = unknown, true = passes, false = fails)
    CASE
        -- No meaningful balance sheet data at all -> unknown
        WHEN debt_equity   IS NULL
         AND current_ratio IS NULL                                       THEN NULL

        -- Debt/Equity gate — always enforced
        WHEN TRY_CAST(debt_equity AS DOUBLE) > 2.0                     THEN false

        -- Liquidity gate — waived when FCF_yr0 > $1B (cash generation
        -- makes current ratio structurally irrelevant for large-cap firms)
        WHEN TRY_CAST(current_ratio AS DOUBLE) < 1.2
         AND (fcf_yr0 IS NULL
              OR TRY_CAST(fcf_yr0 AS DOUBLE) <= 1000000000)            THEN false

        -- Interest coverage gate (skip when NULL — bank / zero-debt firm)
        WHEN interest_coverage IS NOT NULL
         AND TRY_CAST(interest_coverage AS DOUBLE) < 3.0               THEN false

        -- FCF consistency gate: need all 3 years; fail if < 2 are positive
        WHEN fcf_yr0 IS NOT NULL
         AND fcf_yr1 IS NOT NULL
         AND fcf_yr2 IS NOT NULL
         AND (CASE WHEN TRY_CAST(fcf_yr0 AS DOUBLE) > 0 THEN 1 ELSE 0 END
            + CASE WHEN TRY_CAST(fcf_yr1 AS DOUBLE) > 0 THEN 1 ELSE 0 END
            + CASE WHEN TRY_CAST(fcf_yr2 AS DOUBLE) > 0 THEN 1 ELSE 0 END) < 2
                                                                         THEN false

        -- Net income gate: both most recent years must be positive
        WHEN ni_yr0 IS NOT NULL
         AND ni_yr1 IS NOT NULL
         AND (TRY_CAST(ni_yr0 AS DOUBLE) <= 0
           OR TRY_CAST(ni_yr1 AS DOUBLE) <= 0)                         THEN false

        ELSE true
    END                                                                  AS passes_health_screen

FROM stg_fundamentals
WHERE fetch_error IS NULL
   OR fetch_error = ''

ORDER BY ticker
